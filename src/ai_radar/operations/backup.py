"""Logical database backup to Google Drive, with integrity verification.

The backup is a single gzip-compressed JSON document containing a manifest and a
full logical export of every table that holds memory. A backup is only recorded as
``VALIDATED`` after the uploaded bytes were downloaded again and their SHA-256
matched - "the upload returned 200" is not evidence that the file is intact.

Derived tables are deliberately excluded: ``analysis_cache`` can be rebuilt and
``provider_usage`` is metering, not memory.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import uuid
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from ..models.backup import BackupManifest
from ..utils.logging import get_logger
from ..utils.timeutil import utcnow
from .drive import DriveClient, DriveError

logger = get_logger(__name__)

SCHEMA_VERSION = "1.0.0"

#: Insert order matters: parents before children.
BACKUP_TABLES: tuple[str, ...] = (
    "sources",
    "discoveries",
    "discovery_sources",
    "analysis_results",
    "pricing_history",
    "deliveries",
    "workflow_runs",
    "daily_digests",
    "backup_manifests",
    "system_state",
)

EXCLUDED_TABLES: dict[str, str] = {
    "analysis_cache": "derived cache - rebuilt on demand",
    "provider_usage": "usage metering, not research memory",
    "schema_migrations": "recreated by the migration runner",
}


class BackupError(RuntimeError):
    pass


def _jsonable(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return base64.b64encode(bytes(value)).decode("ascii")
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def tables_checksum(tables: dict[str, list[dict[str, Any]]]) -> str:
    """Checksum of the logical table data, independent of gzip framing.

    Recorded inside the payload so a restore can detect truncated or edited rows
    even before it compares against the file hash stored on Drive.
    """
    canonical = json.dumps(tables, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class BackupService:
    def __init__(self, repository, drive: DriveClient | None, *, code_version: str = "1.0.0") -> None:
        self._repo = repository
        self._db = repository.database
        self._drive = drive
        self._code_version = code_version

    # ------------------------------------------------------------------ export
    async def export(self) -> tuple[BackupManifest, bytes]:
        tables: dict[str, list[dict[str, Any]]] = {}
        counts: dict[str, int] = {}

        async with self._db.transaction() as cursor:
            # A single snapshot for every table. Under the default READ COMMITTED
            # isolation each statement sees a different snapshot, so a run that
            # commits mid-export can produce a discovery without its analysis row.
            await cursor.execute("set transaction isolation level repeatable read read only")
            for table in BACKUP_TABLES:
                await cursor.execute(f"select * from {table}")  # noqa: S608 - fixed allowlist
                rows = [dict(row) for row in await cursor.fetchall()]
                tables[table] = [_jsonable(row) for row in rows]
                counts[table] = len(rows)

        backup_id = f"backup-{utcnow():%Y%m%dT%H%M%SZ}"
        document = {
            "manifest": {
                "backup_id": backup_id,
                "schema_version": SCHEMA_VERSION,
                "code_version": self._code_version,
                "created_at": utcnow().isoformat(),
                "record_counts": counts,
                "tables_sha256": tables_checksum(tables),
                "excluded_tables": EXCLUDED_TABLES,
            },
            "tables": tables,
        }
        raw = json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        compressed = gzip.compress(raw, compresslevel=9, mtime=0)

        manifest = BackupManifest(
            backup_id=backup_id,
            schema_version=SCHEMA_VERSION,
            code_version=self._code_version,
            table_names=list(BACKUP_TABLES),
            record_counts=counts,
            file_name=f"ai-radar-backup-{backup_id.removeprefix('backup-')}.json.gz",
            uncompressed_bytes=len(raw),
            compressed_bytes=len(compressed),
            sha256_checksum=sha256_bytes(compressed),
            status="PENDING",
        )
        logger.info(
            "backup_exported",
            backup_id=backup_id,
            rows=sum(counts.values()),
            compressed_bytes=len(compressed),
        )
        return manifest, compressed

    # ------------------------------------------------------------ verification
    @staticmethod
    def verify_locally(manifest: BackupManifest, payload: bytes) -> None:
        actual = sha256_bytes(payload)
        if actual != manifest.sha256_checksum:
            raise BackupError(
                f"checksum mismatch for {manifest.backup_id}: {actual[:12]} != "
                f"{manifest.sha256_checksum[:12]}"
            )
        try:
            decoded = gzip.decompress(payload)
            json.loads(decoded)
        except (OSError, json.JSONDecodeError) as exc:
            raise BackupError(f"backup payload is not valid gzip JSON: {exc}") from exc
        if len(decoded) != manifest.uncompressed_bytes:
            raise BackupError("uncompressed size does not match the manifest")

    # -------------------------------------------------------------- local copy
    async def run_local(self, directory: Path) -> BackupManifest:
        """Export to a local file and prove the written bytes are intact.

        The offsite copy still needs Google Drive, but this path makes the export
        executable and verifiable without any OAuth grant: the file is written
        atomically, then read back and hashed against the manifest. It is recorded
        in ``backup_manifests`` exactly like a Drive backup, so an operator can
        see that the chain ran.
        """
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        manifest, payload = await self.export()
        await self._repo.save_backup(manifest)
        self.verify_locally(manifest, payload)

        target = directory / manifest.file_name
        temporary = target.with_name(target.name + ".tmp")
        temporary.write_bytes(payload)
        temporary.replace(target)

        written = target.read_bytes()
        if sha256_bytes(written) != manifest.sha256_checksum:
            manifest.status = "FAILED"
            manifest.notes.append("written file does not match the exported checksum")
            await self._repo.save_backup(manifest)
            raise BackupError(manifest.notes[-1])

        manifest.status = "VALIDATED"
        manifest.validated_at = utcnow()
        manifest.notes.append(f"local copy verified by SHA-256 ({manifest.file_name})")
        await self._repo.save_backup(manifest)
        logger.info("backup_validated_locally", backup_id=manifest.backup_id)
        return manifest

    # ------------------------------------------------------------------ upload
    async def run(self, *, retention: int = 12) -> BackupManifest:
        if self._drive is None or not self._drive.configured:
            raise BackupError("Google Drive is not configured - set the GOOGLE_OAUTH_* secrets")

        manifest, payload = await self.export()
        await self._repo.save_backup(manifest)

        self.verify_locally(manifest, payload)

        try:
            uploaded = await self._drive.upload(
                manifest.file_name,
                payload,
                app_properties={
                    "sha256": manifest.sha256_checksum,
                    "backup_id": manifest.backup_id,
                    "schema_version": manifest.schema_version,
                },
            )
        except DriveError as exc:
            manifest.status = "FAILED"
            manifest.notes.append(f"upload failed: {exc}")
            await self._repo.save_backup(manifest)
            raise BackupError(str(exc)) from exc

        manifest.google_drive_file_id = uploaded.id
        manifest.status = "UPLOADED"
        await self._repo.save_backup(manifest)

        # Verify the stored object, not the HTTP response.
        remote = await self._drive.metadata(uploaded.id)
        if remote.size is not None and remote.size != len(payload):
            manifest.status = "FAILED"
            manifest.notes.append(f"remote size {remote.size} != local {len(payload)}")
            await self._repo.save_backup(manifest)
            raise BackupError(manifest.notes[-1])
        recorded = (remote.app_properties or {}).get("sha256")
        if recorded and recorded != manifest.sha256_checksum:
            manifest.status = "FAILED"
            manifest.notes.append("checksum stored with the Drive file does not match the export")
            await self._repo.save_backup(manifest)
            raise BackupError(manifest.notes[-1])

        try:
            downloaded = await self._drive.download(uploaded.id)
        except DriveError as exc:
            manifest.status = "FAILED"
            manifest.notes.append(f"verification download failed: {exc}")
            await self._repo.save_backup(manifest)
            raise BackupError(str(exc)) from exc

        if sha256_bytes(downloaded) != manifest.sha256_checksum:
            manifest.status = "FAILED"
            manifest.notes.append("downloaded copy does not match the exported checksum")
            await self._repo.save_backup(manifest)
            raise BackupError(manifest.notes[-1])

        manifest.status = "VALIDATED"
        manifest.validated_at = utcnow()
        manifest.notes.append("downloaded copy verified by SHA-256")
        await self._repo.save_backup(manifest)
        logger.info("backup_validated", backup_id=manifest.backup_id, drive_id=uploaded.id)

        await self._apply_retention(retention)
        return manifest

    async def _apply_retention(self, retention: int) -> None:
        """Delete old backups only after a newer one has been validated."""
        if self._drive is None:
            return
        try:
            files = await self._drive.list_backups()
        except DriveError as exc:
            logger.warning("backup_retention_list_failed", error=str(exc))
            return
        stale = files[retention:]
        for item in stale:
            try:
                if await self._drive.delete(item.id):
                    logger.info("backup_pruned", file=item.name)
            except DriveError as exc:
                logger.warning("backup_prune_failed", file=item.name, error=str(exc))
