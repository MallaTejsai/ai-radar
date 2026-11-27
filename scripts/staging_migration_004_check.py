"""Read-only inspection around migration 004 (provider CHECK constraint).

Usage::

    python scripts/staging_migration_004_check.py pre
    python scripts/staging_migration_004_check.py post

``pre`` proves the guards (staging mode, dedicated DSN, different endpoint from
production) and records the exact database state before applying anything: the
applied migration versions, the current CHECK constraint definition, and
row/provider counts for the research tables.

``post`` proves that only 004 was added, that its recorded checksum matches the
repository file, that the CHECK constraint allows all six providers, and that no
research row changed.

Both modes are strictly read-only. They never apply a migration, never call a
provider, never send anything, and never print a DSN, password or token.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from ai_radar.cli import _run_async, _staging_settings
from ai_radar.settings import load_config
from ai_radar.storage.db import (
    Database,
    endpoint_signature,
    masked_endpoint,
)
from ai_radar.storage.migrations import checksum, discover_migrations

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "database" / "migrations" / "004_provider_check.sql"
PRE = ROOT / "artifacts" / "staging_migration_004_pre.json"
POST = ROOT / "artifacts" / "staging_migration_004_post.json"

EXPECTED_PROVIDERS = ("freeai", "gemini", "groq", "grok", "sarvam", "deterministic")
COUNT_TABLES = ("discoveries", "analysis_results", "deliveries", "workflow_runs", "provider_usage")

failures: list[str] = []


def check(name: str, ok: bool, detail: str) -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}")
    if not ok:
        failures.append(f"{name}: {detail}")


async def snapshot(db: Database) -> dict[str, object]:
    """Read the state that migration 004 must not disturb."""
    async with db.transaction() as cursor:
        await cursor.execute("select version, checksum from schema_migrations order by version")
        migrations = [
            {"version": row["version"], "checksum": row["checksum"]}
            for row in await cursor.fetchall()
        ]

        counts: dict[str, int | None] = {}
        for table in COUNT_TABLES:
            await cursor.execute("select to_regclass(%s) as reg", (f"public.{table}",))
            row = await cursor.fetchone()
            if not row or row["reg"] is None:
                counts[table] = None
                continue
            await cursor.execute(f"select count(*) as n from {table}")  # noqa: S608 - fixed internal names
            counts[table] = int((await cursor.fetchone())["n"])

        providers: dict[str, int] | None = None
        await cursor.execute("select to_regclass('public.analysis_results') as reg")
        if (await cursor.fetchone())["reg"] is not None:
            await cursor.execute(
                "select provider, count(*) as n from analysis_results group by provider order by provider"
            )
            providers = {row["provider"]: int(row["n"]) for row in await cursor.fetchall()}

        await cursor.execute(
            """
            select pg_get_constraintdef(c.oid) as definition
              from pg_constraint c
              join pg_class t on t.oid = c.conrelid
             where t.relname = 'analysis_results'
               and c.conname = 'analysis_results_provider_check'
            """
        )
        row = await cursor.fetchone()
        constraint = row["definition"] if row else None

    return {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "migrations": migrations,
        "counts": counts,
        "providers": providers,
        "constraint": constraint,
    }


def constraint_providers(definition: str | None) -> list[str]:
    if not definition:
        return []
    return [name for name in EXPECTED_PROVIDERS if f"'{name}'" in definition]


async def run(mode: str) -> int:
    config = load_config()
    settings = config.settings

    check("staging mode", settings.ai_radar_staging, f"AI_RADAR_STAGING={settings.ai_radar_staging}")
    if not settings.ai_radar_staging:
        return finish(mode, {})

    # Effective settings: validates the dedicated DSN and refuses the production
    # endpoint before a connection is ever opened.
    try:
        effective = _staging_settings(config)
    except Exception as exc:  # noqa: BLE001
        check("staging guard", False, f"{type(exc).__name__}: {exc}")
        return finish(mode, {})

    staging_sig = endpoint_signature(effective.database_url)
    production_dsn = settings.database_url
    production_sig = endpoint_signature(production_dsn) if production_dsn else None

    check(
        "staging endpoint",
        True,
        f"{masked_endpoint(effective.database_url)} "
        f"(host={staging_sig[0]} port={staging_sig[1]} user={staging_sig[2]})",
    )
    check(
        "production endpoint (parsed only, never connected)",
        True,
        f"{masked_endpoint(production_dsn)} "
        f"(host={production_sig[0]} port={production_sig[1]} user={production_sig[2]})"
        if production_sig
        else "no DATABASE_URL configured",
    )
    check(
        "staging != production (host/port/user)",
        bool(production_sig) and staging_sig != production_sig,
        "different server and role"
        if production_sig and staging_sig != production_sig
        else "production signature unavailable or identical",
    )

    # Structural proof that this process cannot analyse or deliver anything.
    for name, value in (
        ("free.ai key", settings.freeai_api_key),
        ("Gemini key", settings.gemini_api_key),
        ("Groq key", settings.groq_api_key),
        ("xAI key", settings.xai_api_key),
        ("Sarvam key", settings.sarvam_api_key),
    ):
        check(f"{name} absent", not value, "empty" if not value else "PRESENT")
    check(
        "telegram opt-in off",
        not settings.ai_radar_staging_allow_telegram,
        f"AI_RADAR_STAGING_ALLOW_TELEGRAM={settings.ai_radar_staging_allow_telegram}",
    )

    db = Database(effective.database_url)
    try:
        await db.connect()
        state = await snapshot(db)
    finally:
        await db.close()

    applied = [row["version"] for row in state["migrations"]]  # type: ignore[index]
    pending = [path.stem for path in discover_migrations() if path.stem not in applied]
    state["pending"] = pending
    state["file_checksum"] = checksum(MIGRATION)

    check("database connected", True, "pool opened and closed; read-only session")
    check("applied migrations", bool(applied), ", ".join(applied) or "none")
    check(
        "pending migrations",
        pending == ["004_provider_check"],
        ", ".join(pending) or "none",
    )

    present = constraint_providers(state["constraint"])  # type: ignore[arg-type]
    missing = [name for name in EXPECTED_PROVIDERS if name not in present]
    check(
        "provider constraint",
        True,
        f"definition: {state['constraint']}",
    )
    print(f"[INFO] providers present before: {present or 'none'}")
    print(f"[INFO] providers missing before: {missing or 'none'}")
    print(f"[INFO] 004 file sha256: {state['file_checksum']}")

    if mode == "pre":
        check(
            "004 not yet applied",
            "004_provider_check" not in applied,
            "pending" if "004_provider_check" not in applied else "already applied",
        )
    return finish(mode, state)


def verify_post(state: dict[str, object]) -> None:
    pre = json.loads(PRE.read_text(encoding="utf-8"))
    applied = [row["version"] for row in state["migrations"]]  # type: ignore[index]
    pre_applied = [row["version"] for row in pre["migrations"]]

    check(
        "migrations 001-004 recorded",
        all(any(version.startswith(prefix) for version in applied) for prefix in ("001_", "002_", "003_", "004_")),
        ", ".join(applied),
    )
    added = [version for version in applied if version not in pre_applied]
    check("only 004 applied", added == ["004_provider_check"], ", ".join(added) or "none")
    check(
        "no extra records",
        sorted(applied) == sorted(["001_initial_schema", "002_runtime_state", "003_freeai_provider", "004_provider_check"]),
        f"{len(applied)} versions",
    )

    recorded = next(
        (row["checksum"] for row in state["migrations"] if row["version"] == "004_provider_check"),  # type: ignore[index]
        None,
    )
    check(
        "004 checksum recorded",
        recorded == state["file_checksum"],
        f"recorded {str(recorded)[:16]}... == file {str(state['file_checksum'])[:16]}...",
    )

    present = constraint_providers(state["constraint"])  # type: ignore[arg-type]
    missing = [name for name in EXPECTED_PROVIDERS if name not in present]
    check("constraint allows all six providers", not missing, f"missing: {missing or 'none'}")
    check(
        "constraint definition",
        "CHECK" in (state["constraint"] or ""),
        str(state["constraint"]),
    )

    check(
        "research rows unchanged",
        state["counts"] == pre["counts"],
        f"before {pre['counts']} / after {state['counts']}",
    )
    check(
        "analysis providers unchanged",
        state["providers"] == pre["providers"],
        f"before {pre['providers']} / after {state['providers']}",
    )


def finish(mode: str, state: dict[str, object]) -> int:
    target = POST if mode == "post" else PRE
    failure_path = ROOT / "artifacts" / f"staging_migration_004_{mode}_failures.json"
    payload = {
        "mode": mode,
        "ok": not failures,
        "failures": failures,
        "state": state,
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    if failures:
        failure_path.write_text(json.dumps(failures, indent=2), encoding="utf-8")
    print(f"\n{mode.upper()} result: {'PASS' if not failures else 'FAIL'} ({target.name})")
    for item in failures:
        print(f"  - {item}")
    return 0 if not failures else 1


async def main_async(mode: str) -> int:
    if mode == "pre":
        return await run(mode)
    config = load_config()
    settings = config.settings
    check("staging mode", settings.ai_radar_staging, f"AI_RADAR_STAGING={settings.ai_radar_staging}")
    if not settings.ai_radar_staging or not PRE.exists():
        return finish(mode, {})
    try:
        effective = _staging_settings(config)
    except Exception as exc:  # noqa: BLE001
        check("staging guard", False, f"{type(exc).__name__}: {exc}")
        return finish(mode, {})

    staging_sig = endpoint_signature(effective.database_url)
    production_dsn = settings.database_url
    production_sig = endpoint_signature(production_dsn) if production_dsn else None
    check(
        "staging != production (host/port/user)",
        bool(production_sig) and staging_sig != production_sig,
        f"staging user={staging_sig[2]} host={staging_sig[0]}:{staging_sig[1]} | "
        f"production user={production_sig[2] if production_sig else 'n/a'} "
        f"host={production_sig[0] if production_sig else 'n/a'}:{production_sig[1] if production_sig else 'n/a'}",
    )

    db = Database(effective.database_url)
    try:
        await db.connect()
        state = await snapshot(db)
    finally:
        await db.close()
    state["pending"] = [path.stem for path in discover_migrations()
                        if path.stem not in [row["version"] for row in state["migrations"]]]  # type: ignore[index]
    state["file_checksum"] = checksum(MIGRATION)
    verify_post(state)
    return finish(mode, state)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("pre", "post"))
    args = parser.parse_args()
    # Selector loop on Windows: psycopg's async driver refuses ProactorEventLoop.
    sys.exit(_run_async(main_async(args.mode)))
