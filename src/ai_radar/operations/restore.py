"""Restore a backup into an isolated database and prove the rows arrived.

Restore is the only operation that can destroy data, so it refuses to run against
the production DSN and always targets a separate database. The procedure is:

1. download the object from Drive
2. verify its SHA-256 against the manifest recorded when it was created
3. apply migrations to the target
4. truncate and load every table inside one transaction
5. compare per-table row counts against the manifest
"""

from __future__ import annotations

import gzip
import json
from typing import Any
from urllib.parse import urlsplit

from psycopg.types.json import Jsonb

from ..storage.db import Database
from ..storage.migrations import apply_migrations
from ..utils.logging import get_logger
from ..utils.timeutil import utcnow
from .backup import BACKUP_TABLES, BackupError, sha256_bytes, tables_checksum
from .drive import DriveClient, DriveError

logger = get_logger(__name__)


class RestoreError(RuntimeError):
    pass


def _targets(production_dsn: str, restore_dsn: str) -> tuple[str, str]:
    def signature(dsn: str) -> tuple[str, str, str]:
        parts = urlsplit(dsn)
        return (parts.hostname or "", parts.path or "", str(parts.port or ""))

    if not restore_dsn:
        raise RestoreError("RESTORE_DATABASE_URL is not configured")
    if signature(production_dsn) == signature(restore_dsn):
        raise RestoreError(
            "refusing to restore into the production database - point RESTORE_DATABASE_URL "
            "at a separate database"
        )
    return production_dsn, restore_dsn


class RestoreService:
    def __init__(self, drive: DriveClient | None, *, production_dsn: str) -> None:
        self._drive = drive
        self._production_dsn = production_dsn

    async def fetch(self, file_id: str | None = None) -> tuple[dict[str, Any], bytes, dict[str, Any]]:
        """Download and verify a backup. Returns (manifest, payload, tables)."""
        if self._drive is None or not self._drive.configured:
            raise RestoreError("Google Drive is not configured")

        if file_id is None:
            try:
                files = await self._drive.list_backups()
            except DriveError as exc:
                raise RestoreError(f"could not list backups: {exc}") from exc
            if not files:
                raise RestoreError("no backup files found in the configured Drive folder")
            file_id = files[0].id

        try:
            payload = await self._drive.download(file_id)
        except DriveError as exc:
            raise RestoreError(f"download failed: {exc}") from exc

        # The authoritative file checksum lives in Drive appProperties, which is
        # readable after the database it describes is gone. A backup without a
        # recorded checksum cannot be called verified, so this is a hard failure.
        try:
            remote = await self._drive.metadata(file_id)
        except DriveError as exc:
            raise RestoreError(f"could not read backup metadata: {exc}") from exc

        try:
            document = json.loads(gzip.decompress(payload))
        except (OSError, json.JSONDecodeError) as exc:
            raise RestoreError(f"backup payload is not valid gzip JSON: {exc}") from exc

        manifest = document.get("manifest") or {}
        tables = document.get("tables") or {}

        expected = (remote.app_properties or {}).get("sha256") or str(manifest.get("sha256") or "")
        if not expected:
            raise RestoreError(
                "backup carries no recorded SHA-256 - refusing to treat it as verified"
            )
        actual = sha256_bytes(payload)
        if expected != actual:
            raise RestoreError(
                f"checksum mismatch: downloaded {actual[:12]} != recorded {expected[:12]}"
            )

        recorded_tables = str(manifest.get("tables_sha256") or "")
        if recorded_tables and tables_checksum(tables) != recorded_tables:
            raise RestoreError("table data does not match the checksum recorded inside the backup")

        return manifest, payload, tables

    async def restore(
        self,
        *,
        restore_dsn: str,
        file_id: str | None = None,
    ) -> dict[str, Any]:
        _targets(self._production_dsn, restore_dsn)
        manifest, payload, tables = await self.fetch(file_id)

        database = Database(restore_dsn, min_size=1, max_size=2)
        await database.connect()
        try:
            applied = await apply_migrations(database)
            async with database.transaction() as cursor:
                await cursor.execute("truncate table " + ", ".join(BACKUP_TABLES) + " restart identity cascade")
                for table in BACKUP_TABLES:
                    rows = tables.get(table) or []
                    if not rows:
                        continue
                    await cursor.execute(
                        f"insert into {table} select * from jsonb_populate_recordset(null::{table}, %s)",  # noqa: S608
                        (Jsonb(rows),),
                    )

            restored: dict[str, int] = {}
            async with database.transaction() as cursor:
                for table in BACKUP_TABLES:
                    await cursor.execute(f"select count(*) as n from {table}")  # noqa: S608
                    row = await cursor.fetchone()
                    restored[table] = int(row["n"])

            expected_counts = manifest.get("record_counts") or {}
            mismatches = {
                table: {"expected": expected_counts.get(table), "restored": count}
                for table, count in restored.items()
                if table in expected_counts and expected_counts[table] != count
            }
            if mismatches:
                raise RestoreError(f"row counts do not match the manifest: {mismatches}")

            logger.info(
                "restore_completed",
                backup_id=manifest.get("backup_id"),
                migrations_applied=len(applied),
                tables=len(restored),
                rows=sum(restored.values()),
            )
            return {
                "backup_id": manifest.get("backup_id"),
                "restored_at": utcnow().isoformat(),
                "tables": restored,
                "total_rows": sum(restored.values()),
                "payload_bytes": len(payload),
            }
        finally:
            await database.close()


async def verify_only(drive: DriveClient | None, *, production_dsn: str, file_id: str | None = None) -> dict[str, Any]:
    """Verify a backup without restoring it (used by the weekly workflow)."""
    service = RestoreService(drive, production_dsn=production_dsn)
    manifest, payload, tables = await service.fetch(file_id)
    rows = sum(len(table_rows or []) for table_rows in tables.values())
    return {
        "backup_id": manifest.get("backup_id"),
        "created_at": manifest.get("created_at"),
        "payload_bytes": len(payload),
        "tables": len(tables),
        "rows": rows,
        "sha256": sha256_bytes(payload),
        "verified": True,
    }
