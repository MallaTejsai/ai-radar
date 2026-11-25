"""Read-only pre-flight for the first live staging run.

Proves, before any research happens, that:

* staging mode is on and the staging DSN is the only database that can be used;
* the staging endpoint differs from production (host+port+user);
* free.ai / Gemini / Groq / xAI are structurally absent, so Sarvam is the only
  AI provider in the chain;
* the alert cap is 1 and Telegram is available only inside this process;
* the staging database is reachable, migrations 001-003 are applied, and no
  queued delivery exists that could produce a second message.

It never writes to the database, never sends anything, and never prints a DSN,
password, token or chat id.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from ai_radar.cli import _run_async
from ai_radar.intelligence.router import IntelligenceEngine
from ai_radar.settings import load_config
from ai_radar.storage.db import (
    Database,
    assert_staging_target,
    dsn_fingerprint,
    masked_endpoint,
    validate_dsn,
)
from ai_radar.utils.net import SafeHttpClient

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts" / "staging_preflight.json"

failures: list[str] = []
state: dict[str, object] = {}


def check(name: str, ok: bool, detail: str) -> None:
    state[name] = {"ok": ok, "detail": detail}
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}")
    if not ok:
        failures.append(f"{name}: {detail}")


async def main() -> int:
    config = load_config()
    settings = config.settings

    check("staging mode", settings.ai_radar_staging, f"AI_RADAR_STAGING={settings.ai_radar_staging}")
    check(
        "telegram opt-in",
        settings.ai_radar_staging_allow_telegram,
        f"AI_RADAR_STAGING_ALLOW_TELEGRAM={settings.ai_radar_staging_allow_telegram}",
    )
    check("alert cap", settings.max_alerts_per_run == 1, f"max_alerts_per_run={settings.max_alerts_per_run}")

    staging_dsn = settings.ai_radar_staging_database_url
    check("staging DSN present", bool(staging_dsn), "AI_RADAR_STAGING_DATABASE_URL set" if staging_dsn else "missing")

    if not staging_dsn:
        return _finish()

    try:
        validate_dsn(staging_dsn, label="AI_RADAR_STAGING_DATABASE_URL")
        assert_staging_target(staging_dsn, staging=True, production_dsn=settings.database_url)
        check(
            "staging isolation",
            True,
            f"staging {masked_endpoint(staging_dsn)}; production "
            f"{masked_endpoint(settings.database_url)}; different endpoints, "
            f"production never connected",
        )
    except Exception as exc:  # noqa: BLE001
        check("staging isolation", False, type(exc).__name__)
        return _finish()

    state["dsn_fingerprint"] = dsn_fingerprint(staging_dsn)
    check("staging fingerprint", True, dsn_fingerprint(staging_dsn))

    for name, value in (
        ("free.ai key", settings.freeai_api_key),
        ("Gemini key", settings.gemini_api_key),
        ("Groq key", settings.groq_api_key),
        ("xAI key", settings.xai_api_key),
    ):
        check(f"{name} absent", not value, "empty" if not value else "PRESENT")

    check("Sarvam key present", bool(settings.sarvam_api_key), "set" if settings.sarvam_api_key else "missing")
    check("Sarvam model", settings.sarvam_model == "sarvam-105b", settings.sarvam_model)
    check("Sarvam call cap", settings.sarvam_max_calls_per_run == 1, str(settings.sarvam_max_calls_per_run))

    http = SafeHttpClient(
        max_bytes=settings.http_max_bytes,
        timeout=settings.http_timeout_seconds,
        retries=settings.http_retries,
    )
    try:
        engine = IntelligenceEngine(
            settings=settings,
            signals=config.scoring.signals,
            client=http,
        )
        chain = [provider.name.value for provider in engine._providers]  # noqa: SLF001 - preflight assertion
        check("provider chain", chain == ["sarvam"], f"active providers: {chain}")
        if chain == ["sarvam"]:
            resolved = await engine._providers[0].resolve_model()  # noqa: SLF001
            check("resolved model", resolved == "sarvam-105b", resolved)
    finally:
        await http.aclose()

    check("telegram token", bool(settings.telegram_bot_token), "present" if settings.telegram_bot_token else "missing")
    check("telegram chat id", bool(settings.telegram_chat_id), "present" if settings.telegram_chat_id else "missing")

    # ---- read-only database state -----------------------------------------
    db = Database(staging_dsn)
    try:
        await db.connect()
        async with db.transaction() as cursor:
            await cursor.execute("select version, checksum, applied_at from schema_migrations order by version")
            migrations = await cursor.fetchall()
            await cursor.execute("select count(*) as n from discoveries")
            discoveries = int((await cursor.fetchone())["n"])
            await cursor.execute("select status, count(*) as n from deliveries group by status")
            deliveries = {row["status"]: int(row["n"]) for row in await cursor.fetchall()}
            await cursor.execute(
                "select coalesce(sum(coalesce(prompt_tokens,0)+coalesce(completion_tokens,0)),0) as n"
                " from provider_usage where provider = 'sarvam'"
                " and occurred_at > now() - interval '24 hours'"
            )
            sarvam_tokens = int((await cursor.fetchone())["n"])
            await cursor.execute(
                "select run_id, status, started_at from workflow_runs order by started_at desc limit 1"
            )
            last_run = await cursor.fetchone()

        state["migrations"] = [row["version"] for row in migrations]
        state["discoveries"] = discoveries
        state["deliveries"] = deliveries
        state["sarvam_tokens_24h"] = sarvam_tokens
        state["last_run"] = (
            {
                "run_id": last_run["run_id"],
                "status": last_run["status"],
                "started_at": last_run["started_at"].isoformat(),
            }
            if last_run
            else None
        )

        applied = [row["version"] for row in migrations]
        check(
            "migrations 001-003",
            all(f"00{i}_" in version for i, version in enumerate(applied[:3], start=1)),
            f"applied: {applied}",
        )
        queued = deliveries.get("PENDING", 0) + deliveries.get("SENDING", 0) + deliveries.get("RETRY_WAIT", 0)
        check("no queued deliveries", queued == 0, f"queued={queued}, by status={deliveries}")
        check(
            "Sarvam budget headroom",
            sarvam_tokens < 3000,
            f"{sarvam_tokens} tokens used in the last 24h (daily budget {settings.sarvam_daily_token_budget})",
        )
        print(f"[INFO] discoveries already stored in staging: {discoveries}")
        if last_run:
            print(
                f"[INFO] last staging run: {last_run['run_id']} {last_run['status']} "
                f"at {last_run['started_at'].isoformat()}"
            )
    except Exception as exc:  # noqa: BLE001
        check("staging database", False, type(exc).__name__)
    finally:
        await db.close()

    return _finish()


def _finish() -> int:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(
        json.dumps(
            {
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "ok": not failures,
                "failures": failures,
                "checks": state,
            },
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )
    print(f"\nPreflight result: {'PASS' if not failures else 'FAIL'}")
    if failures:
        for item in failures:
            print(f"  - {item}")
    return 0 if not failures else 1


if __name__ == "__main__":
    # Selector loop on Windows: psycopg's async driver refuses ProactorEventLoop.
    sys.exit(_run_async(main()))
