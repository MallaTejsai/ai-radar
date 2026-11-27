"""The research pipeline: COLLECT -> NORMALIZE -> FILTER -> CANONICALIZE ->
DEDUPLICATE -> VERIFY -> AI ANALYSIS -> CONFIDENCE CHECK -> PRIORITY SCORING ->
PERSIST -> NOTIFY.

Contract details that the tests pin down:

* No collector notifies anything. Collection returns data; only the dispatcher
  talks to Telegram, and only for rows already committed to the database.
* A source checkpoint advances only after the collected records are durably
  persisted, so a crash mid-run re-collects rather than loses.
* Low-confidence discoveries are discarded completely: not stored, not queued,
  not digested. They are only counted.
* Two concurrent runs cannot both proceed: a PostgreSQL advisory lock gates the
  whole run.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ..collectors import collect_all
from ..intelligence import IntelligenceEngine
from ..models.analysis import AnalysisResult
from ..models.core import DiscoveryStatus, RunStatus, Verdict
from ..models.discovery import DiscoveryRecord, NormalizedEntry, ScoreBreakdown
from ..models.run import RunReport
from ..models.source import SourceSpec
from ..notifications import DeliveryDispatcher
from ..processing import dedupe_batch, filter_entries, score_priority
from ..processing.normalize import normalize_batch
from ..settings import AppConfig
from ..storage import Repository, RunLock
from ..utils.logging import get_logger
from ..utils.net import SafeHttpClient
from ..utils.timeutil import age_days, utcnow
from ..verification import EvidenceVerifier, VerificationOutcome
from .heartbeat import HeartbeatReporter
from .recovery import RecoveryPlan, plan_recovery

logger = get_logger(__name__)

LOCK_NAME = "ai_radar_pipeline"


@dataclass(slots=True)
class Candidate:
    entry: NormalizedEntry
    verification: VerificationOutcome
    analysis: AnalysisResult | None = None
    breakdown: ScoreBreakdown | None = None


@dataclass(slots=True)
class _StageCounts:
    collected: int = 0
    filtered_out: int = 0
    duplicates: int = 0
    already_known: int = 0
    unverified: int = 0
    low_confidence: int = 0
    per_source_persisted: dict[str, int] = field(default_factory=dict)


class ResearchPipeline:
    def __init__(
        self,
        *,
        config: AppConfig,
        repository: Repository,
        http: SafeHttpClient,
        engine: IntelligenceEngine,
        verifier: EvidenceVerifier,
        dispatcher: DeliveryDispatcher | None,
        heartbeat: HeartbeatReporter,
    ) -> None:
        self.config = config
        self.repo = repository
        self.http = http
        self.engine = engine
        self.verifier = verifier
        self.dispatcher = dispatcher
        self.heartbeat = heartbeat

    # ------------------------------------------------------------------- public
    async def run(self, *, trigger: str = "schedule") -> RunReport:
        started = utcnow()
        run_id = f"{started:%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:6]}"
        report = RunReport(
            run_id=run_id,
            started_at=started,
            trigger=trigger,
            recovery_window_hours=self.config.recovery_window_hours,
        )

        lock = RunLock(
            self.repo.database,
            LOCK_NAME,
            timeout_seconds=self.config.settings.run_lock_timeout_seconds,
        )
        async with lock:
            if not lock.acquired:
                report.status = RunStatus.SKIPPED_LOCKED
                report.finished_at = utcnow()
                report.duration_seconds = (report.finished_at - started).total_seconds()
                await self.repo.start_run(report)
                await self.repo.finish_run(report)
                logger.warning("pipeline_skipped_locked", run_id=run_id)
                return report

            await self.repo.start_run(report)
            try:
                await self._execute(report)
            except Exception as exc:  # noqa: BLE001 - the run row must record the failure
                report.status = RunStatus.FAILED
                report.add_error(f"{type(exc).__name__}: {exc}")
                logger.exception("pipeline_failed", run_id=run_id)
            finally:
                report.finished_at = utcnow()
                report.duration_seconds = (report.finished_at - started).total_seconds()
                if report.status is RunStatus.RUNNING:
                    report.status = RunStatus.COMPLETED
                await self.repo.finish_run(report)

            # Heartbeat is published only after the run row is safely written.
            try:
                snapshot = await self.heartbeat.snapshot(note=f"run {run_id} {report.status.value}")
                await self.heartbeat.publish(
                    snapshot, success=report.status is not RunStatus.FAILED
                )
            except Exception as exc:  # noqa: BLE001 - monitoring must not fail the run
                logger.error("heartbeat_publish_failed", error=str(exc))

        return report

    # ------------------------------------------------------------------ stages
    async def _execute(self, report: RunReport) -> None:
        cfg = self.config
        counts = _StageCounts()

        await self.repo.sync_sources(cfg.sources.sources)
        states = await self.repo.load_source_states()

        plan = plan_recovery(
            last_successful_run=await self.repo.last_successful_run(),
            successful_runs=await self.repo.recent_run_starts(48),
            window_hours=cfg.recovery_window_hours,
        )
        report.recovered_missed_runs = plan.missed_count
        logger.info("recovery_plan", summary=plan.summary(), widen=plan.widen_factor)

        # 1. COLLECT ---------------------------------------------------------
        specs = [
            spec.model_copy(update={"max_items": plan.effective_max_items(spec.max_items)})
            for spec in cfg.sources.enabled()
        ]
        results = await collect_all(
            specs,
            states,
            self.http,
            concurrency=cfg.settings.collect_concurrency,
            github_token=cfg.settings.github_token,
        )
        report.sources_attempted = len(results)

        by_id: dict[str, SourceSpec] = {spec.id: spec for spec in specs}
        normalized: list[NormalizedEntry] = []

        # 2. NORMALIZE (+ per-source isolation) ------------------------------
        for source_id, result in results.items():
            if not result.ok:
                report.sources_failed += 1
                report.add_error(f"{source_id}: {result.error}")
                await self.repo.record_source_failure(source_id, result.error or "unknown error")
                continue
            if result.note:
                logger.info("source_note", source_id=source_id, note=result.note)
            entries, rejected = normalize_batch(result.entries, by_id[source_id])
            if rejected:
                logger.info("source_entries_rejected", source_id=source_id, rejected=rejected)
            normalized.extend(entries)

        counts.collected = len(normalized)
        report.entries_collected = counts.collected

        if not normalized:
            report.status = RunStatus.DEGRADED if report.sources_failed else RunStatus.COMPLETED
            await self._queue_and_dispatch(report)
            logger.info("pipeline_no_entries")
            return

        # 3. FILTER ----------------------------------------------------------
        filtered = filter_entries(
            normalized,
            signals=cfg.scoring.signals,
            max_age_days=cfg.settings.max_age_days,
        )
        counts.filtered_out = filtered.rejected_count
        if counts.filtered_out:
            logger.info("filter_rejections", reasons=filtered.reason_counts())

        # 4. CANONICALIZE + 5. DEDUPLICATE (in-batch) ------------------------
        batch = dedupe_batch(filtered.kept)
        counts.duplicates = batch.removed

        # 5b. DEDUPLICATE against stored memory -------------------------------
        fresh = await self._drop_known(batch.unique, counts)
        report.after_dedup = len(fresh)

        if not fresh:
            logger.info("pipeline_nothing_new", collected=counts.collected)
            await self._advance_checkpoints(report, results)
            # Pending/retry-wait deliveries from earlier runs still get a chance.
            await self._queue_and_dispatch(report)
            report.status = RunStatus.COMPLETED
            return

        # 6. VERIFY ----------------------------------------------------------
        candidates = await self._verify(fresh, batch.merged, counts, report)
        report.verified = len(candidates)
        # Counters must be recorded on every exit path, not only the happy one.
        report.discarded_unverified = counts.unverified
        if not candidates:
            await self._advance_checkpoints(report, results)
            await self._queue_and_dispatch(report)
            report.status = RunStatus.DEGRADED
            return

        # 7. CONFIDENCE CHECK (discard completely, and before spending money) --
        # Confidence is fully determined by verification, so filtering here
        # loses no information. Analysing first meant a paid provider could be
        # called for a candidate that was guaranteed to be thrown away, and the
        # paid result then had no discovery row to be attributed to.
        threshold = cfg.confidence_threshold
        candidates = [
            candidate
            for candidate in candidates
            if self._passes_confidence(candidate, threshold, counts, report)
        ]
        report.discarded_low_confidence = counts.low_confidence
        report.discarded_unverified = counts.unverified
        if not candidates:
            await self._advance_checkpoints(report, results)
            await self._queue_and_dispatch(report)
            report.status = RunStatus.COMPLETED
            return

        # 8. AI ANALYSIS -----------------------------------------------------
        analysed = await self._analyse(candidates, report)
        survivors = [candidate for candidate in analysed if candidate.analysis is not None]
        for candidate in analysed:
            if candidate.analysis is None:
                report.record_discard("analysis_failed")

        # 9. PRIORITY SCORING ------------------------------------------------
        for candidate in survivors:
            candidate.breakdown = self._score(candidate)
            if candidate.breakdown.total >= cfg.min_priority:
                report.qualified += 1

        # 10. PERSIST --------------------------------------------------------
        persisted_cleanly = await self._persist(survivors, counts, report)

        # 11. Advance checkpoints only now, and only if every write succeeded,
        #     so a failed write is re-collected instead of being skipped forever.
        if persisted_cleanly:
            await self._advance_checkpoints(report, results)
        else:
            logger.error(
                "checkpoints_not_advanced",
                reason="at least one discovery failed to persist",
            )
            report.status = RunStatus.DEGRADED

        # 12. NOTIFY ---------------------------------------------------------
        await self._queue_and_dispatch(report)

        report.status = (
            RunStatus.DEGRADED
            if (report.errors or report.sources_failed)
            else RunStatus.COMPLETED
        )
        report.ai_provider_counts = dict(self.engine.stats.by_provider)
        report.ai_errors = sum(self.engine.stats.errors.values())

    # ------------------------------------------------------------------ helpers
    async def _drop_known(
        self, entries: list[NormalizedEntry], counts: _StageCounts
    ) -> list[NormalizedEntry]:
        known_keys, known_fingerprints = await self.repo.existing_identities(
            [entry.canonical_key for entry in entries],
            [entry.title_fingerprint for entry in entries],
        )
        fresh: list[NormalizedEntry] = []
        for entry in entries:
            if entry.canonical_key in known_keys or entry.title_fingerprint in known_fingerprints:
                counts.already_known += 1
                continue
            fresh.append(entry)
        if counts.already_known:
            logger.info("existing_discoveries_skipped", count=counts.already_known)
        return fresh

    async def _verify(
        self,
        entries: list[NormalizedEntry],
        merged: dict[str, list[NormalizedEntry]],
        counts: _StageCounts,
        report: RunReport,
    ) -> list[Candidate]:
        semaphore = asyncio.Semaphore(self.config.settings.verify_concurrency)

        async def verify_one(entry: NormalizedEntry) -> Candidate | Exception:
            async with semaphore:
                try:
                    outcome = await self.verifier.verify(entry, merged.get(entry.canonical_key, []))
                    return Candidate(entry=entry, verification=outcome)
                except Exception as exc:  # noqa: BLE001 - isolate one entry
                    return exc

        outcomes = await asyncio.gather(*(verify_one(entry) for entry in entries))
        candidates: list[Candidate] = []
        for outcome in outcomes:
            if isinstance(outcome, Exception):
                counts.unverified += 1
                report.add_error(f"verify failed: {type(outcome).__name__}: {outcome}")
                continue
            if outcome.verification.verdict in (Verdict.CONFIRMED, Verdict.PARTIAL):
                candidates.append(outcome)
            else:
                counts.unverified += 1
                logger.info(
                    "discovery_discarded_unverified",
                    key=outcome.entry.canonical_key,
                    verdict=outcome.verification.verdict.value,
                )
        return candidates

    def _passes_confidence(
        self,
        candidate: Candidate,
        threshold: float,
        counts: _StageCounts,
        report: RunReport,
    ) -> bool:
        if candidate.verification.confidence.total >= threshold:
            return True
        counts.low_confidence += 1
        report.record_discard("low_confidence")
        logger.info(
            "discovery_discarded_low_confidence",
            key=candidate.entry.canonical_key,
            confidence=candidate.verification.confidence.total,
            threshold=threshold,
        )
        return False

    async def _analyse(self, candidates: list[Candidate], report: RunReport) -> list[Candidate]:
        cap = self.config.settings.max_analyses_per_run
        epoch = datetime.min.replace(tzinfo=timezone.utc)
        ordered = sorted(
            candidates, key=lambda item: item.entry.published_at or epoch, reverse=True
        )
        selected = ordered[:cap]
        if len(ordered) > cap:
            skipped = len(ordered) - cap
            for _ in range(skipped):
                report.record_discard("analysis_cap")
            logger.warning(
                "analysis_cap_reached",
                eligible=len(ordered),
                analysed=cap,
                note="remaining discoveries stay unpersisted and will be recollected next run",
            )

        semaphore = asyncio.Semaphore(self.config.settings.ai_concurrency)

        async def analyse_one(candidate: Candidate) -> Candidate:
            async with semaphore:
                source = self.config.sources.by_id().get(candidate.entry.source_id)
                try:
                    candidate.analysis = await self.engine.analyze(
                        candidate.entry, source.name if source else candidate.entry.source_id
                    )
                except Exception as exc:  # noqa: BLE001
                    report.add_error(f"analysis failed for {candidate.entry.canonical_key}: {exc}")
                    candidate.analysis = None
                return candidate

        return list(await asyncio.gather(*(analyse_one(item) for item in selected)))

    def _score(self, candidate: Candidate) -> ScoreBreakdown:
        cfg = self.config
        entry = candidate.entry
        return score_priority(
            category=entry.category,
            text=f"{entry.title} {entry.summary}",
            published_at=entry.published_at,
            pricing_status=candidate.verification.pricing.status,
            verdict=candidate.verification.verdict,
            tiers=candidate.verification.tiers,
            distinct_domains=candidate.verification.distinct_domains,
            config=cfg.scoring,
        )

    async def _persist(
        self, survivors: list[Candidate], counts: _StageCounts, report: RunReport
    ) -> bool:
        """Write every survivor. Returns False when any row failed to persist."""
        cfg = self.config
        all_ok = True
        for candidate in survivors:
            entry = candidate.entry
            breakdown = candidate.breakdown or self._score(candidate)
            record = DiscoveryRecord(
                canonical_key=entry.canonical_key,
                title=entry.title,
                category=entry.category,
                summary=(candidate.analysis.payload.summary if candidate.analysis else "") or entry.summary[:2000],
                url=entry.url,
                source_id=entry.source_id,
                tier=entry.tier,
                published_at=entry.published_at,
                last_verified_at=utcnow(),
                priority_score=breakdown.total,
                confidence=candidate.verification.confidence.total,
                status=DiscoveryStatus.VERIFIED,
                pricing_status=candidate.verification.pricing.status,
                pricing_restricted=candidate.verification.pricing.restricted,
                verdict=candidate.verification.verdict,
                evidence=candidate.verification.evidence,
                verification=candidate.verification.detail,
                score_breakdown=breakdown,
                analysis=candidate.analysis.payload if candidate.analysis else None,
            )
            try:
                # ``analysis`` is passed through verbatim, so a successful paid
                # result keeps its provider attribution in ``analysis_results``.
                discovery_id, created = await self.repo.upsert_discovery(
                    record, candidate.analysis
                )
            except Exception as exc:  # noqa: BLE001 - one bad row must not lose the batch
                all_ok = False
                report.record_discard("persist_failed")
                report.add_error(f"persist failed for {entry.canonical_key}: {exc}")
                logger.error(
                    "discovery_persist_failed",
                    key=entry.canonical_key,
                    provider=candidate.analysis.provider.value if candidate.analysis else None,
                    error=str(exc),
                )
                continue

            counts.per_source_persisted[entry.source_id] = (
                counts.per_source_persisted.get(entry.source_id, 0) + 1
            )
            if created:
                report.persisted_new += 1
            else:
                report.persisted_updated += 1

            if not created or self.dispatcher is None:
                continue
            if not record.qualifies_for_alert(cfg.min_priority, cfg.confidence_threshold):
                continue
            age = age_days(entry.published_at)
            if age is not None and age > (cfg.alert_max_age_hours / 24.0):
                report.record_discard("stale_alert")
                logger.info(
                    "alert_suppressed_stale",
                    key=entry.canonical_key,
                    age_hours=round(age * 24, 1),
                )
                continue
            row = await self.repo.get_discovery_context(entry.canonical_key)
            if row is None:
                continue
            await self.dispatcher.queue(discovery_id, row)
        return all_ok

    async def _advance_checkpoints(self, report: RunReport, results) -> None:
        for source_id, result in results.items():
            if not result.ok:
                continue
            try:
                await self.repo.record_source_success(
                    source_id,
                    etag=result.etag,
                    last_modified=result.last_modified,
                    entries=len(result.entries),
                    checkpoint=utcnow(),
                )
                report.checkpoints_advanced += 1
            except Exception as exc:  # noqa: BLE001
                report.add_error(f"checkpoint update failed for {source_id}: {exc}")

    async def _queue_and_dispatch(self, report: RunReport) -> None:
        """Drain up to the configured cap, one claim at a time.

        A single ``dispatch(limit=cap)`` used to claim ``cap`` rows and then stop
        at the first failure, and a backlog therefore drained one alert per run
        instead of ``cap``. One-at-a-time keeps each claim/decision atomic while
        still emitting up to ``cap`` messages per run.
        """
        if self.dispatcher is None:
            return
        cap = self.config.max_alerts_per_run
        sent = retry_wait = unknown = deferred = skipped = 0
        for _ in range(cap):
            outcome = await self.dispatcher.dispatch(limit=1)
            sent += outcome.sent
            retry_wait += outcome.retry_wait
            unknown += outcome.unknown
            deferred += outcome.deferred
            skipped += outcome.skipped_rate_limited
            # Nothing dispatchable left, or the run has to stop to avoid a
            # duplicate / a rate-limit pile-on.
            if outcome.sent == 0 or outcome.unknown or outcome.deferred or outcome.retry_wait or outcome.skipped_rate_limited:
                break
        report.alerts_sent = sent
        if unknown:
            report.add_error(
                f"{unknown} delivery attempt(s) had an unknown outcome and are not retried"
            )
            report.status = RunStatus.DEGRADED
        logger.info(
            "dispatch_complete",
            sent=sent,
            retry=retry_wait,
            unknown=unknown,
            deferred=deferred,
            skipped_rate_limited=skipped,
        )
