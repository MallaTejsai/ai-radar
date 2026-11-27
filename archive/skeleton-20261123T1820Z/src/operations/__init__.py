"""Operations module for AI Radar V1.0 - Pipeline orchestration and workflow."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import structlog

from ...collectors import collect_all_sources
from ...processing import (
    deduplicate_discoveries,
    extract_priority_score,
    classify_confidence,
    canonicalize_discovery,
)
from ...verification import verify_discovery
from ...intelligence import ProviderAdapter
from ...storage import DatabaseManager
from ...notifications import TelegramNotifier
from ...reporting import DailyDigestGenerator

logger = structlog.get_logger()


class ResearchPipeline:
    """Orchestrates the full research pipeline."""

    def __init__(self):
        self.db = DatabaseManager()
        self.notifier = TelegramNotifier()
        self.intelligence = ProviderAdapter()
        self.digest_generator = DailyDigestGenerator()
        self.collected_count = 0
        self.verified_count = 0
        self.persisted_count = 0
        self.notified_count = 0

    async def run_full_pipeline(self) -> Dict[str, Any]:
        """Run the complete research pipeline.

        Returns:
            Dict with pipeline results and statistics
        """
        run_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        logger.info("pipeline_started", run_id=run_id)

        # Create workflow run record
        await self.db.create_workflow_run(run_id=run_id)

        try:
            # Phase 1: Collect
            logger.info("pipeline_phase_collect")
            discoveries = await self._phase_collect()

            # Phase 2: Normalize & Canonicalize
            logger.info("pipeline_phase_normalize")
            normalized = await self._phase_normalize(discoveries)

            # Phase 3: Deduplicate
            logger.info("pipeline_phase_dedup")
            deduped, all_keys = await self._phase_dedup(normalized)

            # Phase 4: Verify
            logger.info("pipeline_phase_verify")
            verified = await self._phase_verify(deduped)

            # Phase 5: AI Analysis
            logger.info("pipeline_phase_analyze")
            analyzed = await self._phase_analyze(verified)

            # Phase 6: Priority Scoring
            logger.info("pipeline_phase_scoring")
            scored = await self._phase_scoring(analyzed)

            # Phase 7: Persist
            logger.info("pipeline_phase_persist")
            persisted = await self._phase_persist(scored)

            # Phase 8: Notify
            logger.info("pipeline_phase_notify")
            notified = await self._phase_notify(persisted)

            # Phase 9: Daily Digest (if end of day)
            logger.info("pipeline_phase_digest")
            await self._phase_digest(persisted)

            # Update workflow run
            await self.db.update_workflow_run(
                run_id=run_id,
                status="completed",
                discoveries_found=len(discoveries),
                discoveries_persisted=len(persisted),
                discoveries_notified=len(notified),
                duration_seconds=self._get_duration(),
            )

            result = {
                "run_id": run_id,
                "status": "completed",
                "discoveries_collected": len(discoveries),
                "discoveries_normalized": len(normalized),
                "discoveries_deduped": len(deduped),
                "discoveries_verified": len(verified),
                "discoveries_analyzed": len(analyzed),
                "discoveries_scored": len(scored),
                "discoveries_persisted": len(persisted),
                "discoveries_notified": len(notified),
                "new_discoveries": len([d for d in persisted if d.get("_is_new", False)]),
            }

            logger.info("pipeline_completed", **result)
            return result

        except Exception as e:
            logger.error("pipeline_failed", error=str(e), run_id=run_id)

            await self.db.update_workflow_run(
                run_id=run_id,
                status="failed",
                error_message=str(e),
                duration_seconds=self._get_duration(),
            )

            raise

    async def _phase_collect(self) -> List[dict]:
        """Phase 1: Collect from all sources."""
        results = await collect_all_sources()

        all_entries = []
        for source_id, (entries, metadata) in results.items():
            # Canonicalize each entry with source ID
            for entry in entries:
                canon = canonicalize_discovery(entry, source_id)
                all_entries.append(canon)

            # Update source checkpoint
            await self.db.upsert_source_checkpoint(
                type(self)._source_checkpoint_from_metadata(source_id, metadata)
            )

        self.collected_count = len(all_entries)
        logger.info("collection_phase_complete", count=len(all_entries))
        return all_entries

    @staticmethod
    def _source_checkpoint_from_metadata(
        source_id: str, metadata: dict
    ) -> SourceCheckpoint:
        """Create a SourceCheckpoint from collection metadata."""
        return SourceCheckpoint(
            source_id=source_id,
            last_checkpoint=metadata.get("checkpoint"),
            last_error=metadata.get("last_error"),
            health_status=metadata.get("health_status", "healthy"),
            records_collected=metadata.get("fetched", 0),
        )

    async def _phase_normalize(
        self, discoveries: List[dict]
    ) -> List[dict]:
        """Phase 2: Normalize and canonicalize all discoveries."""
        normalized = []
        for disc in discoveries:
            # Ensure canonical_key exists
            if not disc.get("canonical_key"):
                disc = canonicalize_discovery(disc, disc.get("source_id", "unknown"))
            normalized.append(disc)

        logger.info("normalization_complete", count=len(normalized))
        return normalized

    async def _phase_dedup(
        self, discoveries: List[dict]
    ) -> Tuple[List[dict], Set[str]]:
        """Phase 3: Deduplicate discoveries."""
        # Get existing keys from database
        all_keys = set()
        # In a real implementation, we'd query the DB for existing canonical keys
        # For now, we use an in-memory set

        deduped, new_keys = deduplicate_discoveries(discoveries, all_keys)
        self.verified_count = len(deduped)  # reuse variable
        logger.info("deduplication_complete", input_count=len(discoveries), output_count=len(deduped))
        return deduped, new_keys

    async def _phase_verify(
        self, discoveries: List[dict]
    ) -> List[dict]:
        """Phase 4: Verify each discovery using evidence checks."""
        verified = []
        semaphore = asyncio.Semaphore(3)  # Concurrent verification limit

        async def verify_one(disc):
            async with semaphore:
                source_id = disc.get("source_id", "unknown")
                # Get source config
                from ...settings import get_source_config

                source_config = get_source_config(source_id)
                if source_config:
                    verified_disc, verdict = await verify_discovery(disc, source_config)
                    verified_disc["verification_verdict"] = verdict
                    # Only keep verified or partial
                    if verdict in ["CONFIRMED", "PARTIAL"]:
                        return verified_disc
                return None

        tasks = [verify_one(disc) for disc in discoveries]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for result in results:
            if result is not None and isinstance(result, dict):
                verified.append(result)

        logger.info("verification_complete", input_count=len(discoveries), output_count=len(verified))
        return verified

    async def _phase_analyze(
        self, discoveries: List[dict]
    ) -> List[dict]:
        """Phase 5: AI analysis of each discovery."""
        analyzed = []

        for disc in discoveries:
            # Skip if already analyzed
            if disc.get("analysis_result"):
                analyzed.append(disc)
                continue

            try:
                analysis, provider = await self.intelligence.analyze_discovery(disc)
                disc["analysis_result"] = analysis
                disc["ai_provider"] = provider
                analyzed.append(disc)
            except Exception as e:
                logger.error("analysis_failed", error=str(e), title=disc.get("title", "unknown"))
                disc["analysis_result"] = {"error": str(e)}
                disc["ai_provider"] = "failed"
                analyzed.append(disc)

        logger.info("analysis_complete", count=len(analyzed))
        return analyzed

    async def _phase_scoring(
        self, discoveries: List[dict]
    ) -> List[dict]:
        """Phase 6: Calculate priority scores and confidence."""
        weights_config = get_settings().get("scoring", {}).get("weights", {})

        for disc in discoveries:
            # Extract priority score
            priority = extract_priority_score(disc, weights=weights_config)
            disc["priority_score"] = priority

            # Classify confidence
            confidence = classify_confidence(disc)
            disc["confidence"] = confidence

            # Determine if it qualifies for alerts
            min_priority = 40  # 3/5 threshold
            disc["_qualifies_for_alert"] = priority >= min_priority

        logger.info("scoring_complete", count=len(discoveries))
        return discoveries

    async def _phase_persist(
        self, discoveries: List[dict]
    ) -> List[dict]:
        """Phase 7: Persist discoveries to Supabase."""
        persisted = []

        for disc in discoveries:
            # Convert to DiscoveryCreate model
            try:
                disc_copy = dict(disc)

                # Remove internal fields
                for key in ["_qualifies_for_alert", "analysis_result", "ai_provider", "verification_verdict"]:
                    disc_copy.pop(key, None)

                disc_model = DiscoveryCreate(**disc_copy)

                # Upsert into database
                record_id = await self.db.upsert_discovery(disc_model)

                if record_id:
                    disc["_is_new"] = True
                    disc["_db_id"] = record_id
                    persisted.append(disc)
                else:
                    disc["_is_new"] = False
                    persisted.append(disc)

            except ValidationError as e:
                logger.warning("disc_validation_failed", error=str(e), title=disc.get("title", "unknown"))
                persisted.append(disc)  # Persist even if flawed

        self.persisted_count = len(persisted)
        logger.info("persist_complete", input_count=len(discoveries), output_count=len(persisted))
        return persisted

    async def _phase_notify(
        self, discoveries: List[dict]
    ) -> List[dict]:
        """Phase 8: Send Telegram alerts for qualifying discoveries."""
        notified = []

        # Filter for qualifying discoveries
        qualifying = [
            d for d in discoveries
            if d.get("_qualifies_for_alert", False)
            and d.get("confidence", 0) >= 0.5
        ]

        logger.info("notification_qualifying_count", count=len(qualifying))

        # Send alerts (max 5 per hour)
        semaphore = asyncio.Semaphore(5)

        async def send_alert(disc):
            async with semaphore:
                success, message_id = await self.notifier.send_discovery_alert(disc)
                if success:
                    # Mark as notified
                    disc["_telegram_message_id"] = message_id
                    self.notified_count += 1
                return disc

        tasks = [send_alert(disc) for disc in qualifying[:10]]  # Limit to 10 for safety
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for result in results:
            if result is not None and isinstance(result, dict):
                notified.append(result)

        logger.info("notification_complete", notified_count=self.notified_count)
        return notified

    async def _phase_digest(self, discoveries: List[dict]):
        """Phase 9: Generate and send daily digest if appropriate."""
        # Check if we should send a digest (at 10 PM IST or end of research day)
        from ...settings import is_research_hour_ist

        # For now, always generate digest from the discoveries
        # In production, this would be triggered by a cron at 22:00 IST

        digest_text = await self.digest_generator.generate_digest(discoveries)

        if digest_text:
            # Send to Telegram
            await self.notifier.send_daily_digest(discoveries)
            logger.info("digest_generated_and_sent", length=len(digest_text))
        else:
            logger.info("digest_no_qualifying_discoveries")

    def _get_duration(self) -> float:
        """Get pipeline duration in seconds."""
        # This would be tracked properly in a real implementation
        return 0.0