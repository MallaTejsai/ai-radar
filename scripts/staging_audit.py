"""AI Radar V1.0 - read-only staging audit.

Safety contract (enforced here, not by convention):

* The staging DSN arrives only through ``AI_RADAR_STAGING_DATABASE_URL`` in the
  process environment; it is never printed, logged, written to disk or put on a
  command line.
* ``AI_RADAR_STAGING=1`` is required and the endpoint is validated against the
  production signature (host, port, user) before a connection is opened. The
  production DSN itself is parsed, never connected to.
* Every statement runs inside a session marked READ ONLY, so an accidental write
  is refused by PostgreSQL itself. Only ``SELECT`` / ``SHOW`` statements exist.
* No provider is called, no Telegram message is sent, no research cycle runs.

The report (``artifacts/staging_audit_report.json``) stores masked endpoints and
fingerprints only - never the DSN.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ai_radar.settings import AppConfig, load_config
from ai_radar.storage import Database
from ai_radar.storage.db import (
    assert_staging_target,
    dsn_fingerprint,
    endpoint_signature,
    masked_endpoint,
    validate_dsn,
)

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / "artifacts"
REPORT = ARTIFACTS / "staging_audit_report.json"
ALERT_TITLE_HINT = "art of play"
ALERT_CATEGORY_HINT = "AI Coding Tools"

# Every statement here is bound with parameters. A literal percent sign in SQL is
# never written inline: psycopg parses the statement for placeholders whenever a
# parameter sequence is supplied (even an empty one), so `LIKE '%provider%'` would
# fail with "only '%s', '%b', '%t' are allowed as placeholders, got '%p'".
# LIKE patterns are therefore passed as bound values, and would be written as
# `%%` only inside a template that is later parameterised.
PROVIDER_CONSTRAINT_SQL = """
    SELECT pg_get_constraintdef(c.oid)
    FROM pg_constraint c
    JOIN pg_class t ON t.oid = c.conrelid
    WHERE t.relname = 'analysis_results' AND c.contype = 'c'
      AND pg_get_constraintdef(c.oid) LIKE %s
    LIMIT 1
"""
PROVIDER_CONSTRAINT_PARAMS = ("%provider%",)

ALERT_LOOKUP_SQL = (
    "SELECT id, canonical_key, title, category, source_id, tier, priority_score, "
    "priority_label, confidence, verdict, status, published_at, discovered_at, url, "
    "summary, score_breakdown, verification FROM discoveries "
    "WHERE lower(title) LIKE %s ORDER BY created_at DESC LIMIT 5"
)


def alert_lookup_params() -> tuple[str, ...]:
    """Bind the LIKE pattern rather than interpolating it into the statement."""
    return (f"%{ALERT_TITLE_HINT}%",)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (datetime,)):
        return value.isoformat()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _run_async(coro):
    """Windows defaults to ProactorEventLoop, which psycopg's async driver refuses."""
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    return asyncio.run(coro)


class Audit:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.settings = config.settings
        self.db: Database | None = None
        self.report: dict[str, Any] = {
            "started_at": _now(),
            "mode": "read-only-staging-audit",
            "staging": {},
            "integrity": {},
            "migrations": {},
            "runs": {},
            "sources": {},
            "discoveries": {},
            "provider_evidence": {},
            "alert": {},
            "deliveries": {},
            "checks": [],
            "failures": [],
            "finished_at": None,
        }

    # ------------------------------------------------------------------ guards
    def guard(self) -> str:
        settings = self.settings
        if not settings.ai_radar_staging:
            raise SystemExit("AI_RADAR_STAGING is not 1: this audit never runs against production")

        staging_dsn = settings.ai_radar_staging_database_url
        production_dsn = settings.database_url
        validate_dsn(staging_dsn, label="AI_RADAR_STAGING_DATABASE_URL")
        assert_staging_target(
            staging_dsn,
            staging=True,
            production_dsn=production_dsn,
        )
        staging_sig = endpoint_signature(staging_dsn)
        production_sig = endpoint_signature(production_dsn) if production_dsn else ("", "", "")
        if production_sig[0] and staging_sig == production_sig:
            raise SystemExit("staging signature equals the production signature; refusing to connect")

        self.report["staging"] = {
            "staging": masked_endpoint(staging_dsn),
            "staging_signature": list(staging_sig),
            "production": masked_endpoint(production_dsn),
            "production_signature": list(production_sig),
            "fingerprint": dsn_fingerprint(staging_dsn),
            "production_connected": False,
        }
        return staging_dsn

    def check(self, name: str, ok: bool, detail: str) -> None:
        self.report["checks"].append({"check": name, "ok": bool(ok), "detail": detail})
        print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}")

    # ------------------------------------------------------------------- audit
    async def run(self) -> None:
        dsn = self.guard()
        self.db = Database(dsn, min_size=1, max_size=2, connect_timeout=20.0)
        await self.db.connect()
        try:
            async with self.db.session() as conn:
                # Belt and braces: PostgreSQL refuses any write in this session.
                await conn.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
                async with conn.cursor() as cur:
                    await self._collect(cur)
        finally:
            await self.db.close()

    async def _rows(self, cur, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        await cur.execute(sql, params)
        rows = await cur.fetchall()
        names = [d.name for d in (cur.description or [])]
        return [_jsonable(dict(zip(names, row))) for row in rows]

    async def _scalar(self, cur, sql: str, params: tuple = ()) -> Any:
        await cur.execute(sql, params)
        row = await cur.fetchone()
        return _jsonable(row[0]) if row else None

    async def _collect(self, cur) -> None:
        r = self.report

        # ---------------------------------------------------------- migrations
        ledger = await self._rows(
            cur, "SELECT version, checksum, applied_at FROM schema_migrations ORDER BY version"
        )
        applied_versions = [row["version"] for row in ledger]
        local = {m["version"]: m["checksum"] for m in _migrations()}
        r["migrations"] = {
            "ledger": ledger,
            "discovered": list(local),
            "pending_expected": [v for v in local if v not in applied_versions],
            "checksum_mismatches": [
                {"version": row["version"], "ledger": row["checksum"], "local": local.get(row["version"])}
                for row in ledger
                if local.get(row["version"]) != row["checksum"]
            ],
            "provider_constraint": await self._scalar(
                cur, PROVIDER_CONSTRAINT_SQL, PROVIDER_CONSTRAINT_PARAMS
            ),
        }

        # ------------------------------------------------------- table counts
        counts = {}
        for table in (
            "sources",
            "discoveries",
            "discovery_sources",
            "analysis_results",
            "analysis_cache",
            "provider_usage",
            "deliveries",
            "workflow_runs",
            "daily_digests",
            "backup_manifests",
            "pricing_history",
            "system_state",
        ):
            counts[table] = await self._scalar(cur, f"SELECT count(*) FROM {table}")
        r["discoveries"]["counts"] = counts

        # ------------------------------------------------------------ integrity
        r["integrity"] = {
            "duplicate_canonical_keys": await self._scalar(
                cur,
                "SELECT count(*) FROM (SELECT canonical_key FROM discoveries "
                "GROUP BY canonical_key HAVING count(*) > 1) dup",
            ),
            "duplicate_title_fingerprints": await self._rows(
                cur,
                "SELECT title_fingerprint, count(*) AS n FROM discoveries "
                "GROUP BY title_fingerprint HAVING count(*) > 1 ORDER BY n DESC LIMIT 20",
            ),
            "discoveries_without_analysis": await self._scalar(
                cur,
                "SELECT count(*) FROM discoveries d LEFT JOIN analysis_results a "
                "ON a.discovery_id = d.id WHERE a.id IS NULL",
            ),
            "analyses_without_discovery": await self._scalar(
                cur,
                "SELECT count(*) FROM analysis_results a LEFT JOIN discoveries d "
                "ON d.id = a.discovery_id WHERE d.id IS NULL",
            ),
            "deliveries_without_discovery": await self._scalar(
                cur,
                "SELECT count(*) FROM deliveries x LEFT JOIN discoveries d "
                "ON d.id = x.discovery_id WHERE d.id IS NULL",
            ),
            "deliveries_without_message_hash": await self._scalar(
                cur, "SELECT count(*) FROM deliveries WHERE coalesce(message_hash,'') = ''"
            ),
            "duplicate_delivery_intents": await self._scalar(
                cur,
                "SELECT count(*) FROM (SELECT discovery_id, channel FROM deliveries "
                "GROUP BY discovery_id, channel HAVING count(*) > 1) dup",
            ),
            "discoveries_without_evidence": await self._scalar(
                cur,
                "SELECT count(*) FROM discoveries d LEFT JOIN discovery_sources s "
                "ON s.discovery_id = d.id WHERE s.discovery_id IS NULL",
            ),
            "analysis_provider_counts": await self._rows(
                cur,
                "SELECT provider, model_id, count(*) AS n, min(created_at) AS first_seen, "
                "max(created_at) AS last_seen FROM analysis_results "
                "GROUP BY provider, model_id ORDER BY n DESC",
            ),
        }

        # ----------------------------------------------------------- workflow
        runs = await self._rows(
            cur,
            "SELECT run_id, trigger, status, started_at, finished_at, sources_attempted, "
            "sources_failed, entries_collected, after_dedup, verified, qualified, "
            "persisted_new, alerts_sent, recovered_missed_runs, duration_seconds, report "
            "FROM workflow_runs ORDER BY started_at DESC LIMIT 12",
        )
        r["runs"] = {
            "recent": runs,
            "total": counts.get("workflow_runs"),
            "latest": runs[0] if runs else None,
            "per_status": await self._rows(
                cur, "SELECT status, count(*) AS n FROM workflow_runs GROUP BY status"
            ),
        }

        # -------------------------------------------------------------- sources
        r["sources"] = {
            "health": await self._rows(
                cur,
                "SELECT source_id, category, tier, enabled, health_status, "
                "consecutive_failures, last_checkpoint, last_success_at, last_error, "
                "records_collected FROM sources ORDER BY source_id",
            ),
            "by_category": await self._rows(
                cur, "SELECT category, count(*) AS n FROM sources WHERE enabled GROUP BY category ORDER BY category"
            ),
        }

        # --------------------------------------------------------- discoveries
        latest_start = runs[0]["started_at"] if runs else None
        r["discoveries"]["by_category"] = await self._rows(
            cur, "SELECT category, count(*) AS n FROM discoveries GROUP BY category ORDER BY n DESC"
        )
        r["discoveries"]["by_verdict"] = await self._rows(
            cur, "SELECT verdict, count(*) AS n FROM discoveries GROUP BY verdict ORDER BY n DESC"
        )
        r["discoveries"]["by_status"] = await self._rows(
            cur, "SELECT status, count(*) AS n FROM discoveries GROUP BY status ORDER BY n DESC"
        )
        r["discoveries"]["priority_bands"] = await self._rows(
            cur,
            "SELECT priority_label, count(*) AS n, min(priority_score) AS min_score, "
            "max(priority_score) AS max_score FROM discoveries GROUP BY priority_label "
            "ORDER BY priority_label",
        )
        r["discoveries"]["confidence_stats"] = await self._rows(
            cur,
            "SELECT count(*) AS n, min(confidence) AS min_c, max(confidence) AS max_c, "
            "avg(confidence) AS avg_c, count(*) FILTER (WHERE confidence >= 0.57) AS at_or_above_threshold "
            "FROM discoveries",
        )
        if latest_start:
            r["discoveries"]["latest_run_window"] = await self._rows(
                cur,
                "SELECT count(*) AS n, min(created_at) AS first_created, max(created_at) AS last_created "
                "FROM discoveries WHERE created_at >= %s",
                (latest_start,),
            )
        r["discoveries"]["recent"] = await self._rows(
            cur,
            "SELECT id, canonical_key, title, category, source_id, tier, priority_score, "
            "priority_label, confidence, verdict, status, published_at, url, "
            "discovered_at, created_at, updated_at FROM discoveries "
            "ORDER BY created_at DESC LIMIT 25",
        )

        # -------------------------------------------------- provider evidence
        usage_by_provider = await self._rows(
            cur,
            "SELECT provider, model_id, ok, error_kind, count(*) AS n, "
            "sum(prompt_tokens) AS prompt_tokens, sum(completion_tokens) AS completion_tokens, "
            "avg(latency_ms)::int AS avg_latency_ms, max(latency_ms) AS max_latency_ms, "
            "min(occurred_at) AS first_seen, max(occurred_at) AS last_seen "
            "FROM provider_usage GROUP BY provider, model_id, ok, error_kind "
            "ORDER BY provider, ok DESC, n DESC",
        )
        sarvam_rows = await self._rows(
            cur,
            "SELECT id, provider, model_id, ok, error_kind, latency_ms, prompt_tokens, "
            "completion_tokens, occurred_at FROM provider_usage WHERE provider = 'sarvam' "
            "ORDER BY occurred_at DESC LIMIT 25",
        )
        r["provider_evidence"] = {
            "usage_by_provider": usage_by_provider,
            "sarvam_rows": sarvam_rows,
            "sarvam_calls": len(sarvam_rows),
            "sarvam_successes": sum(1 for row in sarvam_rows if row["ok"]),
            "xai_rows": await self._rows(
                cur,
                "SELECT id, model_id, ok, error_kind, latency_ms, prompt_tokens, completion_tokens, "
                "occurred_at FROM provider_usage WHERE provider IN ('grok','xai') "
                "ORDER BY occurred_at DESC LIMIT 10",
            ),
            "cache_by_provider": await self._rows(
                cur, "SELECT provider, model_id, count(*) AS n FROM analysis_cache GROUP BY provider, model_id"
            ),
        }
        if latest_start:
            r["provider_evidence"]["latest_run_window"] = await self._rows(
                cur,
                "SELECT provider, ok, error_kind, count(*) AS n, sum(prompt_tokens) AS prompt_tokens, "
                "sum(completion_tokens) AS completion_tokens FROM provider_usage "
                "WHERE occurred_at >= %s GROUP BY provider, ok, error_kind ORDER BY provider",
                (latest_start,),
            )

        # ------------------------------------------------------- alert target
        alert_rows = await self._rows(cur, ALERT_LOOKUP_SQL, alert_lookup_params())
        r["alert"]["discoveries"] = alert_rows
        if alert_rows:
            discovery = alert_rows[0]
            did = discovery["id"]
            r["alert"]["analysis"] = await self._rows(
                cur,
                "SELECT id, provider, model_id, prompt_version, input_fingerprint, latency_ms, "
                "cached, fallback_reason, created_at, payload FROM analysis_results "
                "WHERE discovery_id = %s ORDER BY created_at DESC",
                (did,),
            )
            r["alert"]["evidence"] = await self._rows(
                cur,
                "SELECT discovery_id, source_id, evidence_url, tier, domain, http_status, "
                "reachable, checked_at FROM discovery_sources WHERE discovery_id = %s",
                (did,),
            )
            r["alert"]["deliveries"] = await self._rows(
                cur,
                "SELECT id, status, attempts, telegram_message_id, last_error, claimed_by, "
                "claimed_at, next_attempt_at, created_at, updated_at, message_hash, "
                "message_text FROM deliveries WHERE discovery_id = %s",
                (did,),
            )
            r["alert"]["pricing_history"] = await self._rows(
                cur,
                "SELECT pricing_status, restricted, evidence, observed_at FROM pricing_history "
                "WHERE discovery_id = %s ORDER BY observed_at DESC",
                (did,),
            )
            r["alert"]["url_probe"] = await _probe_url(discovery.get("url") or "")

        # ---------------------------------------------------------- deliveries
        r["deliveries"] = {
            "all": await self._rows(
                cur,
                "SELECT x.id, x.status, x.attempts, x.telegram_message_id, x.last_error, "
                "x.claimed_by, x.created_at, x.updated_at, d.title, d.category, "
                "d.priority_label, d.confidence, d.verdict, d.source_id, d.url "
                "FROM deliveries x JOIN discoveries d ON d.id = x.discovery_id "
                "ORDER BY x.created_at DESC LIMIT 30",
            ),
            "by_status": await self._rows(
                cur, "SELECT status, count(*) AS n FROM deliveries GROUP BY status ORDER BY status"
            ),
            "sent": await self._rows(
                cur,
                "SELECT x.id, x.telegram_message_id, x.attempts, x.updated_at, d.title "
                "FROM deliveries x JOIN discoveries d ON d.id = x.discovery_id "
                "WHERE x.status = 'SENT' ORDER BY x.updated_at DESC",
            ),
            "digests": await self._rows(
                cur,
                "SELECT digest_date, discovery_count, telegram_message_id, created_at "
                "FROM daily_digests ORDER BY digest_date DESC LIMIT 10",
            ),
            "backups": await self._rows(
                cur,
                "SELECT backup_id, status, created_at, validated_at, restore_tested_at, file_name "
                "FROM backup_manifests ORDER BY created_at DESC LIMIT 10",
            ),
        }

        # ------------------------------------------------------------- verdict
        ledger_versions = [row["version"] for row in r["migrations"]["ledger"]]
        checks = [
            ("migrations 001-004 applied", ledger_versions == [
                "001_initial_schema", "002_runtime_state", "003_freeai_provider", "004_provider_check"
            ], f"ledger contains {ledger_versions}"),
            ("provider CHECK includes sarvam", "sarvam" in (r["migrations"]["provider_constraint"] or ""),
             str(r["migrations"]["provider_constraint"])[:160]),
            ("no duplicate canonical keys", r["integrity"]["duplicate_canonical_keys"] == 0,
             f"{r['integrity']['duplicate_canonical_keys']} duplicate(s)"),
            ("no orphan analyses", r["integrity"]["analyses_without_discovery"] == 0,
             f"{r['integrity']['analyses_without_discovery']} orphan(s)"),
            ("no orphan deliveries", r["integrity"]["deliveries_without_discovery"] == 0,
             f"{r['integrity']['deliveries_without_discovery']} orphan(s)"),
            ("alert discovery present in staging", bool(alert_rows),
             alert_rows[0]["title"] if alert_rows else "not found"),
            ("first research run exists", counts.get("workflow_runs", 0) >= 1,
             f"{counts.get('workflow_runs')} run(s)"),
        ]
        if alert_rows:
            analysis = r["alert"].get("analysis") or []
            checks.append(("alert has persisted analysis", bool(analysis),
                           analysis[0]["provider"] if analysis else "no analysis row"))
        checks = [(name, bool(ok), detail) for name, ok, detail in checks]
        r["checks_actionable"] = [{"check": n, "ok": o, "detail": d} for n, o, d in checks]
        if not self.report["checks"]:
            # An empty `checks` array made the report look as though no check had
            # run. The verdict list *is* the check record; mirror it.
            self.report["checks"] = [dict(item) for item in r["checks_actionable"]]

    def finish(self) -> None:
        self.report["finished_at"] = _now()
        failures = [c for c in self.report.get("checks_actionable", []) if not c["ok"]]
        self.report["failures"] = failures
        self.report["ok"] = not failures


def _migrations() -> list[dict[str, str]]:
    from ai_radar.storage.migrations import checksum, discover_migrations

    return [{"version": path.stem, "checksum": checksum(path)} for path in discover_migrations()]


async def _probe_url(url: str) -> dict[str, Any]:
    """Confirm the evidence URL still resolves to the publisher (read-only GET)."""
    if not url:
        return {"url": "", "status": None, "note": "no URL stored"}
    try:
        import urllib.request

        request = urllib.request.Request(
            url, method="GET", headers={"User-Agent": "AI-Radar-Audit/1.0 (+read-only)"}
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            body = response.read(4000).decode("utf-8", "ignore")
            title = ""
            lowered = body.lower()
            if "<title>" in lowered:
                start = lowered.index("<title>") + 7
                end = lowered.find("</title>", start)
                title = body[start:end].strip()[:200]
            return {
                "url": url,
                "status": response.status,
                "final_url": response.geturl(),
                "content_type": response.headers.get("Content-Type"),
                "html_title": title,
                "host": urlsplit(response.geturl()).hostname,
            }
    except Exception as exc:  # noqa: BLE001 - a probe failure is evidence, not a crash
        return {"url": url, "status": None, "note": f"{type(exc).__name__}: {exc}"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only AI Radar staging audit")
    parser.add_argument("--out", default=str(REPORT))
    args = parser.parse_args(argv)

    config = load_config()
    audit = Audit(config)
    _run_async(audit.run())
    audit.finish()

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(audit.report, indent=2, default=str), encoding="utf-8")

    print("\n=== summary ===")
    staging = audit.report["staging"]
    print(f"staging endpoint : {staging.get('staging')}")
    print(f"fingerprint      : {staging.get('fingerprint')}")
    for check in audit.report.get("checks_actionable", []):
        print(f"[{'PASS' if check['ok'] else 'FAIL'}] {check['check']}: {check['detail']}")
    print(f"report           : {out}")
    print(f"overall          : {'OK' if audit.report.get('ok') else 'ATTENTION REQUIRED'}")
    return 0 if audit.report.get("ok") else 3


if __name__ == "__main__":
    raise SystemExit(main())
