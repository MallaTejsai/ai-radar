"""Read-only verification of the first live staging run.

Connects with AI_RADAR_STAGING_DATABASE_URL only, re-applies the staging guard,
and reports what the run actually persisted. No writes, no Telegram calls.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from ai_radar.cli import _run_async
from ai_radar.settings import load_config
from ai_radar.storage.db import Database, assert_staging_target

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts" / "staging_verify.json"
PRE = ROOT / "artifacts" / "staging_preflight.json"


async def main() -> int:
    config = load_config()
    settings = config.settings
    dsn = settings.ai_radar_staging_database_url
    if not settings.ai_radar_staging or not dsn:
        print("REFUSED: not in staging mode with a staging DSN")
        return 2
    assert_staging_target(dsn, staging=True, production_dsn=settings.database_url)

    pre = json.loads(PRE.read_text(encoding="utf-8")) if PRE.exists() else {}
    pre_count = int(pre.get("checks", {}).get("discoveries", 0)) if pre else 0

    db = Database(dsn)
    result: dict[str, object] = {}
    try:
        await db.connect()
        async with db.transaction() as cursor:
            await cursor.execute(
                "select * from workflow_runs order by started_at desc limit 1"
            )
            run = await cursor.fetchone()
            if run is None:
                print("REFUSED: no workflow_runs row in staging")
                return 2
            started = run["started_at"]
            result["run"] = {
                "run_id": run["run_id"],
                "status": run["status"],
                "trigger": run["trigger"],
                "started_at": started.isoformat(),
                "finished_at": run["finished_at"].isoformat() if run["finished_at"] else None,
                "duration_seconds": run["duration_seconds"],
                "report": run["report"],
            }

            await cursor.execute(
                "select count(*) as n from discoveries where discovered_at >= %s", (started,)
            )
            new_discoveries = int((await cursor.fetchone())["n"])
            await cursor.execute("select count(*) as n from discoveries")
            total_discoveries = int((await cursor.fetchone())["n"])
            result["discoveries"] = {
                "new_this_run": new_discoveries,
                "total_staging": total_discoveries,
                "delta_vs_preflight": total_discoveries - pre_count,
            }

            await cursor.execute(
                """
                select d.canonical_key, d.title, d.category, d.priority_score, d.priority_label,
                       d.confidence, d.verdict, d.status, d.pricing_status, d.url,
                       d.discovered_at, a.provider, a.model_id, a.latency_ms, a.cached,
                       a.fallback_reason, a.prompt_version
                  from discoveries d
                  left join analysis_results a on a.discovery_id = d.id
                 where d.discovered_at >= %s
                 order by d.discovered_at
                """,
                (started,),
            )
            result["rows"] = [
                {
                    **{k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in row.items()},
                }
                for row in await cursor.fetchall()
            ]

            await cursor.execute(
                """
                select provider, count(*) as n,
                       count(*) filter (where ok) as ok_count,
                       count(*) filter (where not ok) as fail_count,
                       coalesce(sum(coalesce(prompt_tokens,0)+coalesce(completion_tokens,0)),0) as tokens
                  from provider_usage
                 where occurred_at >= %s
                 group by provider
                 order by provider
                """,
                (started,),
            )
            result["provider_usage"] = [
                {k: (int(v) if k != "provider" else v) for k, v in row.items()}
                for row in await cursor.fetchall()
            ]

            await cursor.execute(
                """
                select provider, model_id, ok, error_kind, latency_ms,
                       prompt_tokens, completion_tokens, occurred_at
                  from provider_usage
                 where occurred_at >= %s
                 order by occurred_at
                """,
                (started,),
            )
            result["provider_calls"] = [
                {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in row.items()}
                for row in await cursor.fetchall()
            ]

            await cursor.execute(
                """
                select dl.id, dl.status, dl.attempts, dl.telegram_message_id,
                       dl.last_error, dl.created_at, dl.updated_at,
                       d.canonical_key, d.title
                  from deliveries dl
                  join discoveries d on d.id = dl.discovery_id
                 where dl.created_at >= %s
                 order by dl.created_at
                """,
                (started,),
            )
            result["deliveries"] = [
                {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in row.items()}
                for row in await cursor.fetchall()
            ]

            await cursor.execute("select status, count(*) as n from deliveries group by status")
            result["delivery_totals"] = {r["status"]: int(r["n"]) for r in await cursor.fetchall()}

            await cursor.execute(
                """
                select count(*) as n,
                       coalesce(avg(confidence),0) as avg_conf,
                       coalesce(min(confidence),0) as min_conf,
                       coalesce(max(confidence),0) as max_conf,
                       coalesce(avg(priority_score),0) as avg_priority
                  from discoveries where discovered_at >= %s
                """,
                (started,),
            )
            dist = await cursor.fetchone()
            result["distribution"] = {k: float(v) if k != "n" else int(v) for k, v in dist.items()}

            await cursor.execute(
                "select priority_label, count(*) as n from discoveries"
                " where discovered_at >= %s group by priority_label order by priority_label",
                (started,),
            )
            result["priority_labels"] = {r["priority_label"]: int(r["n"]) for r in await cursor.fetchall()}

            await cursor.execute(
                "select version from schema_migrations order by version"
            )
            result["migrations"] = [r["version"] for r in await cursor.fetchall()]
    finally:
        await db.close()

    OUT.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")

    run = result["run"]
    report = run["report"] or {}
    print(json.dumps(
        {
            "run_id": run["run_id"],
            "status": run["status"],
            "trigger": run["trigger"],
            "started_at": run["started_at"],
            "finished_at": run["finished_at"],
            "sources_attempted": report.get("sources_attempted"),
            "sources_failed": report.get("sources_failed"),
            "entries_collected": report.get("entries_collected"),
            "after_dedup": report.get("after_dedup"),
            "verified": report.get("verified"),
            "discarded_unverified": report.get("discarded_unverified"),
            "discarded_low_confidence": report.get("discarded_low_confidence"),
            "qualified": report.get("qualified"),
            "persisted_new": report.get("persisted_new"),
            "persisted_updated": report.get("persisted_updated"),
            "alerts_sent": report.get("alerts_sent"),
            "ai_provider_counts": report.get("ai_provider_counts"),
            "ai_errors": report.get("ai_errors"),
            "errors": report.get("errors"),
            "discoveries_new": result["discoveries"]["new_this_run"],
            "provider_usage": result["provider_usage"],
            "deliveries": result["deliveries"],
            "delivery_totals": result["delivery_totals"],
            "migrations": result["migrations"],
        },
        indent=2,
    ))
    return 0


if __name__ == "__main__":
    # Selector loop on Windows: psycopg's async driver refuses ProactorEventLoop.
    sys.exit(_run_async(main()))
