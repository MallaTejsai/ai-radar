"""Repository: every SQL statement the system uses, in one auditable place.

Design notes:

* Writes that must not half-happen (discovery + evidence + analysis + pricing) run
  in a single transaction.
* Delivery claims use ``FOR UPDATE SKIP LOCKED`` so two runners can never take the
  same message, and ``UNIQUE (discovery_id, channel)`` makes the intent itself
  idempotent.
* Nothing here stores secrets or full external page bodies.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Iterable, Sequence

from psycopg.types.json import Jsonb

from ..models.analysis import AnalysisResult
from ..models.backup import BackupManifest
from ..models.core import Category, DeliveryStatus, DiscoveryStatus, PricingStatus, RunStatus, SourceTier, Verdict
from ..models.delivery import DeliveryIntent
from ..models.discovery import DiscoveryRecord, Evidence
from ..models.run import RunReport
from ..models.source import SourceSpec, SourceState
from ..utils.logging import get_logger
from ..utils.timeutil import utcnow
from .db import Database

logger = get_logger(__name__)


class Repository:
    """All persistence for AI Radar."""

    def __init__(self, database: Database) -> None:
        self._db = database

    @property
    def database(self) -> Database:
        return self._db

    # ------------------------------------------------------------------ sources
    async def sync_sources(self, specs: Sequence[SourceSpec]) -> int:
        """Upsert the reviewed registry into the database (the state side)."""
        async with self._db.transaction() as cursor:
            for spec in specs:
                await cursor.execute(
                    """
                    insert into sources (source_id, name, category, tier, method, url, enabled,
                                         health_status)
                    values (%s, %s, %s, %s, %s, %s, %s, %s)
                    on conflict (source_id) do update
                       set name = excluded.name,
                           category = excluded.category,
                           tier = excluded.tier,
                           method = excluded.method,
                           url = excluded.url,
                           enabled = excluded.enabled,
                           health_status = case
                               when excluded.enabled = false then 'disabled'
                               when sources.health_status = 'disabled' then 'unknown'
                               else sources.health_status
                           end
                    """,
                    (
                        spec.id,
                        spec.name,
                        spec.category.value,
                        spec.tier.value,
                        spec.method.value,
                        spec.url,
                        spec.enabled,
                        "unknown" if spec.enabled else "disabled",
                    ),
                )
            return len(specs)

    async def load_source_states(self) -> dict[str, SourceState]:
        async with self._db.transaction() as cursor:
            await cursor.execute(
                """
                select source_id, last_checkpoint, last_success_at, last_error,
                       consecutive_failures, health_status, records_collected, etag, last_modified
                from sources
                """
            )
            rows = await cursor.fetchall()
        return {
            row["source_id"]: SourceState(
                source_id=row["source_id"],
                last_checkpoint=row["last_checkpoint"],
                last_success_at=row["last_success_at"],
                last_error=row["last_error"],
                consecutive_failures=row["consecutive_failures"],
                health_status=row["health_status"],
                records_collected=row["records_collected"],
                etag=row["etag"],
                last_modified=row["last_modified"],
            )
            for row in rows
        }

    async def record_source_success(
        self,
        source_id: str,
        *,
        etag: str | None,
        last_modified: str | None,
        entries: int,
        checkpoint: datetime | None = None,
    ) -> None:
        """Advance a checkpoint. Only call this after the records are committed."""
        async with self._db.transaction() as cursor:
            await cursor.execute(
                """
                update sources
                   set last_checkpoint = coalesce(%s, now()),
                       last_success_at = now(),
                       last_error = null,
                       consecutive_failures = 0,
                       health_status = 'healthy',
                       etag = %s,
                       last_modified = %s,
                       records_collected = records_collected + %s
                 where source_id = %s
                """,
                (checkpoint, etag, last_modified, max(0, entries), source_id),
            )

    async def record_source_failure(self, source_id: str, error: str) -> None:
        async with self._db.transaction() as cursor:
            await cursor.execute(
                """
                update sources
                   set last_error = %s,
                       consecutive_failures = consecutive_failures + 1,
                       health_status = case
                           when consecutive_failures + 1 >= 3 then 'failing'
                           else 'degraded'
                       end
                 where source_id = %s
                """,
                (error[:1000], source_id),
            )

    # -------------------------------------------------------------- discoveries
    async def existing_identities(
        self, canonical_keys: Sequence[str], title_fingerprints: Sequence[str]
    ) -> tuple[set[str], dict[str, str]]:
        """Return known canonical keys and fingerprint -> canonical_key mapping."""
        if not canonical_keys and not title_fingerprints:
            return set(), {}
        async with self._db.transaction() as cursor:
            await cursor.execute(
                """
                select canonical_key, title_fingerprint
                  from discoveries
                 where canonical_key = any(%s)
                    or title_fingerprint = any(%s)
                """,
                (list(canonical_keys), list(title_fingerprints)),
            )
            rows = await cursor.fetchall()
        keys = {row["canonical_key"] for row in rows}
        fingerprints = {row["title_fingerprint"]: row["canonical_key"] for row in rows}
        return keys, fingerprints

    async def upsert_discovery(
        self,
        record: DiscoveryRecord,
        analysis: AnalysisResult | None = None,
        *,
        provider_for_analysis: str | None = None,
    ) -> tuple[str, bool]:
        """Insert or refresh a discovery with its evidence and analysis.

        Returns ``(discovery_id, created)``. ``created`` distinguishes a brand new
        record from a refresh, which is what the alert queue uses to avoid
        re-announcing old discoveries.
        """
        async with self._db.transaction() as cursor:
            await cursor.execute(
                """
                insert into discoveries (
                    canonical_key, title_fingerprint, title, category, summary, url,
                    source_id, tier, published_at, discovered_at, last_verified_at,
                    priority_score, priority_label, confidence, status, pricing_status,
                    pricing_restricted, verdict, score_breakdown, verification
                ) values (
                    %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, %s
                )
                on conflict (canonical_key) do update
                   set title = excluded.title,
                       summary = excluded.summary,
                       url = excluded.url,
                       category = excluded.category,
                       tier = excluded.tier,
                       published_at = coalesce(excluded.published_at, discoveries.published_at),
                       last_verified_at = excluded.last_verified_at,
                       priority_score = excluded.priority_score,
                       priority_label = excluded.priority_label,
                       confidence = excluded.confidence,
                       pricing_status = excluded.pricing_status,
                       pricing_restricted = excluded.pricing_restricted,
                       verdict = excluded.verdict,
                       score_breakdown = excluded.score_breakdown,
                       verification = excluded.verification
                returning id, (xmax = 0) as created
                """,
                (
                    record.canonical_key,
                    record.title_fingerprint,
                    record.title,
                    record.category.value,
                    record.summary,
                    record.url,
                    record.source_id,
                    record.tier.value,
                    record.published_at,
                    record.discovered_at,
                    record.last_verified_at or utcnow(),
                    record.priority_score,
                    record.score_breakdown.label if record.score_breakdown else "1/5",
                    record.confidence,
                    record.status.value,
                    record.pricing_status.value,
                    record.pricing_restricted,
                    record.verdict.value,
                    Jsonb(record.score_breakdown.model_dump()) if record.score_breakdown else None,
                    Jsonb(record.verification.model_dump()),
                ),
            )
            row = await cursor.fetchone()
            discovery_id = str(row["id"])
            created = bool(row["created"])

            for item in record.evidence:
                await self._insert_evidence(cursor, discovery_id, item)

            if analysis is not None:
                await cursor.execute(
                    """
                    insert into analysis_results (
                        discovery_id, provider, model_id, prompt_version,
                        input_fingerprint, payload, latency_ms, cached, fallback_reason
                    ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    on conflict (discovery_id, prompt_version) do update
                       set provider = excluded.provider,
                           model_id = excluded.model_id,
                           payload = excluded.payload,
                           latency_ms = excluded.latency_ms,
                           cached = excluded.cached,
                           fallback_reason = excluded.fallback_reason
                    """,
                    (
                        discovery_id,
                        analysis.provider.value,
                        analysis.model_id,
                        analysis.prompt_version,
                        analysis.input_fingerprint,
                        Jsonb(analysis.payload.model_dump(mode="json")),
                        analysis.latency_ms,
                        analysis.cached,
                        analysis.fallback_reason,
                    ),
                )

            await self._record_pricing_change(cursor, discovery_id, record)

        return discovery_id, created

    @staticmethod
    def _fingerprint(title: str) -> str:
        from ..utils.text import title_fingerprint

        return title_fingerprint(title)

    async def _insert_evidence(self, cursor, discovery_id: str, item: Evidence) -> None:
        await cursor.execute(
            """
            insert into discovery_sources (
                discovery_id, source_id, evidence_url, tier, domain, http_status, reachable, checked_at
            ) values (%s, %s, %s, %s, %s, %s, %s, %s)
            on conflict (discovery_id, source_id, evidence_url) do update
               set http_status = excluded.http_status,
                   reachable = excluded.reachable,
                   checked_at = excluded.checked_at
            """,
            (
                discovery_id,
                item.source_id,
                item.url,
                item.tier.value,
                item.domain,
                item.http_status,
                item.reachable,
                item.checked_at,
            ),
        )

    async def _record_pricing_change(
        self, cursor, discovery_id: str, record: DiscoveryRecord
    ) -> None:
        await cursor.execute(
            """
            select pricing_status
              from pricing_history
             where discovery_id = %s
             order by observed_at desc
             limit 1
            """,
            (discovery_id,),
        )
        row = await cursor.fetchone()
        if row and row["pricing_status"] == record.pricing_status.value:
            return
        await cursor.execute(
            """
            insert into pricing_history (discovery_id, pricing_status, restricted, evidence)
            values (%s, %s, %s, %s)
            """,
            (
                discovery_id,
                record.pricing_status.value,
                record.pricing_restricted,
                record.verification.notes[0] if record.verification.notes else "",
            ),
        )

    async def get_discovery(self, canonical_key: str) -> dict[str, Any] | None:
        async with self._db.transaction() as cursor:
            await cursor.execute("select * from discoveries where canonical_key = %s", (canonical_key,))
            return await cursor.fetchone()

    async def get_discovery_context(self, canonical_key: str) -> dict[str, Any] | None:
        """Discovery row plus its analysis and source name, for message rendering."""
        async with self._db.transaction() as cursor:
            await cursor.execute(
                """
                select d.*, a.payload as analysis, s.name as source_name
                  from discoveries d
                  left join analysis_results a on a.discovery_id = d.id
                  left join sources s on s.source_id = d.source_id
                 where d.canonical_key = %s
                 order by a.created_at desc nulls last
                 limit 1
                """,
                (canonical_key,),
            )
            return await cursor.fetchone()

    async def mark_notified(self, discovery_id: str) -> None:
        async with self._db.transaction() as cursor:
            await cursor.execute(
                "update discoveries set status = 'notified' where id = %s and status = 'verified'",
                (discovery_id,),
            )

    async def queue_delivery(
        self, discovery_id: str, message_text: str, message_hash: str
    ) -> str | None:
        """Persist delivery intent. Returns the delivery id, or None if it existed."""
        async with self._db.transaction() as cursor:
            await cursor.execute(
                """
                insert into deliveries (discovery_id, message_text, message_hash, status)
                values (%s, %s, %s, 'PENDING')
                on conflict (discovery_id, channel) do nothing
                returning id
                """,
                (discovery_id, message_text, message_hash),
            )
            row = await cursor.fetchone()
            return str(row["id"]) if row else None

    async def has_delivery(self, discovery_id: str) -> bool:
        async with self._db.transaction() as cursor:
            await cursor.execute(
                "select 1 from deliveries where discovery_id = %s limit 1", (discovery_id,)
            )
            return bool(await cursor.fetchone())

    async def discovery_for_delivery(self, delivery_id: str) -> dict[str, Any] | None:
        """Load the discovery row plus analysis needed to render a message."""
        async with self._db.transaction() as cursor:
            await cursor.execute(
                """
                select d.*, a.payload as analysis, a.provider as analysis_provider,
                       s.name as source_name
                  from deliveries dl
                  join discoveries d on d.id = dl.discovery_id
                  left join analysis_results a
                         on a.discovery_id = d.id
                  left join sources s on s.source_id = d.source_id
                 where dl.id = %s
                 order by a.created_at desc nulls last
                 limit 1
                """,
                (delivery_id,),
            )
            return await cursor.fetchone()

    # ---------------------------------------------------------------- deliveries
    async def claim_deliveries(
        self, worker_id: str, limit: int, *, max_age_hours: int | None = None
    ) -> list[DeliveryIntent]:
        """Atomically claim up to ``limit`` dispatchable deliveries.

        ``max_age_hours`` is re-checked here, not only when the intent was
        queued: a backlog drained slowly must never deliver news that is older
        than the alert window just because it was fresh when queued. Rows that
        fail the age check stay unclaimed and are expired by
        :meth:`expire_stale_deliveries`.
        """
        async with self._db.transaction() as cursor:
            await cursor.execute(
                """
                with claimed as (
                    select dl.id
                      from deliveries dl
                      join discoveries d on d.id = dl.discovery_id
                     where dl.status in ('PENDING', 'RETRY_WAIT')
                       and dl.next_attempt_at <= now()
                       and (%s::int is null or d.published_at is null
                            or d.published_at >= now() - make_interval(hours => %s::int))
                     order by dl.created_at
                     for update of dl skip locked
                     limit %s
                )
                update deliveries d
                   set status = 'SENDING',
                       claimed_by = %s,
                       claimed_at = now(),
                       attempts = d.attempts + 1
                  from claimed
                 where d.id = claimed.id
                returning d.id, d.discovery_id, d.channel, d.status, d.attempts,
                          d.message_text, d.message_hash, d.telegram_message_id,
                          d.last_error, d.claimed_by, d.claimed_at, d.created_at
                """,
                (max_age_hours, max_age_hours, limit, worker_id),
            )
            rows = await cursor.fetchall()

        return [
            DeliveryIntent(
                id=str(row["id"]),
                discovery_key=str(row["discovery_id"]),
                channel=row["channel"],
                status=DeliveryStatus(row["status"]),
                attempts=row["attempts"],
                message_text=row["message_text"],
                message_hash=row["message_hash"],
                telegram_message_id=row["telegram_message_id"],
                last_error=row["last_error"],
                claimed_by=row["claimed_by"],
                claimed_at=row["claimed_at"],
                created_at=row["created_at"],
            )
            for row in rows
        ]

    async def mark_delivery_sent(self, delivery_id: str, telegram_message_id: int) -> None:
        async with self._db.transaction() as cursor:
            await cursor.execute(
                """
                update deliveries
                   set status = 'SENT', telegram_message_id = %s, last_error = null,
                       claimed_by = null, claimed_at = null
                 where id = %s
                """,
                (telegram_message_id, delivery_id),
            )

    async def mark_delivery_retry(
        self, delivery_id: str, error: str, *, delay_seconds: int
    ) -> None:
        async with self._db.transaction() as cursor:
            await cursor.execute(
                """
                update deliveries
                   set status = 'RETRY_WAIT',
                       last_error = %s,
                       claimed_by = null,
                       claimed_at = null,
                       next_attempt_at = now() + make_interval(secs => %s)
                 where id = %s
                """,
                (error[:1000], delay_seconds, delivery_id),
            )

    async def mark_delivery_unknown(self, delivery_id: str, error: str) -> None:
        """Outcome genuinely unknown (e.g. timeout after the request was sent).

        The row is never retried automatically: a duplicate alert is worse than a
        missing one, and a human can inspect it from the heartbeat report.
        """
        async with self._db.transaction() as cursor:
            await cursor.execute(
                """
                update deliveries
                   set status = 'UNKNOWN', last_error = %s, claimed_by = null, claimed_at = null
                 where id = %s
                """,
                (error[:1000], delivery_id),
            )

    async def defer_delivery(self, delivery_id: str, *, delay_seconds: int, reason: str) -> None:
        """Hand a claimed delivery back to the queue without consuming it."""
        async with self._db.transaction() as cursor:
            await cursor.execute(
                """
                update deliveries
                   set status = 'RETRY_WAIT',
                       last_error = %s,
                       claimed_by = null,
                       claimed_at = null,
                       next_attempt_at = now() + make_interval(secs => %s)
                 where id = %s
                """,
                (reason[:1000], max(1, delay_seconds), delivery_id),
            )

    async def expire_stale_deliveries(self, max_age_hours: int) -> int:
        """Close intents whose discovery is older than the alert window.

        They are never sent later. ``UNKNOWN`` is used (rather than silently
        deleting the row) because it keeps the decision auditable and shows up
        in the heartbeat for review.
        """
        async with self._db.transaction() as cursor:
            await cursor.execute(
                """
                update deliveries dl
                   set status = 'UNKNOWN',
                       last_error = 'expired: discovery older than the alert window before delivery',
                       claimed_by = null,
                       claimed_at = null
                  from discoveries d
                 where d.id = dl.discovery_id
                   and dl.status in ('PENDING', 'RETRY_WAIT')
                   and d.published_at is not null
                   and d.published_at < now() - make_interval(hours => %s)
                returning dl.id
                """,
                (max_age_hours,),
            )
            rows = await cursor.fetchall()
        return len(rows)

    async def release_stale_claims(self, older_than_minutes: int = 15) -> int:
        """Return abandoned SENDING rows to a reviewable state.

        A runner killed between claim and result would otherwise leave rows in
        SENDING forever. They become UNKNOWN (not retried) and are reported.
        """
        async with self._db.transaction() as cursor:
            await cursor.execute(
                """
                update deliveries
                   set status = 'UNKNOWN',
                       last_error = coalesce(last_error, 'runner died while sending'),
                       claimed_by = null,
                       claimed_at = null
                 where status = 'SENDING'
                   and claimed_at < now() - make_interval(mins => %s)
                returning id
                """,
                (older_than_minutes,),
            )
            rows = await cursor.fetchall()
        return len(rows)

    async def delivery_counts(self) -> dict[str, int]:
        async with self._db.transaction() as cursor:
            await cursor.execute("select status, count(*) as n from deliveries group by status")
            rows = await cursor.fetchall()
        return {row["status"]: int(row["n"]) for row in rows}

    # ------------------------------------------------------------- workflow runs
    async def start_run(self, report: RunReport) -> None:
        async with self._db.transaction() as cursor:
            await cursor.execute(
                """
                insert into workflow_runs (run_id, started_at, status, trigger, recovered_missed_runs)
                values (%s, %s, %s, %s, %s)
                on conflict (run_id) do nothing
                """,
                (report.run_id, report.started_at, report.status.value, report.trigger,
                 report.recovered_missed_runs),
            )

    async def finish_run(self, report: RunReport) -> None:
        async with self._db.transaction() as cursor:
            await cursor.execute(
                """
                update workflow_runs
                   set finished_at = %s,
                       status = %s,
                       sources_attempted = %s,
                       sources_failed = %s,
                       entries_collected = %s,
                       after_dedup = %s,
                       verified = %s,
                       qualified = %s,
                       persisted_new = %s,
                       alerts_sent = %s,
                       recovered_missed_runs = %s,
                       duration_seconds = %s,
                       report = %s
                 where run_id = %s
                """,
                (
                    report.finished_at or utcnow(),
                    report.status.value,
                    report.sources_attempted,
                    report.sources_failed,
                    report.entries_collected,
                    report.after_dedup,
                    report.verified,
                    report.qualified,
                    report.persisted_new,
                    report.alerts_sent,
                    report.recovered_missed_runs,
                    report.duration_seconds,
                    Jsonb(report.model_dump(mode="json")),
                    report.run_id,
                ),
            )

    async def last_successful_run(self) -> datetime | None:
        async with self._db.transaction() as cursor:
            await cursor.execute(
                """
                select max(coalesce(finished_at, started_at)) as last
                  from workflow_runs
                 where status in ('completed', 'degraded')
                """
            )
            row = await cursor.fetchone()
        return row["last"] if row else None

    async def recent_run_starts(self, hours: int = 48) -> list[datetime]:
        async with self._db.transaction() as cursor:
            await cursor.execute(
                """
                select started_at
                  from workflow_runs
                 where status in ('completed', 'degraded')
                   and started_at > now() - make_interval(hours => %s)
                 order by started_at desc
                """,
                (hours,),
            )
            rows = await cursor.fetchall()
        return [row["started_at"] for row in rows]

    async def last_run_status(self) -> tuple[str | None, str | None]:
        async with self._db.transaction() as cursor:
            await cursor.execute(
                "select run_id, status from workflow_runs order by started_at desc limit 1"
            )
            row = await cursor.fetchone()
        return (row["run_id"], row["status"]) if row else (None, None)

    # ------------------------------------------------------------------- digest
    async def discoveries_for_digest(
        self,
        start: datetime,
        end: datetime,
        *,
        min_confidence: float,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        async with self._db.transaction() as cursor:
            await cursor.execute(
                """
                select d.*, a.payload as analysis, s.name as source_name,
                       (d.status = 'notified') as already_alerted
                  from discoveries d
                  left join analysis_results a on a.discovery_id = d.id
                  left join sources s on s.source_id = d.source_id
                 where d.verdict = 'CONFIRMED'
                   and d.status <> 'archived'
                   and d.confidence >= %s
                   and d.discovered_at >= %s
                   and d.discovered_at < %s
                 order by d.priority_score desc, d.discovered_at desc
                 limit %s
                """,
                (min_confidence, start, end, limit),
            )
            return list(await cursor.fetchall())

    async def save_digest(
        self,
        *,
        digest_date,
        text: str,
        discovery_keys: Sequence[str],
        count: int,
        telegram_message_id: int | None = None,
    ) -> None:
        async with self._db.transaction() as cursor:
            await cursor.execute(
                """
                insert into daily_digests (digest_date, digest_text, discovery_count,
                                           discovery_keys, telegram_message_id)
                values (%s, %s, %s, %s, %s)
                on conflict (digest_date) do update
                   set digest_text = excluded.digest_text,
                       discovery_count = excluded.discovery_count,
                       discovery_keys = excluded.discovery_keys,
                       telegram_message_id = coalesce(
                           excluded.telegram_message_id, daily_digests.telegram_message_id)
                """,
                (digest_date, text, count, Jsonb(list(discovery_keys)), telegram_message_id),
            )

    async def get_digest(self, digest_date) -> dict[str, Any] | None:
        async with self._db.transaction() as cursor:
            await cursor.execute(
                "select * from daily_digests where digest_date = %s", (digest_date,)
            )
            return await cursor.fetchone()

    # ------------------------------------------------------------------- backups
    async def save_backup(self, manifest: BackupManifest) -> str:
        async with self._db.transaction() as cursor:
            await cursor.execute(
                """
                insert into backup_manifests (
                    backup_id, schema_version, created_at, table_counts, file_name,
                    uncompressed_bytes, compressed_bytes, sha256, drive_file_id,
                    status, validated_at, restore_tested_at, notes
                ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                on conflict (backup_id) do update
                   set status = excluded.status,
                       drive_file_id = coalesce(excluded.drive_file_id,
                                                backup_manifests.drive_file_id),
                       validated_at = excluded.validated_at,
                       restore_tested_at = excluded.restore_tested_at,
                       notes = excluded.notes,
                       sha256 = excluded.sha256,
                       compressed_bytes = excluded.compressed_bytes
                returning id
                """,
                (
                    manifest.backup_id,
                    manifest.schema_version,
                    manifest.created_at,
                    Jsonb(manifest.record_counts),
                    manifest.file_name,
                    manifest.uncompressed_bytes,
                    manifest.compressed_bytes,
                    manifest.sha256_checksum,
                    manifest.google_drive_file_id,
                    manifest.status,
                    manifest.validated_at,
                    manifest.restore_tested_at,
                    Jsonb(manifest.notes),
                ),
            )
            row = await cursor.fetchone()
        return str(row["id"])

    async def latest_backup(self) -> dict[str, Any] | None:
        async with self._db.transaction() as cursor:
            await cursor.execute(
                "select * from backup_manifests order by created_at desc limit 1"
            )
            return await cursor.fetchone()

    # -------------------------------------------------------------------- state
    async def set_state(self, key: str, value: dict[str, Any]) -> None:
        async with self._db.transaction() as cursor:
            await cursor.execute(
                """
                insert into system_state (key, value) values (%s, %s)
                on conflict (key) do update set value = excluded.value, updated_at = now()
                """,
                (key, Jsonb(value)),
            )

    async def get_state(self, key: str) -> dict[str, Any] | None:
        async with self._db.transaction() as cursor:
            await cursor.execute("select value from system_state where key = %s", (key,))
            row = await cursor.fetchone()
        return row["value"] if row else None

    # ------------------------------------------------------- analysis cache/metrics
    async def cache_get(self, fingerprint: str) -> AnalysisResult | None:
        async with self._db.transaction() as cursor:
            await cursor.execute(
                "select provider, model_id, prompt_version, payload from analysis_cache where fingerprint = %s",
                (fingerprint,),
            )
            row = await cursor.fetchone()
        if not row:
            return None
        from ..models.analysis import AnalysisPayload
        from ..models.core import Provider

        try:
            return AnalysisResult(
                discovery_key=fingerprint,
                provider=Provider(row["provider"]),
                model_id=row["model_id"],
                payload=AnalysisPayload.model_validate(row["payload"]),
                prompt_version=row["prompt_version"],
                input_fingerprint=fingerprint,
                latency_ms=0,
                cached=True,
            )
        except (ValueError, TypeError) as exc:
            logger.warning("analysis_cache_row_invalid", error=str(exc))
            return None

    async def cache_put(self, fingerprint: str, result: AnalysisResult) -> None:
        async with self._db.transaction() as cursor:
            await cursor.execute(
                """
                insert into analysis_cache (fingerprint, provider, model_id, prompt_version, payload)
                values (%s, %s, %s, %s, %s)
                on conflict (fingerprint) do nothing
                """,
                (
                    fingerprint,
                    result.provider.value,
                    result.model_id,
                    result.prompt_version,
                    Jsonb(result.payload.model_dump(mode="json")),
                ),
            )

    async def record_provider_call(
        self,
        *,
        provider: str,
        model_id: str,
        ok: bool,
        error_kind: str | None,
        latency_ms: int,
        prompt_tokens: int | None,
        completion_tokens: int | None,
    ) -> None:
        async with self._db.transaction() as cursor:
            await cursor.execute(
                """
                insert into provider_usage (provider, model_id, ok, error_kind, latency_ms,
                                            prompt_tokens, completion_tokens)
                values (%s, %s, %s, %s, %s, %s, %s)
                """,
                (provider, model_id, ok, error_kind, latency_ms, prompt_tokens, completion_tokens),
            )

    async def provider_health(self, hours: int = 24) -> dict[str, dict[str, int]]:
        async with self._db.transaction() as cursor:
            await cursor.execute(
                """
                select provider,
                       count(*) filter (where ok) as successes,
                       count(*) filter (where not ok) as failures
                  from provider_usage
                 where occurred_at > now() - make_interval(hours => %s)
                 group by provider
                """,
                (hours,),
            )
            rows = await cursor.fetchall()
        return {
            row["provider"]: {
                "successes": int(row["successes"]),
                "failures": int(row["failures"]),
            }
            for row in rows
        }

    async def provider_tokens_last_24h(self, provider: str) -> int:
        """Billed tokens for a provider in the trailing 24 hours.

        Rolling rather than calendar-day on purpose: free.ai's reset timezone is not
        documented, and a rolling window can only under-spend the pool.
        """
        async with self._db.transaction() as cursor:
            await cursor.execute(
                """
                select coalesce(
                           sum(coalesce(prompt_tokens, 0) + coalesce(completion_tokens, 0)), 0
                       ) as n
                from provider_usage
                where provider = %s
                  and occurred_at > now() - interval '24 hours'
                """,
                (provider,),
            )
            row = await cursor.fetchone()
        return int(row["n"]) if row else 0

    async def quota_used_today(self, provider: str) -> int:
        async with self._db.transaction() as cursor:
            await cursor.execute(
                """
                select count(*) as n
                  from provider_usage
                 where provider = %s
                   and occurred_at >= date_trunc('day', now())
                """,
                (provider,),
            )
            row = await cursor.fetchone()
        return int(row["n"]) if row else 0

    # ------------------------------------------------------------------ overview
    async def overview(self) -> dict[str, Any]:
        """Aggregate facts for the heartbeat snapshot."""
        async with self._db.transaction() as cursor:
            await cursor.execute(
                """
                select count(*) as total,
                       count(*) filter (where health_status in ('degraded', 'failing')) as unhealthy,
                       count(*) filter (where enabled) as enabled
                  from sources
                """
            )
            sources = await cursor.fetchone()

            await cursor.execute(
                """
                select count(*) filter (where status in ('PENDING', 'RETRY_WAIT')) as pending,
                       count(*) filter (where status = 'UNKNOWN') as unknown
                  from deliveries
                """
            )
            deliveries = await cursor.fetchone()

            await cursor.execute(
                """
                select count(*) as total,
                       count(*) filter (where verdict = 'CONFIRMED') as confirmed
                  from discoveries
                """
            )
            discoveries = await cursor.fetchone()

        last_run = await self.last_successful_run()
        run_id, run_status = await self.last_run_status()
        latest_backup = await self.latest_backup()

        return {
            "sources_total": int(sources["total"]),
            "sources_unhealthy": int(sources["unhealthy"]),
            "sources_enabled": int(sources["enabled"]),
            "pending_deliveries": int(deliveries["pending"]),
            "unknown_deliveries": int(deliveries["unknown"]),
            "discoveries_total": int(discoveries["total"]),
            "discoveries_confirmed": int(discoveries["confirmed"]),
            "last_successful_run_at": last_run,
            "last_run_id": run_id,
            "last_run_status": run_status,
            "last_backup_at": latest_backup["created_at"] if latest_backup else None,
            "last_backup_status": latest_backup["status"] if latest_backup else None,
        }

    # ------------------------------------------------------------ maintenance
    async def prune_analysis_cache(self, keep_days: int = 45) -> int:
        async with self._db.transaction() as cursor:
            await cursor.execute(
                "delete from analysis_cache where created_at < now() - make_interval(days => %s)",
                (keep_days,),
            )
            return cursor.rowcount or 0

    async def prune_provider_usage(self, keep_days: int = 120) -> int:
        async with self._db.transaction() as cursor:
            await cursor.execute(
                "delete from provider_usage where occurred_at < now() - make_interval(days => %s)",
                (keep_days,),
            )
            return cursor.rowcount or 0
