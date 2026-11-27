"""In-memory test doubles.

These exist **only** for tests. Production code never falls back to an in-memory
store: if PostgreSQL is unreachable, the pipeline fails instead of pretending to
remember. The doubles below mirror the SQL semantics closely enough to exercise
the pipeline, the delivery queue and the heartbeat.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Any

from ai_radar.models.analysis import AnalysisResult
from ai_radar.models.core import DeliveryStatus
from ai_radar.models.delivery import DeliveryIntent
from ai_radar.models.discovery import DiscoveryRecord
from ai_radar.models.run import RunReport
from ai_radar.models.source import SourceSpec, SourceState
from ai_radar.utils.timeutil import utcnow


def _as_datetime(value):
    """Accept the ISO strings ``model_dump(mode="json")`` produces."""
    if value is None or isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


class _RowsCursor:
    """Cursor double used by the backup exporter."""

    async def execute(self, sql, params=None):
        self._sql = sql

    async def fetchall(self):
        return [{"id": 1, "name": "row-one"}, {"id": 2, "name": "row-two"}]


class _BackupDatabase:
    @asynccontextmanager
    async def transaction(self):
        yield _RowsCursor()


class FakeRepository:
    """Minimal but faithful in-memory repository."""

    def __init__(self) -> None:
        # Only the backup exporter reaches into ``database``; the advisory lock is
        # patched out in pipeline tests.
        self.database = _BackupDatabase()
        self.saved: list[Any] = []
        self.sources: dict[str, dict[str, Any]] = {}
        self.discoveries: dict[str, dict[str, Any]] = {}
        self.evidence: list[dict[str, Any]] = []
        self.analyses: dict[str, AnalysisResult] = {}
        self.deliveries: dict[str, dict[str, Any]] = {}
        self.runs: dict[str, RunReport] = {}
        self.state: dict[str, dict[str, Any]] = {}
        self.provider_calls: list[dict[str, Any]] = []
        self.digests: dict[str, dict[str, Any]] = {}
        self.checkpoint_advances: list[str] = []
        self.source_failures: list[tuple[str, str]] = []
        self.cache: dict[str, AnalysisResult] = {}
        self.fail_on_persist = False
        self.fail_on_existing = False
        self.fail_on_mark_sent = False
        self.fail_on_overview = False
        #: discovery ids for which a delivery intent was created
        self.queued_for: list[str] = []

    # ------------------------------------------------------------------ sources
    async def sync_sources(self, specs) -> int:
        for spec in specs:
            existing = self.sources.get(spec.id)
            self.sources[spec.id] = {
                "source_id": spec.id,
                "health_status": existing["health_status"] if existing else "unknown",
                "last_checkpoint": existing["last_checkpoint"] if existing else None,
                "etag": existing["etag"] if existing else None,
                "last_modified": existing["last_modified"] if existing else None,
                "consecutive_failures": existing["consecutive_failures"] if existing else 0,
            }
        return len(specs)

    async def load_source_states(self) -> dict[str, SourceState]:
        return {
            source_id: SourceState(source_id=source_id, **{
                key: value
                for key, value in row.items()
                if key in {"last_checkpoint", "last_error", "consecutive_failures",
                           "health_status", "etag", "last_modified", "records_collected"}
            })
            for source_id, row in self.sources.items()
        }

    async def record_source_success(self, source_id, *, etag, last_modified, entries, checkpoint=None):
        self.checkpoint_advances.append(source_id)
        row = self.sources.setdefault(source_id, {"source_id": source_id})
        row.update(
            {
                "last_checkpoint": checkpoint or utcnow(),
                "health_status": "healthy",
                "etag": etag,
                "last_modified": last_modified,
                "consecutive_failures": 0,
            }
        )

    async def record_source_failure(self, source_id, error):
        self.source_failures.append((source_id, error))
        row = self.sources.setdefault(source_id, {"source_id": source_id})
        row["health_status"] = "degraded"

    # -------------------------------------------------------------- discoveries
    async def existing_identities(self, canonical_keys, title_fingerprints):
        if self.fail_on_existing:
            raise RuntimeError("simulated database outage")
        keys = set(self.discoveries)
        fingerprints = {
            row["title_fingerprint"]: key for key, row in self.discoveries.items()
        }
        return keys, fingerprints

    async def upsert_discovery(self, record: DiscoveryRecord, analysis: AnalysisResult | None = None):
        if self.fail_on_persist:
            raise RuntimeError("simulated database failure")
        created = record.canonical_key not in self.discoveries
        discovery_id = self.discoveries.get(record.canonical_key, {}).get(
            "id", str(uuid.uuid4())
        )
        self.discoveries[record.canonical_key] = {
            **record.model_dump(mode="json"),
            "id": discovery_id,
            "title_fingerprint": record.title_fingerprint,
            "analysis": analysis.payload.model_dump(mode="json") if analysis else None,
            "source_name": "Test Source",
            "status": record.status.value,
            "already_alerted": False,
        }
        for item in record.evidence:
            self.evidence.append({"discovery_id": discovery_id, **item.model_dump(mode="json")})
        if analysis is not None:
            self.analyses[record.canonical_key] = analysis
        return discovery_id, created

    async def get_discovery_context(self, canonical_key):
        row = self.discoveries.get(canonical_key)
        return dict(row) if row else None

    async def get_discovery(self, canonical_key):
        return self.discoveries.get(canonical_key)

    async def mark_notified(self, discovery_id):
        for row in self.discoveries.values():
            if row["id"] == discovery_id:
                row["status"] = "notified"
                row["already_alerted"] = True

    async def discoveries_for_digest(self, start, end, *, min_confidence, limit=10):
        rows = [
            row
            for row in self.discoveries.values()
            if row.get("verdict") == "CONFIRMED"
            and float(row.get("confidence") or 0) >= min_confidence
            and row.get("status") != "archived"
        ]
        rows.sort(key=lambda row: row.get("priority_score") or 0, reverse=True)
        return rows[:limit]

    # ---------------------------------------------------------------- deliveries
    async def queue_delivery(self, discovery_id, message_text, message_hash):
        for row in self.deliveries.values():
            if row["discovery_id"] == discovery_id and row["channel"] == "telegram":
                return None
        delivery_id = str(uuid.uuid4())
        self.deliveries[delivery_id] = {
            "id": delivery_id,
            "discovery_id": discovery_id,
            "channel": "telegram",
            "status": DeliveryStatus.PENDING.value,
            "attempts": 0,
            "message_text": message_text,
            "message_hash": message_hash,
            "telegram_message_id": None,
            "last_error": None,
            "claimed_by": None,
            "claimed_at": None,
            "next_attempt_at": utcnow(),
            "created_at": utcnow(),
        }
        self.queued_for.append(discovery_id)
        return delivery_id

    def _discovery_published_at(self, discovery_id):
        for row in self.discoveries.values():
            if row.get("id") == discovery_id:
                return _as_datetime(row.get("published_at"))
        return None

    async def claim_deliveries(self, worker_id, limit, *, max_age_hours=None):
        now = utcnow()
        ready = []
        for row in self.deliveries.values():
            if row["status"] not in (
                DeliveryStatus.PENDING.value,
                DeliveryStatus.RETRY_WAIT.value,
            ):
                continue
            if row["next_attempt_at"] > now:
                continue
            if max_age_hours is not None:
                published = self._discovery_published_at(row["discovery_id"])
                if published is not None:
                    age_hours = (now - published).total_seconds() / 3600.0
                    if age_hours > max_age_hours:
                        continue
            ready.append(row)
        ready.sort(key=lambda row: row["created_at"])
        claimed = []
        for row in ready[:limit]:
            row.update(
                {
                    "status": DeliveryStatus.SENDING.value,
                    "claimed_by": worker_id,
                    "claimed_at": now,
                    "attempts": row["attempts"] + 1,
                }
            )
            claimed.append(
                DeliveryIntent(
                    id=row["id"],
                    discovery_key=row["discovery_id"],
                    status=DeliveryStatus(row["status"]),
                    attempts=row["attempts"],
                    message_text=row["message_text"],
                    message_hash=row["message_hash"],
                    claimed_by=worker_id,
                    claimed_at=now,
                )
            )
        return claimed

    async def mark_delivery_sent(self, delivery_id, telegram_message_id):
        if self.fail_on_mark_sent:
            raise RuntimeError("simulated crash after the send was accepted")
        self.deliveries[delivery_id].update(
            {"status": DeliveryStatus.SENT.value, "telegram_message_id": telegram_message_id}
        )

    async def mark_delivery_retry(self, delivery_id, error, *, delay_seconds):
        self.deliveries[delivery_id].update(
            {
                "status": DeliveryStatus.RETRY_WAIT.value,
                "last_error": error,
                "next_attempt_at": utcnow() + timedelta(seconds=delay_seconds),
            }
        )

    async def mark_delivery_unknown(self, delivery_id, error):
        self.deliveries[delivery_id].update(
            {"status": DeliveryStatus.UNKNOWN.value, "last_error": error}
        )

    async def defer_delivery(self, delivery_id, *, delay_seconds, reason):
        self.deliveries[delivery_id].update(
            {
                "status": DeliveryStatus.RETRY_WAIT.value,
                "last_error": reason,
                "next_attempt_at": utcnow() + timedelta(seconds=delay_seconds),
            }
        )

    async def expire_stale_deliveries(self, max_age_hours):
        now = utcnow()
        expired = 0
        for row in self.deliveries.values():
            if row["status"] not in (
                DeliveryStatus.PENDING.value,
                DeliveryStatus.RETRY_WAIT.value,
            ):
                continue
            published = self._discovery_published_at(row["discovery_id"])
            if published is None:
                continue
            if (now - published).total_seconds() / 3600.0 > max_age_hours:
                row["status"] = DeliveryStatus.UNKNOWN.value
                row["last_error"] = (
                    "expired: discovery older than the alert window before delivery"
                )
                expired += 1
        return expired

    async def release_stale_claims(self, older_than_minutes=15):
        return 0

    async def delivery_counts(self):
        counts: dict[str, int] = {}
        for row in self.deliveries.values():
            counts[row["status"]] = counts.get(row["status"], 0) + 1
        return counts

    # --------------------------------------------------------------------- runs
    async def start_run(self, report: RunReport):
        self.runs[report.run_id] = report

    async def finish_run(self, report: RunReport):
        self.runs[report.run_id] = report

    async def last_successful_run(self):
        finished = [
            run.finished_at
            for run in self.runs.values()
            if run.finished_at and run.status.value in ("completed", "degraded")
        ]
        return max(finished) if finished else None

    async def recent_run_starts(self, hours=48):
        return [run.started_at for run in self.runs.values()]

    async def last_run_status(self):
        if not self.runs:
            return None, None
        run = max(self.runs.values(), key=lambda item: item.started_at)
        return run.run_id, run.status.value

    # ------------------------------------------------------------------- digest
    async def save_digest(self, *, digest_date, text, discovery_keys, count, telegram_message_id=None):
        self.digests[str(digest_date)] = {
            "digest_date": digest_date,
            "digest_text": text,
            "discovery_keys": list(discovery_keys),
            "discovery_count": count,
            "telegram_message_id": telegram_message_id,
        }

    async def get_digest(self, digest_date):
        return self.digests.get(str(digest_date))

    # -------------------------------------------------------------------- state
    async def set_state(self, key, value):
        self.state[key] = value

    async def get_state(self, key):
        return self.state.get(key)

    # ------------------------------------------------------------------- cache
    async def cache_get(self, fingerprint):
        return self.cache.get(fingerprint)

    async def cache_put(self, fingerprint, result):
        self.cache.setdefault(fingerprint, result)

    async def record_provider_call(self, *, provider, model_id, ok, error_kind, latency_ms, prompt_tokens, completion_tokens):
        self.provider_calls.append(
            {"provider": provider, "ok": ok, "error_kind": error_kind, "model_id": model_id}
        )

    async def provider_health(self, hours=24):
        summary: dict[str, dict[str, int]] = {}
        for call in self.provider_calls:
            bucket = summary.setdefault(call["provider"], {"successes": 0, "failures": 0})
            bucket["successes" if call["ok"] else "failures"] += 1
        return summary

    async def quota_used_today(self, provider):
        return sum(1 for call in self.provider_calls if call["provider"] == provider)

    async def provider_tokens_last_24h(self, provider):
        return sum(
            int(call.get("prompt_tokens") or 0) + int(call.get("completion_tokens") or 0)
            for call in self.provider_calls
            if call["provider"] == provider
        )

    # ----------------------------------------------------------------- overview
    async def overview(self):
        if self.fail_on_overview:
            raise RuntimeError("simulated database outage")
        unknown = sum(
            1 for row in self.deliveries.values() if row["status"] == DeliveryStatus.UNKNOWN.value
        )
        pending = sum(
            1
            for row in self.deliveries.values()
            if row["status"] in (DeliveryStatus.PENDING.value, DeliveryStatus.RETRY_WAIT.value)
        )
        last = await self.last_successful_run()
        return {
            "sources_total": len(self.sources),
            "sources_unhealthy": sum(
                1
                for row in self.sources.values()
                if row.get("health_status") in ("degraded", "failing")
            ),
            "sources_enabled": len(self.sources),
            "pending_deliveries": pending,
            "unknown_deliveries": unknown,
            "discoveries_total": len(self.discoveries),
            "discoveries_confirmed": sum(
                1 for row in self.discoveries.values() if row.get("verdict") == "CONFIRMED"
            ),
            "last_successful_run_at": last,
            "last_run_id": (await self.last_run_status())[0],
            "last_run_status": (await self.last_run_status())[1],
            "last_backup_at": None,
            "last_backup_status": None,
        }

    async def prune_analysis_cache(self, keep_days=45):
        return 0

    async def prune_provider_usage(self, keep_days=120):
        return 0

    async def save_backup(self, manifest):
        self.saved.append(manifest)
        return str(uuid.uuid4())

    async def latest_backup(self):
        return None


class FakeTelegram:
    """Records messages or raises a prepared failure."""
    def __init__(self, *, error=None, configured: bool = True) -> None:
        self.sent: list[str] = []
        self._error = error
        self._configured = configured

    @property
    def configured(self) -> bool:
        return self._configured

    async def send_message(self, text: str, **_kwargs) -> int:
        if self._error is not None:
            raise self._error
        self.sent.append(text)
        return 1000 + len(self.sent)

    async def get_me(self):
        return {"username": "ai_radar_test_bot"}


class FakeDrive:
    """In-memory Drive that can be told to fail at a specific step."""

    def __init__(
        self,
        *,
        configured: bool = True,
        fail_upload: bool = False,
        corrupt_download: bool = False,
        size_mismatch: bool = False,
    ) -> None:
        from ai_radar.operations.drive import DriveError, DriveFile

        self._drive_error = DriveError
        self._file_cls = DriveFile
        self.configured = configured
        self.fail_upload = fail_upload
        self.corrupt_download = corrupt_download
        self.size_mismatch = size_mismatch
        self.stored: dict[str, bytes] = {}
        self.properties: dict[str, dict[str, str]] = {}
        self.uploaded: list[str] = []
        self.deleted: list[str] = []

    async def upload(
        self,
        name: str,
        data: bytes,
        *,
        mime_type: str = "application/gzip",
        app_properties: dict[str, str] | None = None,
    ):
        if self.fail_upload:
            raise self._drive_error("simulated upload failure")
        file_id = f"file-{len(self.stored) + 1}"
        self.stored[file_id] = data
        self.properties[file_id] = dict(app_properties or {})
        self.uploaded.append(name)
        return self._file_cls(
            id=file_id,
            name=name,
            size=len(data),
            md5="md5",
            app_properties=self.properties[file_id] or None,
        )

    async def metadata(self, file_id: str):
        payload = self.stored.get(file_id, b"")
        size = len(payload) + (10 if self.size_mismatch else 0)
        return self._file_cls(
            id=file_id,
            name="backup",
            size=size,
            md5="md5",
            app_properties=self.properties.get(file_id) or None,
        )

    async def download(self, file_id: str) -> bytes:
        payload = self.stored.get(file_id, b"")
        return payload + b"corrupted" if self.corrupt_download else payload

    async def list_backups(self, name_prefix: str = "ai-radar-backup-"):
        return [
            self._file_cls(
                id=file_id,
                name=name,
                size=len(payload),
                app_properties=self.properties.get(file_id) or None,
            )
            for file_id, (name, payload) in (
                (key, (key, value)) for key, value in self.stored.items()
            )
        ]

    async def delete(self, file_id: str) -> bool:
        self.deleted.append(file_id)
        self.stored.pop(file_id, None)
        return True

    async def access_token(self) -> str:
        return "fake-token"
