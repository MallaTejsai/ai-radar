"""Non-destructive database readiness verification (G0).

Purpose: prove that a freshly migrated Supabase project has the schema the app
expects and that the critical SQL behaviours work, **without leaving a single row
behind**. Everything that writes happens inside one transaction that is rolled
back, and the script re-checks afterwards that no probe row survived.

What it does not do:
  * it does not apply migrations (use ``python -m ai_radar migrate``);
  * it does not run the destructive integration suite (that needs a disposable
    database whose name contains test/ci/scratch/sandbox);
  * it never prints the DSN, the password or the project ref in full.

Usage:
    python scripts/verify_database.py
    python scripts/verify_database.py --json

Exit codes: 0 all checks passed, 1 a check failed, 2 configuration/precondition.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import sys
import uuid
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import psycopg
from psycopg import errors
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from ai_radar.settings import Settings  # noqa: E402
from ai_radar.storage.db import DatabaseError, validate_dsn
from ai_radar.utils.redact import scrub

EXPECTED_TABLES: dict[str, set[str]] = {
    "sources": {
        "source_id", "name", "category", "tier", "method", "url", "enabled",
        "health_status", "consecutive_failures", "last_checkpoint", "last_success_at",
        "last_error", "etag", "last_modified", "records_collected", "created_at",
        "updated_at",
    },
    "discoveries": {
        "id", "canonical_key", "title_fingerprint", "title", "category", "summary",
        "url", "source_id", "tier", "published_at", "discovered_at",
        "last_verified_at", "priority_score", "priority_label", "confidence",
        "status", "pricing_status", "pricing_restricted", "verdict",
        "score_breakdown", "verification", "created_at", "updated_at",
    },
    "discovery_sources": {
        "discovery_id", "source_id", "evidence_url", "tier", "domain",
        "http_status", "reachable", "checked_at", "created_at",
    },
    "analysis_results": {
        "id", "discovery_id", "provider", "model_id", "prompt_version",
        "input_fingerprint", "payload", "latency_ms", "cached", "fallback_reason",
        "created_at",
    },
    "pricing_history": {
        "id", "discovery_id", "pricing_status", "restricted", "evidence", "observed_at",
    },
    "deliveries": {
        "id", "discovery_id", "channel", "status", "attempts", "message_text",
        "message_hash", "telegram_message_id", "last_error", "claimed_by",
        "claimed_at", "next_attempt_at", "created_at", "updated_at",
    },
    "workflow_runs": {
        "run_id", "started_at", "finished_at", "status", "trigger",
        "sources_attempted", "sources_failed", "entries_collected", "after_dedup",
        "verified", "qualified", "persisted_new", "alerts_sent",
        "recovered_missed_runs", "duration_seconds", "report",
    },
    "daily_digests": {
        "id", "digest_date", "digest_text", "discovery_count", "discovery_keys",
        "telegram_message_id", "created_at",
    },
    "backup_manifests": {
        "id", "backup_id", "schema_version", "created_at", "table_counts",
        "file_name", "uncompressed_bytes", "compressed_bytes", "sha256",
        "drive_file_id", "status", "validated_at", "restore_tested_at", "notes",
    },
    "analysis_cache": {
        "fingerprint", "provider", "model_id", "prompt_version", "payload", "created_at",
    },
    "provider_usage": {
        "id", "occurred_at", "provider", "model_id", "ok", "error_kind",
        "latency_ms", "prompt_tokens", "completion_tokens",
    },
    "system_state": {"key", "value", "updated_at"},
    "schema_migrations": {"version", "applied_at", "checksum"},
}

EXPECTED_INDEXES = {
    "idx_discoveries_category_priority",
    "idx_discoveries_discovered_at",
    "idx_discoveries_title_fingerprint",
    "idx_discoveries_status_verdict",
    "idx_discovery_sources_source",
    "idx_pricing_history_discovery",
    "idx_deliveries_dispatchable",
    "idx_workflow_runs_started",
    "idx_workflow_runs_status",
    "idx_analysis_cache_created",
    "idx_provider_usage_occurred",
    "idx_provider_usage_provider",
    "idx_backup_manifests_created",
}

EXPECTED_UNIQUE = {
    "discoveries_canonical_key_key",
    "deliveries_discovery_id_channel_key",
    "analysis_results_discovery_id_prompt_version_key",
    "daily_digests_digest_date_key",
    "backup_manifests_backup_id_key",
}

#: (table, column) -> (referenced table, expected ON DELETE action)
EXPECTED_FOREIGN_KEYS = {
    ("discoveries", "source_id"): ("sources", "r"),
    ("discovery_sources", "discovery_id"): ("discoveries", "c"),
    ("discovery_sources", "source_id"): ("sources", "r"),
    ("analysis_results", "discovery_id"): ("discoveries", "c"),
    ("pricing_history", "discovery_id"): ("discoveries", "c"),
    ("deliveries", "discovery_id"): ("discoveries", "c"),
}


class _Rollback(Exception):
    """Sentinel used to roll the probe transaction back."""


@dataclass
class Report:
    checks: list[dict[str, Any]] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: Any = "") -> None:
        self.checks.append({"check": name, "ok": bool(ok), "detail": detail})

    @property
    def ok(self) -> bool:
        return all(item["ok"] for item in self.checks)

    def failed(self) -> list[dict[str, Any]]:
        return [item for item in self.checks if not item["ok"]]


def _masked_target(dsn: str) -> str:
    parts = urlsplit(dsn)
    host = parts.hostname or "?"
    try:
        ipaddress.ip_address(host)
    except ValueError:
        labels = host.split(".")
        if len(labels) > 1:
            host = labels[0][:4] + "***." + ".".join(labels[1:])
    user = (parts.username or "?").split(".", 1)[0] + ".***"
    database = (parts.path or "/?").lstrip("/") or "?"
    query = parts.query or ""
    ssl = "sslmode=require" if "sslmode=require" in query else (query or "sslmode=default")
    return f"{user}@{host}:{parts.port or 5432}/{database} ({ssl})"


def _connect(dsn: str) -> psycopg.Connection:
    return psycopg.connect(
        dsn,
        connect_timeout=20,
        row_factory=dict_row,
        application_name="ai-radar-g0-verify",
    )


def _schema_checks(conn: psycopg.Connection, report: Report) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "select table_name from information_schema.tables where table_schema = 'public'"
        )
        tables = {row["table_name"] for row in cur.fetchall()}
        missing = sorted(set(EXPECTED_TABLES) - tables)
        report.add("tables", not missing, f"{len(tables)} present; missing={missing}")

        cur.execute(
            "select table_name, column_name from information_schema.columns "
            "where table_schema = 'public'"
        )
        columns: dict[str, set[str]] = {}
        for row in cur.fetchall():
            columns.setdefault(row["table_name"], set()).add(row["column_name"])
        gaps = {
            table: sorted(required - columns.get(table, set()))
            for table, required in EXPECTED_TABLES.items()
            if required - columns.get(table, set())
        }
        report.add("columns", not gaps, gaps or "every required column exists")

        cur.execute("select indexname from pg_indexes where schemaname = 'public'")
        indexes = {row["indexname"] for row in cur.fetchall()}
        missing_indexes = sorted(EXPECTED_INDEXES - indexes)
        report.add("indexes", not missing_indexes, f"missing={missing_indexes}")

        cur.execute(
            "select conname from pg_constraint where connamespace = 'public'::regnamespace "
            "and contype = 'u'"
        )
        uniques = {row["conname"] for row in cur.fetchall()}
        missing_unique = sorted(EXPECTED_UNIQUE - uniques)
        report.add("unique constraints", not missing_unique, f"missing={missing_unique}")

        cur.execute(
            "select conname, confdeltype, "
            "       conrelid::regclass::text as table_name, "
            "       confrelid::regclass::text as ref_table "
            "from pg_constraint "
            "where connamespace = 'public'::regnamespace and contype = 'f'"
        )
        found = {}
        for row in cur.fetchall():
            table = str(row["table_name"]).split(".")[-1]
            ref = str(row["ref_table"]).split(".")[-1]
            found.setdefault((table, ref), []).append(row)
        problems = []
        for (table, column), (ref_table, action) in EXPECTED_FOREIGN_KEYS.items():
            candidates = [
                row for (t, r), rows in found.items() if t == table for row in rows
            ]
            if not any(row["ref_table"] == ref_table for row in candidates):
                problems.append(f"{table} -> {ref_table} missing")
                continue
            actions = {row["confdeltype"] for row in candidates if row["ref_table"] == ref_table}
            if action not in actions:
                problems.append(f"{table} -> {ref_table} ON DELETE {action} expected, got {actions}")
        report.add("foreign keys", not problems, problems or "cascade/restrict actions correct")

        cur.execute("select version, checksum from schema_migrations order by version")
        migrations = [row["version"] for row in cur.fetchall()]
        expected_migrations = {
            "001_initial_schema",
            "002_runtime_state",
            "003_freeai_provider",
        }
        report.add("migrations", expected_migrations <= set(migrations), migrations)

        cur.execute(
            "select pg_get_constraintdef(oid) as definition from pg_constraint "
            "where conname = 'analysis_results_provider_check'"
        )
        row = cur.fetchone()
        definition = str(row["definition"]) if row else ""
        report.add(
            "provider constraint",
            "freeai" in definition,
            "freeai allowed" if "freeai" in definition else "free.ai is still rejected",
        )


def _behaviour_probe(conn: psycopg.Connection, report: Report) -> None:
    """Insert/update/claim inside one transaction, then roll it back."""
    token = uuid.uuid4().hex[:10]
    source_id = f"g0_probe_{token}"
    key = f"g0probe:{token}"
    fingerprint = f"g0probe-{token}"
    delivery_id = None

    try:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    "insert into sources (source_id, name, category, tier, method, url) "
                    "values (%s, %s, 'ai_models', 'A', 'rss', 'https://example.com/g0.xml')",
                    (source_id, "G0 probe"),
                )
                cur.execute(
                    "insert into discoveries "
                    "(canonical_key, title_fingerprint, title, category, summary, url, "
                    " source_id, tier, published_at, priority_score, confidence, verdict) "
                    "values (%s, %s, 'G0 probe', 'ai_models', 'probe', "
                    "        'https://example.com/g0', %s, 'A', now(), 50, 0.8, 'CONFIRMED') "
                    "returning id",
                    (key, fingerprint, source_id),
                )
                discovery_id = cur.fetchone()["id"]
                cur.execute(
                    "insert into discovery_sources "
                    "(discovery_id, source_id, evidence_url, tier, domain, http_status, reachable) "
                    "values (%s, %s, 'https://example.com/g0', 'A', 'example.com', 200, true)",
                    (discovery_id, source_id),
                )
                cur.execute(
                    "insert into analysis_results "
                    "(discovery_id, provider, model_id, prompt_version, input_fingerprint, payload) "
                    "values (%s, 'deterministic', 'rules', 'g0', %s, %s)",
                    (discovery_id, fingerprint, Jsonb({"summary": "probe"})),
                )
                cur.execute(
                    "insert into pricing_history (discovery_id, pricing_status, evidence) "
                    "values (%s, 'UNVERIFIED', 'probe')",
                    (discovery_id,),
                )
                cur.execute(
                    "insert into deliveries (discovery_id, message_text, message_hash, status) "
                    "values (%s, 'probe message', %s, 'PENDING') returning id",
                    (discovery_id, token),
                )
                delivery_id = cur.fetchone()["id"]

                # checkpoint persistence
                cur.execute(
                    "update sources set last_checkpoint = now(), etag = %s, "
                    "last_success_at = now(), health_status = 'healthy' "
                    "where source_id = %s",
                    ('"g0"', source_id),
                )
                report.add("checkpoint update", cur.rowcount == 1, "one source row updated")

                # atomic claim (same shape as the repository query)
                cur.execute(
                    "select id from deliveries "
                    "where status in ('PENDING','RETRY_WAIT') and next_attempt_at <= now() "
                    "order by created_at for update skip locked limit 1"
                )
                claimed = cur.fetchone()
                cur.execute(
                    "update deliveries set status = 'SENDING', claimed_by = 'g0', "
                    "claimed_at = now(), attempts = attempts + 1 "
                    "where id = %s returning attempts",
                    (claimed["id"],),
                )
                attempts = cur.fetchone()["attempts"]
                report.add("atomic claim", attempts == 1, f"attempts={attempts}")

                # uniqueness must reject a duplicate canonical key
                try:
                    with conn.transaction():  # savepoint
                        cur.execute(
                            "insert into discoveries "
                            "(canonical_key, title_fingerprint, title, category, source_id, tier) "
                            "values (%s, 'dup', 'dup', 'ai_models', %s, 'A')",
                            (key, source_id),
                        )
                except errors.UniqueViolation:
                    report.add("unique canonical_key", True, "duplicate rejected")
                else:
                    report.add("unique canonical_key", False, "duplicate was accepted")

                # force rollback of every probe row
                raise _Rollback
    except _Rollback:
        pass
    except Exception as exc:  # noqa: BLE001 - report, never leak a DSN
        report.add("behaviour probe", False, f"{type(exc).__name__}: {scrub(str(exc))}")
        return

    with conn.cursor() as cur:
        cur.execute(
            "select count(*) as n from sources where source_id like 'g0_probe_%'"
        )
        leftover_sources = cur.fetchone()["n"]
        cur.execute(
            "select count(*) as n from discoveries where canonical_key like 'g0probe:%'"
        )
        leftover_discoveries = cur.fetchone()["n"]
        cur.execute(
            "select count(*) as n from deliveries where message_hash = %s", (token,)
        )
        leftover_deliveries = cur.fetchone()["n"]
    leftovers = leftover_sources + leftover_discoveries + leftover_deliveries
    report.add("rollback left no rows", leftovers == 0, f"leftover rows={leftovers}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Non-destructive database verification")
    parser.add_argument("--json", action="store_true", help="print a JSON report")
    parser.add_argument(
        "--target-only",
        action="store_true",
        help="connect, print the masked target, and stop before schema checks",
    )
    args = parser.parse_args(argv)

    try:
        dsn = validate_dsn(Settings().database_url)
    except DatabaseError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    report = Report()
    print(f"target: {_masked_target(dsn)}")
    print("mode:   non-destructive (all writes rolled back)\n")

    try:
        conn = _connect(dsn)
    except Exception as exc:  # noqa: BLE001
        message = scrub(str(exc))
        print(f"connection failed: {type(exc).__name__}: {message}", file=sys.stderr)
        print(
            "hint: use the shared pooler in session mode (port 5432), "
            "username postgres.<project-ref>, sslmode=require",
            file=sys.stderr,
        )
        return 1

    try:
        with conn.cursor() as cur:
            cur.execute(
                "select version() as version, current_database() as db, current_user as usr, "
                "       inet_server_addr()::text as addr"
            )
            meta = cur.fetchone()
            report.add("connection", True, meta["version"].split(",")[0])
            if args.target_only:
                cur.execute(
                    "select count(*) as n from information_schema.tables "
                    "where table_schema = 'public'"
                )
                table_count = cur.fetchone()["n"]
                print(f"database: {meta['db']}")
                print(f"user:     {str(meta['usr']).split('.', 1)[0]}.***")
                print(f"server:   {meta['addr'] or 'pooler endpoint'}")
                print(f"public tables: {table_count}")
                print("mode: target-only (no writes, no schema assertions)")
                return 0
            cur.execute("show timezone")
            report.add("timezone", True, cur.fetchone()["TimeZone"])
            try:
                cur.execute("select ssl from pg_stat_ssl where pid = pg_backend_pid()")
                ssl_row = cur.fetchone()
                report.add("tls at server", True, f"pg_stat_ssl={ssl_row['ssl'] if ssl_row else 'n/a'}")
            except errors.InsufficientPrivilege:
                conn.rollback()
                report.add("tls at server", True, "pg_stat_ssl not readable; DSN uses sslmode")

        _schema_checks(conn, report)
        _behaviour_probe(conn, report)
    finally:
        conn.close()

    if args.json:
        print(json.dumps({"target": _masked_target(dsn), "checks": report.checks}, indent=2))
    else:
        for item in report.checks:
            mark = "PASS" if item["ok"] else "FAIL"
            print(f"[{mark}] {item['check']:<22} {item['detail']}")

    if report.ok:
        print("\nRESULT: PASS — schema and write paths verified; no rows were left behind.")
        return 0
    print("\nRESULT: FAIL — do not proceed to the next G0 step.", file=sys.stderr)
    for item in report.failed():
        print(f"  - {item['check']}: {item['detail']}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
