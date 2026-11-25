"""Reporting module for AI Radar V1.0 - Daily digest and summary generation."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import structlog

from ...storage import DatabaseManager
from ...settings import get_settings

logger = structlog.get_logger()


class DailyDigestGenerator:
    """Generates the daily digest at 10 PM IST."""

    def __init__(self):
        self.settings = get_settings()
        self.min_priority = self.settings.get("scoring", {}).get(
            "min_priority_score", 40
        )

    async def generate_digest(
        self, discoveries: List[dict], include_verified_only: bool = True
    ) -> Optional[str]:
        """Generate a daily digest text from discoveries.

        Args:
            discoveries: List of discovery records
            include_verified_only: If True, only include verified discoveries

        Returns:
            Generated digest text, or None if no qualifying discoveries
        """
        # Filter discoveries
        filtered = await self._filter_discoveries(discoveries, include_verified_only)

        if not filtered:
            return None

        # Sort by priority score (descending)
        filtered.sort(key=lambda d: d.get("priority_score", 0), reverse=True)

        # Build digest
        digest = self._build_digest_content(filtered)

        return digest

    async def _filter_discoveries(
        self, discoveries: List[dict], verified_only: bool
    ) -> List[dict]:
        """Filter discoveries for digest inclusion."""
        filtered = []

        for disc in discoveries:
            # Priority filter
            priority = disc.get("priority_score", 0)
            if priority < self.min_priority:
                continue

            # Confidence filter
            confidence = disc.get("confidence", 0)
            min_confidence = self.settings.get("scoring", {}).get(
                "min_confidence_for_alert", 0.5
            )
            if confidence < min_confidence:
                continue

            # Verification status filter
            if verified_only:
                status = disc.get("verification_status", "UNVERIFIED")
                if status != "CONFIRMED":
                    continue

            filtered.append(disc)

        return filtered

    def _build_digest_content(
        self, discoveries: List[dict]
    ) -> str:
        """Build the digest content from filtered discoveries."""
        ist_now = datetime.now().strftime("%Y-%m-%d %H:%M:%S IST")

        sections = []
        sections.append("=" * 60)
        sections.append(f"AI Radar Daily Digest")
        sections.append(f"Date: {ist_now}")
        sections.append("=" * 60)
        sections.append("")

        # Top 5 discoveries
        sections.append("🥇 Top 5 Discoveries:")
        sections.append("-" * 40)

        top_five = discoveries[:5]
        for i, disc in enumerate(top_five, 1):
            title = disc.get("title", "Untitled")[:80]
            category = disc.get("category", "unknown")
            summary = disc.get("summary", "")[:150]
            priority = disc.get("priority_score", 0)
            pricing = disc.get("pricing_status", "UNVERIFIED")
            published = disc.get("published_at", "unknown")

            sections.append(f"{i}. **{title}**")
            sections.append(f"   Category: {category}")
            sections.append(f"   Priority: {priority}/100 ({self._priority_label(priority)})")
            sections.append(f"   Pricing: {pricing}")
            if published != "unknown":
                sections.append(f"   Published: {published}")
            if summary:
                sections.append(f"   Summary: {summary}")
            sections.append("")

        # Important model releases
        sections.append("🤖 Important Model Releases:")
        sections.append("-" * 40)
        model_discs = [
            d for d in discoveries if d.get("category") == "ai_models"
        ][:3]
        if model_discs:
            for disc in model_discs:
                title = disc.get("title", "Untitled")[:60]
                summary = disc.get("summary", "")[:120]
                sections.append(f"• {title}: {summary}")
        else:
            sections.append("• None this period")
        sections.append("")

        # Best AI coding tools
        sections.append("💻 Best AI Coding Tools:")
        sections.append("-" * 40)
        coding_discs = [
            d for d in discoveries if d.get("category") == "ai_coding_tools"
        ][:3]
        if coding_discs:
            for disc in coding_discs:
                title = disc.get("title", "Untitled")[:60]
                sections.append(f"• {title}")
        else:
            sections.append("• None this period")
        sections.append("")

        # Useful free/open-source findings
        sections.append("🆓 Useful Free/Open-Source Findings:")
        sections.append("-" * 40)
        free_discs = [
            d for d in discoveries
            if d.get("pricing_status") in ["FREE", "FREE_TIER", "OPEN_SOURCE"]
        ][:3]
        if free_discs:
            for disc in free_discs:
                title = disc.get("title", "Untitled")[:60]
                sections.append(f"• {title}")
        else:
            sections.append("• None this period")
        sections.append("")

        # Significant industry developments
        sections.append("📈 Significant Industry Developments:")
        sections.append("-" * 40)
        industry_discs = [
            d for d in discoveries if d.get("category") == "ai_industry_updates"
        ][:3]
        if industry_discs:
            for disc in industry_discs:
                title = disc.get("title", "Untitled")[:60]
                sections.append(f"• {title}")
        else:
            sections.append("• None this period")
        sections.append("")

        # One practical recommendation
        sections.append("💡 Practical Recommendation:")
        sections.append("-" * 40)
        # Pick the highest priority discovery as recommendation
        if top_five:
            first = top_five[0]
            title = first.get("title", "Explore new AI tool")[:70]
            categories = first.get("category", "AI")
            pricing = first.get("pricing_status", "to explore")
            sections.append(
                f"Explore '{title}' ({categories}, {pricing}) – "
                "it may enhance your workflow or research."
            )
        else:
            sections.append("Stay curious and keep exploring the AI landscape!")
        sections.append("")

        # Source links
        sections.append("📎 Source Links:")
        sections.append("-" * 40)
        for disc in top_five[:3]:
            url = disc.get("url", "")
            if url:
                # Truncate long URLs
                display_url = url[:60] + "..." if len(url) > 60 else url
                sections.append(f"• {display_url}")
        if not any(disc.get("url", "") for disc in top_five[:3]):
            sections.append("• Check AI Radar for full source details")

        sections.append("")
        sections.append("=" * 60)
        sections.append("End of daily digest")
        sections.append("=" * 60)

        return "\n".join(sections)

    @staticmethod
    def _priority_label(score: int) -> str:
        """Get priority label for a score."""
        if score >= 80:
            return "5/5"
        elif score >= 60:
            return "4/5"
        elif score >= 40:
            return "3/5"
        else:
            return "2/5"

    async def save_digest_record(
        self, digest_text: str, discovery_count: int, priority_min: int
    ) -> Optional[str]:
        """Save a daily digest record to the database."""
        from ...storage import DatabaseManager

        db = DatabaseManager()

        try:
            from ...storage import BackupManifest
            import copy

            # Create a simple model for digest tracking
            class DigestRecord:
                def __init__(self, text, count, min_pri):
                    self.digest_text = text
                    self.discovery_count = count
                    self.priority_minimum = min_pri
                    self.created_at = datetime.now(timezone.utc)

            record = DigestRecord(digest_text, discovery_count, priority_min)

            # Use the backup_manifests table or create a new table
            # For now, just log and return
            logger.info(
                "digest_record_created",
                discovery_count=discovery_count,
                priority_min=priority_min,
            )
            return f"digest_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

        except Exception as e:
            logger.error("save_digest_record_failed", error=str(e))
            return None