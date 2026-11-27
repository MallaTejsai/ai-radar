"""Migration runner with checksum drift detection.

Migrations are plain SQL files applied in filename order. The runner records a
SHA-256 per file, so editing an already-applied migration is detected instead of
silently diverging between the repository and the live database.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from ..settings import REPO_ROOT
from ..utils.logging import get_logger
from .db import Database

logger = get_logger(__name__)

MIGRATION_DIR = REPO_ROOT / "database" / "migrations"

_BOOTSTRAP = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    text PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now(),
    checksum   text NOT NULL
)
"""


class MigrationError(RuntimeError):
    pass


def discover_migrations(directory: Path | None = None) -> list[Path]:
    directory = directory or MIGRATION_DIR
    if not directory.exists():
        raise MigrationError(f"migration directory not found: {directory}")
    files = sorted(directory.glob("*.sql"))
    if not files:
        raise MigrationError(f"no .sql migrations in {directory}")
    return files


def checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


async def apply_migrations(database: Database, directory: Path | None = None) -> list[str]:
    """Apply pending migrations. Returns the versions applied in this call."""
    files = discover_migrations(directory)
    applied_now: list[str] = []

    async with database.transaction() as cursor:
        await cursor.execute(_BOOTSTRAP)

    async with database.transaction() as cursor:
        await cursor.execute("select version, checksum from schema_migrations")
        existing = {row["version"]: row["checksum"] for row in await cursor.fetchall()}

    for path in files:
        version = path.stem
        digest = checksum(path)
        if version in existing:
            if existing[version] != digest:
                raise MigrationError(
                    f"migration {version} was modified after being applied "
                    f"({existing[version][:8]} != {digest[:8]}); add a new migration instead"
                )
            continue

        # utf-8-sig: a BOM at the start of a SQL file is a syntax error on the
        # server, and editors on Windows add one silently.
        sql = path.read_text(encoding="utf-8-sig")
        logger.info("migration_applying", version=version, bytes=len(sql))
        try:
            async with database.transaction() as cursor:
                await cursor.execute(sql)
                await cursor.execute(
                    "insert into schema_migrations (version, checksum) values (%s, %s)",
                    (version, digest),
                )
        except Exception as exc:  # noqa: BLE001
            raise MigrationError(f"migration {version} failed: {exc}") from exc
        applied_now.append(version)
        logger.info("migration_applied", version=version)

    return applied_now
