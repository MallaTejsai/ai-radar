"""Backup export/verification and the advisory run lock."""

from __future__ import annotations

import gzip
import json
from contextlib import asynccontextmanager

import pytest

from ai_radar.operations.backup import (
    BACKUP_TABLES,
    BackupError,
    BackupService,
    sha256_bytes,
    tables_checksum,
)
from ai_radar.operations.restore import RestoreError, RestoreService
from ai_radar.storage.db import RunLock
from support.fakes import FakeDrive


# --------------------------------------------------------------------- fakes
class _FakeCursor:
    async def execute(self, sql, params=None):
        self._sql = sql

    async def fetchall(self):
        return [{"id": 1, "name": "row-one"}, {"id": 2, "name": "row-two"}]


class _FakeDatabase:
    @asynccontextmanager
    async def transaction(self):
        yield _FakeCursor()


class _FakeRepo:
    def __init__(self) -> None:
        self.database = _FakeDatabase()
        self.saved = []

    async def save_backup(self, manifest):
        self.saved.append(manifest)
        return "id"


# --------------------------------------------------------------------- export
async def test_export_covers_every_memory_table():
    service = BackupService(_FakeRepo(), None)
    manifest, payload = await service.export()
    assert manifest.table_names == list(BACKUP_TABLES)
    assert all(count == 2 for count in manifest.record_counts.values())
    assert manifest.compressed_bytes == len(payload)
    assert manifest.sha256_checksum == sha256_bytes(payload)


async def test_exported_document_is_readable_json_with_a_manifest():
    service = BackupService(_FakeRepo(), None)
    manifest, payload = await service.export()
    document = json.loads(gzip.decompress(payload))
    assert document["manifest"]["backup_id"] == manifest.backup_id
    assert set(document["tables"]) == set(BACKUP_TABLES)
    assert "analysis_cache" in document["manifest"]["excluded_tables"]


async def test_gzip_output_is_deterministic():
    first_manifest, first = await BackupService(_FakeRepo(), None).export()
    second_manifest, second = await BackupService(_FakeRepo(), None).export()
    # mtime=0 and sorted keys make the compressed bytes reproducible for equal data.
    assert json.loads(gzip.decompress(first))["tables"] == json.loads(gzip.decompress(second))["tables"]
    assert first_manifest.record_counts == second_manifest.record_counts


def test_verify_locally_accepts_a_matching_payload():
    service = BackupService(_FakeRepo(), None)
    manifest, payload = None, None
    import asyncio

    manifest, payload = asyncio.run(service.export())
    BackupService.verify_locally(manifest, payload)  # must not raise


async def test_verify_locally_rejects_a_tampered_payload():
    service = BackupService(_FakeRepo(), None)
    manifest, payload = await service.export()
    tampered = payload[:-5] + b"XXXXX"
    with pytest.raises(BackupError, match="checksum mismatch"):
        BackupService.verify_locally(manifest, tampered)


async def test_verify_locally_rejects_a_corrupt_gzip():
    service = BackupService(_FakeRepo(), None)
    manifest, payload = await service.export()
    corrupt = b"not gzip at all"
    manifest.sha256_checksum = sha256_bytes(corrupt)
    with pytest.raises(BackupError, match="not valid gzip"):
        BackupService.verify_locally(manifest, corrupt)


async def test_backup_without_drive_configuration_fails_loudly():
    service = BackupService(_FakeRepo(), None)
    with pytest.raises(BackupError, match="Google Drive is not configured"):
        await service.run()


async def test_local_backup_runs_and_verifies_without_any_oauth_grant(tmp_path):
    """The export path must be exercisable (and provable) before Drive exists."""
    repo = _FakeRepo()
    manifest = await BackupService(repo, None).run_local(tmp_path)

    assert manifest.status == "VALIDATED"
    assert manifest.validated_at is not None
    written = tmp_path / manifest.file_name
    assert written.exists()
    assert sha256_bytes(written.read_bytes()) == manifest.sha256_checksum
    # Recorded like an uploaded backup, so the chain is visible in the database.
    assert repo.saved[-1].status == "VALIDATED"


# ------------------------------------------------------------ integrity chain
async def test_backup_stores_its_checksum_with_the_drive_file():
    drive = FakeDrive()
    service = BackupService(_FakeRepo(), drive)

    manifest = await service.run()

    assert manifest.status == "VALIDATED"
    assert drive.properties[manifest.google_drive_file_id]["sha256"] == manifest.sha256_checksum


async def test_a_corrupted_download_cannot_be_reported_as_verified():
    drive = FakeDrive()
    manifest = await BackupService(_FakeRepo(), drive).run()
    # A later download returns a different, still-valid gzip document - the file
    # hash stored on Drive is the only thing that can catch this.
    drive.stored[manifest.google_drive_file_id] = gzip.compress(
        b'{"manifest":{},"tables":{}}', mtime=0
    )

    service = RestoreService(drive, production_dsn="postgresql://prod/ai_radar")
    with pytest.raises(RestoreError, match="checksum mismatch"):
        await service.fetch(manifest.google_drive_file_id)


async def test_restore_refuses_a_download_with_no_recorded_checksum():
    drive = FakeDrive()
    manifest = await BackupService(_FakeRepo(), drive).run()
    drive.properties[manifest.google_drive_file_id] = {}

    service = RestoreService(drive, production_dsn="postgresql://prod/ai_radar")
    with pytest.raises(RestoreError, match="no recorded SHA-256"):
        await service.fetch(manifest.google_drive_file_id)


async def test_restore_detects_edited_rows_even_when_the_file_hash_matches():
    drive = FakeDrive()
    await BackupService(_FakeRepo(), drive).run()
    original = next(iter(drive.stored.values()))
    document = json.loads(gzip.decompress(original))
    document["tables"]["discoveries"][0]["title"] = "a title somebody edited"

    rewritten = gzip.compress(
        json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        compresslevel=9,
        mtime=0,
    )
    drive.stored["file-edited"] = rewritten
    # Recompute the *outer* checksum so only the inner table checksum can catch it.
    drive.properties["file-edited"] = {"sha256": sha256_bytes(rewritten)}

    service = RestoreService(drive, production_dsn="postgresql://prod/ai_radar")
    with pytest.raises(RestoreError, match="table data does not match"):
        await service.fetch("file-edited")


def test_tables_checksum_is_order_independent():
    rows = [{"id": 1, "title": "x"}]
    left = {"discoveries": rows, "sources": []}
    right = {"sources": [], "discoveries": rows}
    assert tables_checksum(left) == tables_checksum(right)
    assert tables_checksum(left) != tables_checksum({"discoveries": [{"id": 2}], "sources": []})


# ------------------------------------------------------------------ run lock
class _LockCursor:
    def __init__(self, conn) -> None:
        self._conn = conn

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, sql, params=None):
        self._conn.last_sql = sql

    async def fetchone(self):
        if "pg_try_advisory_lock" in self._conn.last_sql:
            return (self._conn.results.pop(0),)
        return (True,)


class _LockConnection:
    def __init__(self, results):
        self.results = list(results)
        self.last_sql = ""

    def cursor(self):
        return _LockCursor(self)


class _LockPool:
    def __init__(self, results):
        self._results = results
        self.returned = 0

    async def getconn(self):
        return _LockConnection(self._results)

    async def putconn(self, conn):
        self.returned += 1


class _LockDatabase:
    def __init__(self, results):
        self.pool = _LockPool(results)


async def test_run_lock_acquires_after_a_retry():
    database = _LockDatabase([False, True])
    async with RunLock(database, "test-lock", timeout_seconds=5) as lock:
        assert lock.acquired is True
    assert database.pool.returned == 1


async def test_run_lock_gives_up_at_the_timeout():
    database = _LockDatabase([False, False, False, False])
    async with RunLock(database, "test-lock", timeout_seconds=1) as lock:
        assert lock.acquired is False
    assert database.pool.returned == 1
