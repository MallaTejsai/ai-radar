"""Command line interface.

Every production entry point is a subcommand here, so the GitHub Actions workflows
contain no business logic - they call one command and check the exit code.

Exit codes: 0 success, 1 runtime failure, 2 configuration/precondition problem.
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import json
import sys
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable

import yaml
from psycopg.errors import UndefinedTable

from .collectors import collect_all
from .intelligence import FreeAIProvider, GeminiProvider, GroqProvider, IntelligenceEngine, ProviderError
from .models.core import Provider, RunStatus
from .notifications import DeliveryDispatcher, TelegramClient, TelegramError
from .operations import (
    BackupError,
    BackupService,
    DigestService,
    DriveClient,
    HeartbeatReporter,
    MaintenanceService,
    ResearchPipeline,
    RestoreError,
    RestoreService,
    verify_only,
)
from .reporting import DigestBuilder
from .settings import AppConfig, Settings, load_config
from .storage import Database, DatabaseError, Repository, apply_migrations
from .storage.db import (
    assert_staging_target,
    dsn_fingerprint,
    masked_endpoint,
    validate_dsn,
)
from .utils.logging import configure_logging, get_logger
from .utils.net import SafeHttpClient
from .utils.redact import scrub
from .utils.timeutil import utcnow
from .verification import EvidenceVerifier

logger = get_logger(__name__)

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_CONFIG = 2


class PreconditionError(RuntimeError):
    """A required secret or resource is missing."""


@dataclass
class AppContext:
    config: AppConfig
    http: SafeHttpClient
    db: Database | None
    repo: Repository | None
    engine: IntelligenceEngine
    verifier: EvidenceVerifier
    dispatcher: DeliveryDispatcher | None
    telegram: TelegramClient | None
    heartbeat: HeartbeatReporter | None
    drive: DriveClient | None


def _freeai_usage_lookup(
    repo: Repository | None,
) -> Callable[[], Awaitable[int]] | None:
    if repo is None:
        return None
    return functools.partial(repo.provider_tokens_last_24h, Provider.FREEAI.value)


def _xai_usage_lookup(
    repo: Repository | None,
) -> Callable[[], Awaitable[int]] | None:
    if repo is None:
        return None
    return functools.partial(repo.provider_tokens_last_24h, Provider.GROK.value)


def _sarvam_usage_lookup(
    repo: Repository | None,
) -> Callable[[], Awaitable[int]] | None:
    if repo is None:
        return None
    return functools.partial(repo.provider_tokens_last_24h, Provider.SARVAM.value)


def _staging_settings(config: AppConfig) -> Settings:
    """Return the settings a run should use, honouring staging mode.

    In staging mode the effective database is ``AI_RADAR_STAGING_DATABASE_URL``,
    never ``DATABASE_URL``. Production is therefore not merely *avoided* - it is
    never read, so a forgotten override cannot reach it.
    """
    settings = config.settings
    if not settings.ai_radar_staging:
        return settings
    staging_dsn = settings.ai_radar_staging_database_url
    # Shape first (readable message for an unreplaced [YOUR-PASSWORD] or a
    # transaction-pooler port), then identity against production.
    validate_dsn(staging_dsn, label="AI_RADAR_STAGING_DATABASE_URL")
    assert_staging_target(
        staging_dsn,
        staging=True,
        production_dsn=settings.database_url,
    )
    return settings.model_copy(
        update={"database_url": settings.ai_radar_staging_database_url}
    )


@asynccontextmanager
async def build_context(
    config: AppConfig,
    *,
    need_db: bool = True,
    dry_run: bool = False,
) -> AsyncIterator[AppContext]:
    # Raises DatabaseError before any connection if staging mode is misconfigured.
    settings = _staging_settings(config)

    http = SafeHttpClient(
        max_bytes=settings.http_max_bytes,
        timeout=settings.http_timeout_seconds,
        retries=settings.http_retries,
    )
    db: Database | None = None
    repo: Repository | None = None
    try:
        if need_db:
            if not settings.database_url:
                raise PreconditionError(
                    "DATABASE_URL is not set. Add it as a GitHub encrypted secret "
                    "(or to .env for local runs)."
                )
            db = Database(settings.database_url)
            await db.connect()
            repo = Repository(db)

        telegram: TelegramClient | None = None
        if settings.telegram_bot_token and settings.telegram_chat_id:
            telegram = TelegramClient(http, settings.telegram_bot_token, settings.telegram_chat_id)

        engine = IntelligenceEngine(
            settings=settings,
            signals=config.scoring.signals,
            client=http,
            cache=repo,
            metrics=repo,
            freeai_usage_lookup=_freeai_usage_lookup(repo),
            xai_usage_lookup=_xai_usage_lookup(repo),
            sarvam_usage_lookup=_sarvam_usage_lookup(repo),
        )
        verifier = EvidenceVerifier(http, config.scoring.signals)

        dispatcher: DeliveryDispatcher | None = None
        if repo is not None and telegram is not None and not dry_run:
            dispatcher = DeliveryDispatcher(
                repo,
                telegram,
                worker_id=f"{settings.telegram_chat_id[:6]}-{uuid.uuid4().hex[:8]}",
                category_names={
                    key.value: value.display_name for key, value in config.categories.categories.items()
                },
                max_per_run=config.max_alerts_per_run,
                alert_max_age_hours=config.alert_max_age_hours,
            )

        heartbeat = (
            HeartbeatReporter(
                repo,
                http,
                ping_url=settings.healthchecks_ping_url,
                stale_after_minutes=settings.stale_after_minutes,
            )
            if repo is not None
            else None
        )

        drive: DriveClient | None = None
        if settings.google_oauth_client_id and settings.google_oauth_refresh_token:
            drive = DriveClient(
                http,
                client_id=settings.google_oauth_client_id,
                client_secret=settings.google_oauth_client_secret,
                refresh_token=settings.google_oauth_refresh_token,
                folder_id=settings.google_drive_folder_id,
            )

        yield AppContext(
            config=config,
            http=http,
            db=db,
            repo=repo,
            engine=engine,
            verifier=verifier,
            dispatcher=dispatcher,
            telegram=telegram,
            heartbeat=heartbeat,
            drive=drive,
        )
    finally:
        await http.aclose()
        if db is not None:
            await db.close()


# --------------------------------------------------------------------- commands
async def cmd_research(config: AppConfig, args: argparse.Namespace) -> int:
    async with build_context(config, dry_run=args.dry_run) as ctx:
        assert ctx.repo is not None and ctx.heartbeat is not None
        pipeline = ResearchPipeline(
            config=config,
            repository=ctx.repo,
            http=ctx.http,
            engine=ctx.engine,
            verifier=ctx.verifier,
            dispatcher=ctx.dispatcher,
            heartbeat=ctx.heartbeat,
        )
        report = await pipeline.run(trigger=args.trigger)
        print(json.dumps(report.model_dump(mode="json"), indent=2, default=str))

        if args.dry_run:
            print("\n[dry run] Telegram dispatcher was not constructed; no alerts were sent.")
        if report.status is RunStatus.FAILED:
            return EXIT_FAILURE
        return EXIT_OK


async def cmd_collect(config: AppConfig, args: argparse.Namespace) -> int:
    """Collect and report without persisting - a diagnostic for new sources."""
    async with build_context(config, need_db=False) as ctx:
        specs = config.sources.enabled() if not args.source else [
            spec for spec in config.sources.enabled() if spec.id in args.source
        ]
        results = await collect_all(
            specs,
            {},
            ctx.http,
            concurrency=config.settings.collect_concurrency,
            github_token=config.settings.github_token,
        )
        rows = [
            {
                "source_id": source_id,
                "ok": result.ok,
                "status": result.http_status,
                "entries": len(result.entries),
                "error": result.error,
                "note": result.note,
                "elapsed_ms": result.elapsed_ms,
            }
            for source_id, result in sorted(results.items())
        ]
        print(json.dumps(rows, indent=2))
        return EXIT_OK if all(row["ok"] for row in rows) else EXIT_FAILURE


async def cmd_digest(config: AppConfig, args: argparse.Namespace) -> int:
    async with build_context(config, dry_run=args.dry_run) as ctx:
        assert ctx.repo is not None
        service = DigestService(
            repository=ctx.repo,
            telegram=ctx.telegram,
            builder=DigestBuilder(
                category_names={
                    key.value: value.display_name
                    for key, value in config.categories.categories.items()
                }
            ),
            min_confidence=config.scoring.alert.min_confidence_for_digest,
        )
        result = await service.run(dry_run=args.dry_run, resend=args.resend)
        print(json.dumps({k: v for k, v in result.items() if k != "text"}, indent=2))
        if args.dry_run:
            print("\n--- digest preview ---\n" + result["text"])
        return EXIT_OK if not str(result["status"]).startswith("send_failed") else EXIT_FAILURE


async def cmd_backup(config: AppConfig, args: argparse.Namespace) -> int:
    async with build_context(config) as ctx:
        assert ctx.repo is not None
        service = BackupService(ctx.repo, ctx.drive)
        if args.local_dir:
            # Exercisable without Google OAuth: export, write, re-read, hash.
            manifest = await service.run_local(Path(args.local_dir))
            print(json.dumps(manifest.model_dump(mode="json"), indent=2))
            return EXIT_OK
        if ctx.drive is None or not ctx.drive.configured:
            raise PreconditionError(
                "Google Drive is not configured (GOOGLE_OAUTH_CLIENT_ID / "
                "GOOGLE_OAUTH_CLIENT_SECRET / GOOGLE_OAUTH_REFRESH_TOKEN). "
                "Use --local-dir for an offline export that still verifies."
            )
        manifest = await service.run(retention=args.retention)
        print(json.dumps(manifest.model_dump(mode="json"), indent=2))
        return EXIT_OK


async def cmd_restore(config: AppConfig, args: argparse.Namespace) -> int:
    async with build_context(config, need_db=False) as ctx:
        if ctx.drive is None or not ctx.drive.configured:
            raise PreconditionError(
                "Google Drive is not configured (GOOGLE_OAUTH_CLIENT_ID / "
                "GOOGLE_OAUTH_CLIENT_SECRET / GOOGLE_OAUTH_REFRESH_TOKEN)"
            )
        target = args.target_dsn or config.settings.restore_database_url
        if args.verify_only:
            result = await verify_only(
                ctx.drive, production_dsn=config.settings.database_url, file_id=args.file_id
            )
            print(json.dumps(result, indent=2))
            return EXIT_OK
        service = RestoreService(ctx.drive, production_dsn=config.settings.database_url)
        result = await service.restore(restore_dsn=target, file_id=args.file_id)
        print(json.dumps(result, indent=2))
        return EXIT_OK


async def cmd_maintenance(config: AppConfig, args: argparse.Namespace) -> int:
    async with build_context(config) as ctx:
        assert ctx.repo is not None and ctx.heartbeat is not None
        service = MaintenanceService(
            repository=ctx.repo,
            heartbeat=ctx.heartbeat,
            telegram=ctx.telegram,
            stale_after_minutes=config.settings.stale_after_minutes,
        )
        result = await service.run(notify=not args.no_notify, prune=not args.no_prune)
        print(json.dumps({k: v for k, v in result.items() if k != "overview"}, indent=2))
        # A stale system is a real failure as far as the schedule is concerned.
        return EXIT_FAILURE if result["stale"] else EXIT_OK


async def cmd_migrate(config: AppConfig, args: argparse.Namespace) -> int:
    async with build_context(config) as ctx:
        assert ctx.db is not None
        applied = await apply_migrations(ctx.db)
        print(json.dumps({"applied": applied}, indent=2))
        return EXIT_OK


async def cmd_notify(config: AppConfig, args: argparse.Namespace) -> int:
    settings = config.settings
    if not settings.telegram_bot_token:
        raise PreconditionError("TELEGRAM_BOT_TOKEN is not configured")

    async with build_context(config, need_db=False) as ctx:
        # Built here rather than taken from ctx: build_context only constructs a
        # client when the token AND the chat id exist, but the token can be
        # validated before a chat id has been obtained.
        telegram = TelegramClient(
            ctx.http,
            settings.telegram_bot_token,
            settings.telegram_chat_id,
        )

        if args.check:
            # Read-only: getMe + getChat. No message is sent, so this is safe to run
            # while the notification gate is closed.
            report: dict[str, Any] = {"token": "UNKNOWN", "chat": "UNKNOWN"}
            try:
                me = await telegram.get_me()
            except TelegramError as exc:
                print(
                    json.dumps(
                        {"token": "FAIL", "kind": exc.kind, "detail": scrub(str(exc))},
                        indent=2,
                    )
                )
                return EXIT_FAILURE
            report["token"] = "PASS"
            report["bot"] = {"username": me.get("username"), "id": me.get("id")}

            if not settings.telegram_chat_id:
                report["chat"] = "NOT_CONFIGURED"
                print(json.dumps(report, indent=2))
                print(
                    "\nToken is valid. The remaining gate is TELEGRAM_CHAT_ID: message "
                    "the bot once, then read result[0].message.chat.id from "
                    "https://api.telegram.org/bot<TOKEN>/getUpdates.",
                    file=sys.stderr,
                )
                return EXIT_CONFIG

            try:
                chat = await telegram.get_chat()
            except TelegramError as exc:
                report["chat"] = {
                    "status": "FAIL",
                    "kind": exc.kind,
                    "detail": scrub(str(exc)),
                }
                print(json.dumps(report, indent=2))
                return EXIT_FAILURE
            report["chat"] = {
                "status": "PASS",
                "id": chat.get("id"),
                "type": chat.get("type"),
                "title": chat.get("title") or chat.get("username"),
            }
            print(json.dumps(report, indent=2))
            print("\nToken and chat id are valid. No message was sent.")
            return EXIT_OK

        if args.test:
            if not settings.telegram_chat_id:
                raise PreconditionError("TELEGRAM_CHAT_ID is not configured")
            me = await telegram.get_me()
            message_id = await telegram.send_message(
                "<b>AI Radar</b> test message.\n"
                "If you can read this, the bot token and chat id are correct.\n"
                "<i>Sent manually, not by a research run.</i>"
            )
            print(json.dumps({"bot": me.get("username"), "message_id": message_id}, indent=2))
        return EXIT_OK


async def cmd_heartbeat(config: AppConfig, args: argparse.Namespace) -> int:
    async with build_context(config) as ctx:
        assert ctx.heartbeat is not None
        check = await ctx.heartbeat.check()
        print(json.dumps({k: v for k, v in check.items() if k != "snapshot"}, indent=2, default=str))
        return EXIT_FAILURE if check["stale"] else EXIT_OK


# ------------------------------------------------------------------------ doctor
SCHEDULE_WORKFLOWS = {
    "research": "research.yml",
    "digest": "digest.yml",
    "backup": "backup.yml",
    "maintenance": "maintenance.yml",
}


def _workflow_crons(path: Path) -> list[str]:
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    # PyYAML parses the bare key `on:` as boolean True (YAML 1.1).
    triggers = data.get("on", data.get(True)) or {}
    if isinstance(triggers, dict):
        schedule = triggers.get("schedule") or []
        return [str(entry.get("cron")) for entry in schedule if isinstance(entry, dict)]
    return []


def audit_schedule(config: AppConfig) -> list[str]:
    """Compare workflow crons with config/settings.yaml (drift detection)."""
    problems: list[str] = []
    workflows = config.paths.root / ".github" / "workflows"
    expected = {
        "research": config.policy.schedule.research.cron_utc,
        "digest": config.policy.schedule.digest.cron_utc,
        "backup": config.policy.schedule.backup.cron_utc,
        "maintenance": config.policy.schedule.maintenance.cron_utc,
    }
    for key, cron in expected.items():
        path = workflows / SCHEDULE_WORKFLOWS[key]
        if not path.exists():
            problems.append(f"workflow missing: .github/workflows/{SCHEDULE_WORKFLOWS[key]}")
            continue
        crons = _workflow_crons(path)
        if cron not in crons:
            problems.append(
                f"{SCHEDULE_WORKFLOWS[key]} schedule {crons or 'none'} does not match "
                f"policy cron {cron!r}"
            )
    return problems


def _render_doctor_checks(checks: list[dict[str, Any]]) -> int:
    width = max(len(item["check"]) for item in checks)
    for item in checks:
        mark = "PASS" if item["ok"] else ("FAIL" if item["blocking"] else "WARN")
        print(f"[{mark}] {item['check']:<{width}}  {item['detail']}")

    blocking_failures = [item for item in checks if not item["ok"] and item["blocking"]]
    return EXIT_OK if not blocking_failures else EXIT_CONFIG


async def cmd_doctor(config: AppConfig, args: argparse.Namespace) -> int:
    checks: list[dict[str, Any]] = []

    def record(name: str, ok: bool, detail: str, *, blocking: bool = True) -> None:
        checks.append({"check": name, "ok": ok, "detail": detail, "blocking": blocking})

    try:
        settings = _staging_settings(config)
    except DatabaseError as exc:
        record("staging target", False, str(exc))
        return _render_doctor_checks(checks)
    if settings.ai_radar_staging:
        # Report the masked target so the operator can confirm identity without
        # the DSN ever appearing in output. The production URL is used only for
        # the endpoint comparison above - no connection is ever opened with it.
        production = config.settings.database_url
        detail = f"staging {masked_endpoint(settings.database_url)}"
        if production:
            detail += (
                f"; production {masked_endpoint(production)}"
                "; different endpoints, production never connected"
            )
        else:
            detail += "; no production DATABASE_URL to compare; disposable name accepted"
        detail += f"; fingerprint {dsn_fingerprint(settings.database_url)}"
        record("staging target", True, detail)

    record(
        "configuration",
        True,
        f"{len(config.sources.sources)} sources, {len(config.categories.categories)} categories, "
        f"confidence threshold {config.confidence_threshold:.2f} "
        f"({'calibrated on a labelled sample' if config.calibration.calibrated else 'seed value, not calibrated'})",
    )

    problems = config.sources.validate_registry()
    record("source registry", not problems, "; ".join(problems) or "structure valid")

    record(
        "schedule policy",
        not audit_schedule(config),
        "; ".join(audit_schedule(config)) or "workflow crons match config/settings.yaml",
        blocking=False,
    )

    async with build_context(config, need_db=False) as ctx:
        # Database
        if not settings.database_url:
            record("database", False, "DATABASE_URL not set")
        else:
            db = None
            try:
                db = Database(settings.database_url)
                await db.connect()
                ok = await db.ping()
                record("database", ok, "connected and responding" if ok else "ping failed")
                if ok:
                    try:
                        async with db.transaction() as cursor:
                            await cursor.execute("select version from schema_migrations")
                            applied = {row["version"] for row in await cursor.fetchall()}
                        from .storage.migrations import discover_migrations

                        pending = [
                            path.stem for path in discover_migrations() if path.stem not in applied
                        ]
                        record(
                            "migrations",
                            not pending,
                            f"pending: {', '.join(pending)}" if pending else f"{len(applied)} applied",
                        )
                    except UndefinedTable:
                        # A fresh project is reachable but has no schema yet. That is
                        # not a connection failure; say so instead of masking the PASS.
                        record("migrations", False, "schema not applied yet (run ai-radar migrate)")
            except Exception as exc:  # noqa: BLE001
                record("database", False, f"{type(exc).__name__}: {scrub(str(exc))}")
            finally:
                if db is not None:
                    await db.close()

        if getattr(args, "database_only", False):
            # Deliberately return before any provider, Telegram, Drive or heartbeat
            # probe. build_context has only constructed clients; nothing was sent.
            return _render_doctor_checks(checks)

        # Intelligence providers, in the same order the router uses.
        factories = (
            (
                "free.ai",
                settings.freeai_api_key,
                lambda http, key: FreeAIProvider(
                    http,
                    key,
                    model=settings.freeai_model,
                    base_url=settings.freeai_base_url,
                    max_output_tokens=settings.freeai_max_output_tokens,
                    daily_token_budget=settings.freeai_daily_token_budget,
                    max_calls_per_run=settings.freeai_max_calls_per_run,
                ),
            ),
            (
                "Gemini",
                settings.gemini_api_key,
                lambda http, key: GeminiProvider(http, key, pinned_model=settings.gemini_model),
            ),
            (
                "Groq",
                settings.groq_api_key,
                lambda http, key: GroqProvider(http, key, pinned_model=settings.groq_model),
            ),
        )
        for label, key, factory in factories:
            if not key:
                record(label, False, "API key not set", blocking=False)
                continue
            provider = factory(ctx.http, key)
            try:
                model = await provider.resolve_model()
                record(label, True, f"reachable, resolved model {model}")
            except ProviderError as exc:
                record(label, False, f"{exc.kind}: {exc}", blocking=False)

        # Telegram
        if settings.ai_radar_staging and not settings.ai_radar_staging_allow_telegram:
            record(
                "telegram",
                True,
                "disabled in staging mode (AI_RADAR_STAGING=1); nothing can be sent",
                blocking=False,
            )
        elif ctx.telegram is None:
            record("telegram", False, "bot token / chat id not set")
        else:
            try:
                me = await ctx.telegram.get_me()
                record("telegram", True, f"authenticated as @{me.get('username')} (chat {settings.telegram_chat_id})")
            except TelegramError as exc:
                record("telegram", False, f"{exc.kind}: {exc}")

        # Google Drive
        if ctx.drive is None or not ctx.drive.configured:
            record("google drive", False, "OAuth client / refresh token not set", blocking=False)
        else:
            try:
                token = await ctx.drive.access_token()
                files = await ctx.drive.list_backups()
                record(
                    "google drive",
                    bool(token),
                    f"token acquired, {len(files)} backup file(s) visible in the target folder",
                )
            except Exception as exc:  # noqa: BLE001
                record("google drive", False, f"{type(exc).__name__}: {exc}", blocking=False)

        record(
            "heartbeat",
            bool(settings.healthchecks_ping_url),
            "ping URL configured" if settings.healthchecks_ping_url else "HEALTHCHECKS_PING_URL not set",
            blocking=False,
        )

    return _render_doctor_checks(checks)


async def cmd_sources(config: AppConfig, args: argparse.Namespace) -> int:
    problems = config.sources.validate_registry()
    coverage: dict[str, int] = {}
    for spec in config.sources.sources:
        coverage[spec.category.value] = coverage.get(spec.category.value, 0) + 1
    print(json.dumps(
        {
            "total": len(config.sources.sources),
            "enabled": len(config.sources.enabled()),
            "coverage": coverage,
            "problems": problems,
        },
        indent=2,
    ))
    return EXIT_OK if not problems else EXIT_CONFIG


# ------------------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ai-radar", description="AI Radar V1.0")
    parser.add_argument("--config-dir", default=None, help="override the config directory")
    parser.add_argument("--log-level", default=None, help="DEBUG/INFO/WARNING/ERROR")
    sub = parser.add_subparsers(dest="command", required=True)

    research = sub.add_parser("research", help="run the full research pipeline")
    research.add_argument("--trigger", default="schedule")
    research.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "run the full pipeline but never send Telegram; note it still writes "
            "discoveries and advances checkpoints"
        ),
    )
    research.set_defaults(func=cmd_research)

    collect = sub.add_parser("collect", help="collect sources only (diagnostic)")
    collect.add_argument("--source", action="append", help="limit to one source id (repeatable)")
    collect.set_defaults(func=cmd_collect)

    digest = sub.add_parser("digest", help="build and send the daily digest")
    digest.add_argument("--dry-run", action="store_true")
    digest.add_argument("--resend", action="store_true")
    digest.set_defaults(func=cmd_digest)

    backup = sub.add_parser("backup", help="export, upload and verify a backup")
    backup.add_argument("--retention", type=int, default=12)
    backup.add_argument(
        "--local-dir",
        default="",
        help="write and verify the backup locally instead of uploading to Drive",
    )
    backup.set_defaults(func=cmd_backup)

    restore = sub.add_parser("restore", help="restore or verify a backup")
    restore.add_argument("--file-id", default=None)
    restore.add_argument("--target-dsn", default=None)
    restore.add_argument("--verify-only", action="store_true")
    restore.set_defaults(func=cmd_restore)

    maintenance = sub.add_parser("maintenance", help="health check, pruning, quota report")
    maintenance.add_argument("--no-notify", action="store_true")
    maintenance.add_argument("--no-prune", action="store_true")
    maintenance.set_defaults(func=cmd_maintenance)

    heartbeat = sub.add_parser("heartbeat", help="evaluate staleness from stored state")
    heartbeat.set_defaults(func=cmd_heartbeat)

    migrate = sub.add_parser("migrate", help="apply database migrations")
    migrate.set_defaults(func=cmd_migrate)

    doctor = sub.add_parser("doctor", help="verify configuration and live integrations")
    doctor.add_argument(
        "--database-only",
        action="store_true",
        help="check configuration and PostgreSQL only; never contacts providers, Telegram, Drive or the heartbeat",
    )
    doctor.set_defaults(func=cmd_doctor)

    sources = sub.add_parser("sources", help="inspect the source registry")
    sources.add_argument("--validate", action="store_true")
    sources.set_defaults(func=cmd_sources)

    notify = sub.add_parser("notify", help="Telegram helpers")
    notify.add_argument(
        "--check",
        action="store_true",
        help="validate the bot token and chat id read-only (getMe/getChat; sends nothing)",
    )
    notify.add_argument(
        "--test",
        action="store_true",
        help="send one real test message (requires explicit approval)",
    )
    notify.set_defaults(func=cmd_notify)

    return parser


def _run_async(coro):
    """Run a coroutine on a loop compatible with psycopg on Windows.

    Windows defaults to ProactorEventLoop, which psycopg's async driver refuses.
    CI runs on Linux, but local commands (`doctor`, `migrate`, `research`) must
    work on the development PC too.
    """
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    return asyncio.run(coro)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    configure_logging(level=args.log_level)
    try:
        config = load_config(args.config_dir)
    except Exception as exc:  # noqa: BLE001 - config errors must be readable, not tracebacks
        print(f"configuration error: {exc}", file=sys.stderr)
        return EXIT_CONFIG

    try:
        return _run_async(args.func(config, args))
    except PreconditionError as exc:
        print(f"missing configuration: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    except DatabaseError as exc:
        # Configuration-level database problems (malformed DSN, staging target
        # refused) must be readable, never a traceback.
        print(f"database error: {scrub(str(exc))}", file=sys.stderr)
        return EXIT_CONFIG
    except (BackupError, RestoreError) as exc:
        print(f"backup/restore failed: {exc}", file=sys.stderr)
        return EXIT_FAILURE
    except KeyboardInterrupt:  # pragma: no cover
        print("interrupted", file=sys.stderr)
        return EXIT_FAILURE


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
