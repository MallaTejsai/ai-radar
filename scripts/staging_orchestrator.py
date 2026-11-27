"""Single-process staging orchestration driver.

Invoked by ``scripts/staging_orchestrate.ps1``, which is the only component that
prompts for the staging DSN. The DSN arrives through
``AI_RADAR_STAGING_DATABASE_URL`` in this process's environment and is never
printed, logged, written to disk or placed on a command line. Every stage runs
in this one process, so no child process is started and no second prompt can
appear.

Stages, in order:

1. ``doctor --database-only``   in-process; its checks are captured for the gate
2. staging isolation            validate + endpoint signature vs production
3. pending migration gate       read-only ledger read, approved set only
4. apply approved migrations    existing runner; refuses an unexpected pending set
5. migration verification       read-only: ledger, checksums, CHECK constraint, row counts
6. one research cycle           ONLY with ``--authorize-research``; never otherwise
7. read-only verification       counts, providers, deliveries, latest run

Nothing here can reach production: staging mode makes ``DATABASE_URL`` unreadable
to the application, and every stage re-validates the staging endpoint against
the production signature before a connection is opened.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from ai_radar import cli
from ai_radar.settings import AppConfig, load_config
from ai_radar.storage import Database, DatabaseError
from ai_radar.storage.db import (
    assert_staging_target,
    dsn_fingerprint,
    endpoint_signature,
    masked_endpoint,
    validate_dsn,
)
from ai_radar.storage.migrations import checksum, discover_migrations

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / "artifacts"
REPORT = ARTIFACTS / "staging_orchestration_report.json"
RESEARCH_MARKER = ARTIFACTS / "staging_orchestration_research.json"

#: The only migrations this orchestrator may apply. Anything else pending aborts.
APPROVED_MIGRATIONS = ("004_provider_check",)
EXPECTED_PROVIDERS = ("freeai", "gemini", "groq", "grok", "sarvam", "deterministic")
COUNT_TABLES = ("discoveries", "analysis_results", "deliveries", "workflow_runs", "provider_usage")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _emit(mark: str, name: str, detail: str) -> None:
    print(f"[{mark}] {name}: {detail}")


class Abort(RuntimeError):
    """A safety gate refused to continue. Nothing further is executed."""


def _constraint_providers(definition: str | None) -> list[str]:
    if not definition:
        return []
    return [name for name in EXPECTED_PROVIDERS if f"'{name}'" in definition]


def _run_async(coro):
    """Run a coroutine on a loop psycopg accepts.

    Windows defaults to ProactorEventLoop, which psycopg's async driver refuses.
    Same policy the CLI applies (``ai_radar.cli._run_async``).
    """
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    return asyncio.run(coro)


class Orchestrator:
    def __init__(self, config: AppConfig, *, authorize_research: bool) -> None:
        self.config = config
        self.settings = config.settings
        self.authorize_research = authorize_research
        self.doctor_checks: list[dict[str, Any]] = []
        self.doctor_exit: int | None = None
        self.pre: dict[str, Any] | None = None
        self.post: dict[str, Any] | None = None
        self.after_research: dict[str, Any] | None = None
        self.applied_now: list[str] = []
        self.research_exit: int | None = None
        self.report: dict[str, Any] = {
            "started_at": _now(),
            "mode": "authorized-research" if authorize_research else "migrations-and-verification",
            "authorize_research": authorize_research,
            "staging": {},
            "doctor": {},
            "pre": None,
            "post": None,
            "after_research": None,
            "applied_this_run": [],
            "research": {"requested": authorize_research, "executed": False},
            "ok": False,
            "failures": [],
            "finished_at": None,
        }

    # ------------------------------------------------------------------ guard
    def guard(self, stage: str) -> str:
        """Validate the staging endpoint against production before connecting.

        Called at the start of every stage that can open a connection. Reuses the
        existing guards (``validate_dsn`` / ``assert_staging_target``); this adds
        the literal-string check and the masked report line.
        """
        settings = self.settings
        if not settings.ai_radar_staging:
            raise Abort(
                "AI_RADAR_STAGING is not 1: this orchestrator never runs against production"
            )

        staging_dsn = settings.ai_radar_staging_database_url
        production_dsn = settings.database_url
        try:
            validate_dsn(staging_dsn, label="AI_RADAR_STAGING_DATABASE_URL")
            assert_staging_target(
                staging_dsn, staging=True, production_dsn=production_dsn
            )
        except DatabaseError as exc:
            raise Abort(f"staging guard refused before stage '{stage}': {exc}") from exc

        staging_sig = endpoint_signature(staging_dsn)
        production_sig = endpoint_signature(production_dsn) if production_dsn else None
        if production_dsn and staging_dsn.strip() == production_dsn.strip():
            raise Abort("the staging DSN is identical to DATABASE_URL; refusing to connect")
        if production_sig and staging_sig == production_sig:
            raise Abort(
                f"stage '{stage}': the staging endpoint matches the production signature "
                "(host/port/user); refusing to connect"
            )

        _emit(
            "PASS",
            f"guard · {stage}",
            f"staging {masked_endpoint(staging_dsn)} | production "
            f"{masked_endpoint(production_dsn) if production_dsn else 'not set'} | "
            "different endpoints, production never connected",
        )
        self.report["staging"] = {
            "staging": masked_endpoint(staging_dsn),
            "staging_signature": list(staging_sig),
            "production": masked_endpoint(production_dsn) if production_dsn else None,
            "production_signature": list(production_sig) if production_sig else None,
            "fingerprint": dsn_fingerprint(staging_dsn),
        }
        return staging_dsn

    # ------------------------------------------------------------- stage 1
    async def stage_doctor(self) -> None:
        """``doctor --database-only`` in-process, with its checks captured.

        Capturing the structured checks (instead of parsing console text) is what
        lets stage 3 accept exactly one specific failure - a migration that is
        pending *and* approved - and refuse every other blocking problem.
        """
        self.guard("doctor")
        captured: list[dict[str, Any]] = []
        original = cli._render_doctor_checks  # noqa: SLF001 - in-process capture

        def capture(checks: list[dict[str, Any]]) -> int:
            captured.extend(checks)
            return original(checks)

        cli._render_doctor_checks = capture  # type: ignore[assignment]  # noqa: SLF001
        try:
            self.doctor_exit = await cli.cmd_doctor(
                self.config, SimpleNamespace(database_only=True)
            )
        finally:
            cli._render_doctor_checks = original  # type: ignore[assignment]  # noqa: SLF001

        self.doctor_checks = captured
        self.report["doctor"] = {"exit_code": self.doctor_exit, "checks": captured}
        _emit(
            "INFO",
            "doctor",
            f"exit {self.doctor_exit}; {len(captured)} checks captured for the migration gate",
        )

    # ------------------------------------------------------------- stage 2/5/7
    async def read_state(self, stage: str) -> dict[str, Any]:
        """Open one read-only session and record the state a change must not disturb."""
        dsn = self.guard(stage)
        db = Database(dsn)
        try:
            await db.connect()
            async with db.transaction() as cursor:
                await cursor.execute(
                    "select version, checksum from schema_migrations order by version"
                )
                migrations = [
                    {"version": row["version"], "checksum": row["checksum"]}
                    for row in await cursor.fetchall()
                ]

                counts: dict[str, int | None] = {}
                for table in COUNT_TABLES:
                    await cursor.execute(
                        "select to_regclass(%s) as reg", (f"public.{table}",)
                    )
                    row = await cursor.fetchone()
                    if row is None or row["reg"] is None:
                        counts[table] = None
                        continue
                    await cursor.execute(f"select count(*) as n from {table}")  # noqa: S608 - fixed internal names
                    counts[table] = int((await cursor.fetchone())["n"])

                providers: dict[str, int] | None = None
                await cursor.execute("select to_regclass('public.analysis_results') as reg")
                row = await cursor.fetchone()
                if row is not None and row["reg"] is not None:
                    await cursor.execute(
                        "select provider, count(*) as n from analysis_results "
                        "group by provider order by provider"
                    )
                    providers = {
                        item["provider"]: int(item["n"]) for item in await cursor.fetchall()
                    }

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

                deliveries: dict[str, int] | None = None
                await cursor.execute("select to_regclass('public.deliveries') as reg")
                row = await cursor.fetchone()
                if row is not None and row["reg"] is not None:
                    await cursor.execute(
                        "select status, count(*) as n from deliveries group by status"
                    )
                    deliveries = {
                        item["status"]: int(item["n"]) for item in await cursor.fetchall()
                    }

                last_run: dict[str, Any] | None = None
                await cursor.execute("select to_regclass('public.workflow_runs') as reg")
                row = await cursor.fetchone()
                if row is not None and row["reg"] is not None:
                    await cursor.execute(
                        "select run_id, status, started_at from workflow_runs "
                        "order by started_at desc limit 1"
                    )
                    run = await cursor.fetchone()
                    if run is not None:
                        last_run = {
                            "run_id": run["run_id"],
                            "status": run["status"],
                            "started_at": run["started_at"].isoformat(),
                        }
        finally:
            await db.close()

        applied = [item["version"] for item in migrations]
        pending = [path.stem for path in discover_migrations() if path.stem not in applied]
        _emit(
            "PASS",
            f"database · {stage}",
            f"read-only session opened and closed; {len(applied)} migrations recorded, "
            f"pending {', '.join(pending) or 'none'}",
        )
        return {
            "checked_at": _now(),
            "migrations": migrations,
            "applied": applied,
            "pending": pending,
            "counts": counts,
            "providers": providers,
            "deliveries": deliveries,
            "constraint": constraint,
            "last_run": last_run,
        }

    # ------------------------------------------------------------- stage 3
    def gate(self) -> None:
        """Decide whether the pending set may be applied. Runs before any write."""
        state = self.pre
        assert state is not None
        pending: list[str] = state["pending"]
        unapproved = [name for name in pending if name not in APPROVED_MIGRATIONS]
        if unapproved:
            raise Abort(
                f"pending migrations outside the approved set {APPROVED_MIGRATIONS}: "
                f"{', '.join(unapproved)}"
            )

        if self.doctor_exit != cli.EXIT_OK:
            blocking = [
                check
                for check in self.doctor_checks
                if not check["ok"] and check.get("blocking", True)
            ]
            only_pending_migration = (
                len(blocking) == 1
                and blocking[0]["check"] == "migrations"
                and bool(pending)
            )
            if not only_pending_migration:
                raise Abort(
                    "doctor --database-only failed for reasons other than the approved "
                    "pending migration: "
                    + "; ".join(f"{c['check']}: {c['detail']}" for c in blocking)
                )
            _emit(
                "PASS",
                "doctor · expected failure",
                f"exit {self.doctor_exit}: only 'migrations' is blocking, and "
                f"{', '.join(pending)} is approved and pending",
            )
        elif pending:
            raise Abort(
                "doctor reports no pending migration but the ledger lists "
                f"{', '.join(pending)}; refusing to guess"
            )

    # ------------------------------------------------------------- stage 4
    async def stage_migrate(self) -> None:
        assert self.pre is not None
        pending: list[str] = self.pre["pending"]
        if not pending:
            _emit("PASS", "migration", "nothing to apply; ledger is current")
            return
        self.guard("migrate")
        _emit("INFO", "migration", f"applying approved set via the existing runner: {', '.join(pending)}")
        code = await cli.cmd_migrate(self.config, SimpleNamespace())
        if code != cli.EXIT_OK:
            raise Abort(f"migrate exited {code}; see artifacts/staging_orchestration_output.txt")
        self.applied_now = list(pending)

    # ------------------------------------------------------------- stage 5
    def verify_migration(self) -> None:
        assert self.pre is not None and self.post is not None
        pre, post = self.pre, self.post
        added = [version for version in post["applied"] if version not in pre["applied"]]
        if added != self.applied_now:
            raise Abort(
                f"ledger changed by {added or 'nothing'}, expected {self.applied_now or 'nothing'}"
            )

        for version in added:
            file_checksum = checksum(ROOT / "database" / "migrations" / f"{version}.sql")
            recorded = next(
                (item["checksum"] for item in post["migrations"] if item["version"] == version),
                None,
            )
            if recorded != file_checksum:
                raise Abort(
                    f"{version} recorded checksum {str(recorded)[:16]}... does not match "
                    f"the repository file {file_checksum[:16]}..."
                )
            _emit("PASS", f"{version} checksum", f"{file_checksum} recorded as applied")

        missing = [
            name
            for name in EXPECTED_PROVIDERS
            if name not in _constraint_providers(post["constraint"])
        ]
        if missing:
            raise Abort(f"provider CHECK constraint is missing {missing}")

        if post["counts"] != pre["counts"]:
            raise Abort(
                "research rows changed during migration-only steps: "
                f"before {pre['counts']} after {post['counts']}"
            )
        if post["providers"] != pre["providers"]:
            raise Abort(
                f"analysis provider distribution changed: {pre['providers']} -> {post['providers']}"
            )
        if post["deliveries"] != pre["deliveries"]:
            raise Abort(f"delivery state changed: {pre['deliveries']} -> {post['deliveries']}")

        _emit("PASS", "migrations recorded", ", ".join(post["applied"]))
        _emit("PASS", "provider constraint", str(post["constraint"]))
        _emit(
            "PASS",
            "research data untouched",
            f"counts {post['counts']}; providers {post['providers']}; deliveries {post['deliveries']}",
        )

    # ------------------------------------------------------------- stage 6
    async def stage_research(self) -> None:
        if not self.authorize_research:
            _emit(
                "SKIP",
                "research",
                "not authorized (pass -AuthorizeResearch to the PowerShell orchestrator); "
                "no research ran, no provider was called, no message was sent",
            )
            return

        if not self.settings.ai_radar_staging_allow_telegram:
            raise Abort(
                "research was authorized but AI_RADAR_STAGING_ALLOW_TELEGRAM is not 1; "
                "refusing to start a paid cycle whose delivery gate is inconsistent"
            )
        if RESEARCH_MARKER.exists():
            raise Abort(
                "a controlled staging research cycle was already attempted "
                f"({RESEARCH_MARKER.relative_to(ROOT)}); refusing a second one"
            )

        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        started = _now()
        RESEARCH_MARKER.write_text(
            json.dumps({"started_at": started, "status": "running"}, indent=2),
            encoding="utf-8",
        )
        self.report["research"]["executed"] = True
        self.report["research"]["started_at"] = started

        # Exactly one invocation, in this process, no retry.
        self.research_exit = await cli.cmd_research(
            self.config, SimpleNamespace(trigger="manual", dry_run=False)
        )

        RESEARCH_MARKER.write_text(
            json.dumps(
                {"started_at": started, "finished_at": _now(), "exit_code": self.research_exit},
                indent=2,
            ),
            encoding="utf-8",
        )
        self.report["research"]["exit_code"] = self.research_exit
        if self.research_exit != cli.EXIT_OK:
            _emit(
                "FAIL",
                "research",
                f"exited {self.research_exit}; read-only verification follows, nothing is retried",
            )

    # ------------------------------------------------------------- stage 7
    def verify_after_research(self) -> None:
        assert self.pre is not None and self.post is not None and self.after_research is not None
        after = self.after_research
        if after["applied"] != self.post["applied"]:
            raise Abort(f"migrations changed during research: {after['applied']}")
        before = self.post["counts"].get("discoveries")
        now = after["counts"].get("discoveries")
        _emit(
            "PASS",
            "read-only verification",
            f"discoveries {before} -> {now}; providers {after['providers']}; "
            f"deliveries {after['deliveries']}; last run "
            f"{after['last_run']['run_id'] + ' ' + after['last_run']['status'] if after['last_run'] else 'none'}",
        )

    # ---------------------------------------------------------------- driver
    async def run(self) -> int:
        try:
            print("=== AI Radar: single-process staging orchestration ===")
            print(f"mode: {self.report['mode']}")

            print("\n--- 1/7 doctor --database-only ---")
            await self.stage_doctor()

            print("\n--- 2/7 staging isolation (read-only snapshot) ---")
            self.pre = await self.read_state("pre-change snapshot")
            self.report["pre"] = self.pre

            print("\n--- 3/7 pending migration gate ---")
            self.gate()

            print("\n--- 4/7 apply approved staging migrations ---")
            await self.stage_migrate()

            print("\n--- 5/7 migration verification ---")
            self.post = await self.read_state("post-migration verification")
            self.report["post"] = self.post
            self.report["applied_this_run"] = self.applied_now
            self.verify_migration()

            print("\n--- 6/7 research cycle ---")
            await self.stage_research()

            if self.authorize_research:
                print("\n--- 7/7 read-only verification after research ---")
                self.after_research = await self.read_state("verification after research")
                self.report["after_research"] = self.after_research
                self.verify_after_research()
            else:
                print("\n--- 7/7 read-only verification after research: skipped (not authorized) ---")
                print(
                    "\n[STOP] Staging is migrated and verified. Research and Telegram delivery "
                    "were NOT run.\n"
                    "Authorize exactly one controlled cycle with:\n"
                    "  .\\scripts\\staging_orchestrate.ps1 -AuthorizeResearch"
                )

            if self.research_exit not in (None, cli.EXIT_OK):
                raise Abort(f"research exited {self.research_exit}")

            print("\n=== staging orchestration finished: exit code 0 ===")
            return cli.EXIT_OK
        except Abort as exc:
            self.report["failures"].append(str(exc))
            print(f"\n[ABORT] {exc}")
            print("No further step was executed; nothing was retried.")
            return cli.EXIT_FAILURE
        except DatabaseError as exc:
            self.report["failures"].append(f"database error: {exc}")
            print(f"\n[ABORT] database error: {exc}", file=sys.stderr)
            return cli.EXIT_CONFIG


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Single-process staging orchestration driver "
        "(invoked by scripts/staging_orchestrate.ps1; it supplies the staging DSN "
        "through AI_RADAR_STAGING_DATABASE_URL)."
    )
    parser.add_argument(
        "--authorize-research",
        action="store_true",
        help="after migrations verify, run exactly one research cycle (paid provider + Telegram)",
    )
    args = parser.parse_args(argv)

    try:
        config = load_config()
    except Exception as exc:  # noqa: BLE001 - config errors must be readable, not tracebacks
        print(f"configuration error: {exc}", file=sys.stderr)
        return cli.EXIT_CONFIG

    orchestrator = Orchestrator(config, authorize_research=args.authorize_research)
    code = cli.EXIT_FAILURE
    try:
        code = _run_async(orchestrator.run())
        orchestrator.report["ok"] = code == cli.EXIT_OK
        return code
    finally:
        # The report holds masked endpoints and signatures only - never the DSN.
        orchestrator.report["finished_at"] = _now()
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        REPORT.write_text(
            json.dumps(orchestrator.report, indent=2, default=str), encoding="utf-8"
        )
        print(f"\nevidence: {REPORT.relative_to(ROOT)}")


if __name__ == "__main__":
    sys.exit(main())
