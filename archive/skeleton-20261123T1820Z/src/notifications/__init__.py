"""Telegram notification module for AI Radar V1.0."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import structlog

import httpx

from ...settings import get_settings

logger = structlog.get_logger()


class TelegramNotifier:
    """Handles Telegram notifications for AI Radar discoveries."""

    def __init__(self):
        self.settings = get_settings()
        self.env = self.settings.get("environment", {})
        self.bot_token = self.env.get("telegram_bot_token", "")
        self.chat_id = self.env.get("telegram_chat_id", "")

        # Rate limiting
        self.last_send_time = 0
        self.min_interval = 1.0  # Minimum seconds between sends
        self.hourly_count = 0
        self.hourly_reset = datetime.utcnow().replace(minute=0, second=0, microsecond=0)

        # Delivery state tracking
        self.pending_deliveries = []  # Will be loaded from DB

    async def initialize(self):
        """Initialize the notifier, loading pending deliveries from database."""
        await self._load_pending_deliveries()

        # Check if we have required config
        if not self.bot_token or not self.chat_id:
            logger.warning(
                "telegram_notifier_unconfigured",
                missing=["bot_token", "chat_id"],
            )

    async def _load_pending_deliveries(self):
        """Load pending deliveries from the database."""
        from ...storage import DatabaseManager

        db = DatabaseManager()
        deliveries = await db.get_pending_deliveries(limit=100)
        self.pending_deliveries = deliveries or []

    async def send_discovery_alert(
        self,
        discovery: dict,
        analysis: dict = None,
    ) -> Tuple[bool, Optional[int]]:
        """Send a Telegram alert for a qualified discovery.

        Returns:
            Tuple of (success, telegram_message_id)
            Success is False if discovery doesn't meet thresholds.
        """
        # Check thresholds
        priority_score = discovery.get("priority_score", 0)
        confidence = discovery.get("confidence", 0)

        # Only send if priority >= 3/5 (score >= 40) and confidence >= minimum
        min_priority = self.settings.get("scoring", {}).get("min_priority_score", 40)
        min_confidence = self.settings.get("scoring", {}).get(
            "min_confidence_for_alert", 0.5
        )

        if priority_score < min_priority:
            logger.info(
                "discovery_below_priority_threshold",
                priority_score=priority_score,
                min_required=min_priority,
            )
            return False, None

        if confidence < min_confidence:
            logger.info(
                "discovery_below_confidence_threshold",
                confidence=confidence,
                min_required=min_confidence,
            )
            return False, None

        # Check hourly limit
        await self._check_hourly_limit()

        # Build message
        message = self._build_alert_message(discovery, analysis)

        # Send to Telegram
        success, message_id = await self._send_telegram(message)

        if success:
            # Update delivery record in database
            await self._record_successful_send(discovery.get("id", ""), message_id)
            self.hourly_count += 1
            logger.info(
                "telegram_alert_sent",
                message_id=message_id,
                priority_score=priority_score,
                confidence=confidence,
            )
        else:
            logger.error("telegram_send_failed", message=message[:100])

        return success, message_id

    async def _check_hourly_limit(self):
        """Check and enforce hourly Telegram alert limit."""
        now = datetime.utcnow()

        # Reset hourly counter if we've moved to a new hour
        if now.minute == 0 and now.second < 10:
            # New hour starting - reset counter
            self.hourly_count = 0
            self.hourly_reset = now.replace(minute=0, second=0, microsecond=0)

        # Enforce max 5 alerts per hour
        if self.hourly_count >= 5:
            logger.warning(
                "hourly_telegram_limit_reached",
                current_count=self.hourly_count,
                limit=5,
            )
            # Wait until next hour
            seconds_until_next_hour = 3600 - (
                now.replace(minute=0, second=0, microsecond=0) - now
            ).seconds + 60
            await asyncio.sleep(min(seconds_until_next_hour, 60))
            self.hourly_count = 0
            self.hourly_reset = datetime.utcnow().replace(minute=0, second=0, microsecond=0)

    def _build_alert_message(
        self, discovery: dict, analysis: dict = None
    ) -> str:
        """Build a Telegram-formatted alert message."""
        title = discovery.get("title", "Untitled")
        category = discovery.get("category", "unknown")
        priority_score = discovery.get("priority_score", 0)
        confidence = discovery.get("confidence", 0)
        summary = discovery.get("summary", "")[:200]
        published = discovery.get("published", "unknown")
        pricing = discovery.get("pricing_status", "UNVERIFIED")
        source = discovery.get("source_id", "unknown")

        # Escape Telegram formatting special characters
        # Telegram uses: _ for italic, * for bold, [ for links, | for code blocks
        escaped_title = self._escape_telegram_markdown(title)
        escaped_summary = self._escape_telegram_markdown(summary)
        escaped_source = self._escape_telegram_markdown(source)

        # Priority emoji/label
        if priority_score >= 80:
            priority_label = "🔥 5/5"
        elif priority_score >= 60:
            priority_label = "⭐ 4/5"
        elif priority_score >= 40:
            priority_label = "📋 3/5"
        else:
            priority_label = "📄 2/5"

        # Pricing status indicator
        pricing_emoji = {
            "FREE": "🆓",
            "FREE_TIER": "🆓",
            "OPEN_SOURCE": "💻",
            "PAID": "💰",
            "UNVERIFIED": "❓",
        }.get(pricing, "❓")

        # Build the message
        message_parts = []

        # Header with title and priority
        message_parts.append(f"*{escaped_title}*")
        message_parts.append(f"{priority_label} {pricing_emoji}")

        # Category and confidence
        message_parts.append(f"_Category_: {category}")
        message_parts.append(f"_Confidence_: {confidence:.1%}")

        # Publication date
        message_parts.append(f"_Published_: {published}")

        # Short explanation/summary
        if summary:
            message_parts.append(f"")
            message_parts.append(f"💡 *What is new*: {escaped_summary}")

        # Why it matters
        message_parts.append(f"")
        message_parts.append(f"⚙️ *Why it matters*: {self._generate_why_it_matters(discovery)}")

        # Practical use case
        message_parts.append(f"")
        message_parts.append(f"🛠️ *Practical use case*: {self._generate_use_case(discovery)}")

        # Pricing/license
        message_parts.append(f"")
        message_parts.append(f"💵 *Pricing/License*: {pricing}")

        # Source/evidence
        message_parts.append(f"")
        message_parts.append(f"📎 *Evidence*: {escaped_source}")

        # Source links
        url = discovery.get("url", "")
        if url:
            # Telegram doesn't support markdown links natively in the same way,
            # but we can include the URL as text
            message_parts.append(f"")
            message_parts.append(f"🔗 *Source*: {url}")

        # Join with newlines
        message = "\n".join(message_parts)

        # Telegram has a 4096 character limit per message
        if len(message) > 4000:
            message = message[:3997] + "..."

        return message

    @staticmethod
    def _escape_telegram_markdown(text: str) -> str:
        """Escape characters that have special meaning in Telegram MarkdownV2."""
        if not text:
            return ""

        # Telegram MarkdownV2 escape characters: _ * [] \` >
        escape_chars = r'_*[]\\`>#+-=|{}.!'

        escaped = re.sub(
            f'([{re.escape(escape_chars)}])',
            r'\\\1',
            text,
        )

        return escaped

    @staticmethod
    def _generate_why_it_matters(discovery: dict) -> str:
        """Generate a 'why it matters' explanation."""
        category = discovery.get("category", "")
        title = discovery.get("title", "").lower()

        reasons = {
            "ai_models": "New model may impact current workflows and capabilities",
            "ai_coding_tools": "Could improve development productivity significantly",
            "ai_agents": "Advances in autonomy may change automation possibilities",
            "image_video_ai": "New creative tools for content generation",
            "audio_speech_ai": "Improved TTS/STT may affect media production",
            "productivity_tools": "Workflow enhancements for daily work",
            "ai_industry_updates": "Industry shifts may signal market direction",
        }

        return reasons.get(
            category,
            "New development in AI landscape",
        )

    @staticmethod
    def _generate_use_case(discovery: dict) -> str:
        """Generate a practical use case explanation."""
        category = discovery.get("category", "")
        pricing = discovery.get("pricing_status", "UNVERIFIED")

        use_cases = {
            "ai_models": "Experiment with new architectures or integrate into projects",
            "ai_coding_tools": "Accelerate development tasks or code review",
            "ai_agents": "Set up automated research or development pipelines",
            "image_video_ai": "Generate creative assets or visual content",
            "audio_speech_ai": "Create voiceovers or transcribe audio content",
            "productivity_tools": "Automate routine tasks or improve workflow",
            "ai_industry_updates": "Stay informed about AI market developments",
        }

        base = use_cases.get(category, "Explore the new tool/capability")

        # Add pricing context
        if pricing in ["FREE", "FREE_TIER"]:
            base += " (free access available)"
        elif pricing == "OPEN_SOURCE":
            base += " (open source, can self-host)"

        return base

    async def _send_telegram(self, message: str) -> Tuple[bool, Optional[int]]:
        """Send a message to Telegram."""
        if not self.bot_token or not self.chat_id:
            logger.error("cannot_send_telegram_missing_config")
            return False, None

        try:
            # Respect minimum interval between sends
            now = asyncio.get_event_loop().time()
            time_since_last = now - self.last_send_time
            if time_since_last < self.min_interval:
                await asyncio.sleep(self.min_interval - time_since_last)

            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.post(
                    f"https://api.telegram.org/bot{self.bot_token}/sendMessage",
                    json={
                        "chat_id": self.chat_id,
                        "text": message,
                        "parse_mode": "MarkdownV2",  # Use MarkdownV2 for proper escaping
                    },
                    headers={"Content-Type": "application/json"},
                )

                if response.status_code != 200:
                    error_text = response.text
                    logger.error(
                        "telegram_api_error",
                        status_code=response.status_code,
                        error=error_text,
                    )
                    # Update delivery as retry wait
                    await self._record_failed_send(
                        error_message=error_text
                    )
                    return False, None

                result = response.json()

                if not result.get("ok"):
                    error_desc = result.get("description", "Unknown error")
                    logger.error("telegram_response_not_ok", error=error_desc)
                    await self._record_failed_send(error_message=error_desc)
                    return False, None

                message_id = result.get("result", {}).get("message_id")
                self.last_send_time = asyncio.get_event_loop().time()

                return True, message_id

        except Exception as e:
            logger.error("telegram_send_exception", error=str(e))
            await self._record_failed_send(error_message=str(e))
            return False, None

    async def _record_successful_send(
        self, discovery_id: str, message_id: int
    ):
        """Record a successful Telegram message send."""
        from ...storage import DatabaseManager

        db = DatabaseManager()
        await db.update_delivery(
            discovery_id="temp",  # Will be updated properly
            status="SENT",
            telegram_message_id=message_id,
        )

    async def _record_failed_send(self, error_message: str = None):
        """Record a failed Telegram send attempt."""
        from ...storage import DatabaseManager

        db = DatabaseManager()
        # Find the oldest pending delivery and update it
        if self.pending_deliveries:
            delivery = self.pending_deliveries[0]
            delivery_id = delivery.get("id")
            attempts = delivery.get("attempts", 0) + 1

            # Set status to RETRY_WAIT for transient errors, or UNKNOWN
            if error_message and ("429" in error_message or "timeout" in error_message.lower()):
                new_status = "RETRY_WAIT"
            else:
                new_status = "UNKNOWN"

            await db.update_delivery(
                delivery_id=delivery_id,
                status=new_status,
                telegram_message_id=None,
                error_message=error_message,
            )

    async def send_daily_digest(self, discoveries: List[dict]) -> bool:
        """Send a daily digest summary to Telegram.

        Args:
            discoveries: List of verified discoveries to include

        Returns:
            True if digest was sent successfully
        """
        # Only send if there are qualifying discoveries
        min_priority = self.settings.get("scoring", {}).get("min_priority_score", 40)

        qualifying = [
            d for d in discoveries
            if d.get("priority_score", 0) >= min_priority
        ]

        if not qualifying:
            # Send honest report with no qualifying discoveries
            await self._send_honest_report()
            return True

        # Build digest message
        message = self._build_digest_message(qualifying)

        # Send
        success, _ = await self._send_telegram(message)

        if success:
            logger.info("daily_digest_sent", discovery_count=len(qualifying))
        else:
            logger.error("daily_digest_send_failed")

        return success

    def _build_digest_message(
        self, discoveries: List[dict]
    ) -> str:
        """Build a daily digest Telegram message."""
        # Top 5 discoveries
        top_five = discoveries[:5]

        sections = []

        # Header
        from datetime import datetime
        ist_now = datetime.now().strftime("%Y-%m-%d %H:%M:%S IST")
        sections.append(f"*AI Radar Daily Digest – {ist_now}*")
        sections.append("")

        # Top discoveries
        sections.append("🔟 *Top Discoveries*:")
        for i, disc in enumerate(top_five, 1):
            title = disc.get("title", "Untitle")[:60]
            category = disc.get("category", "?")
            score = disc.get("priority_score", 0)
            pricing = disc.get("pricing_status", "?")
            sections.append(f"{i}. {title} ({category}, {pricing}, Priority {score}/100)")

        sections.append("")
        sections.append("---")
        sections.append("")

        # Other sections would follow...
        # Important model releases, best coding tools, free/open-source findings, industry developments
        # One practical recommendation

        # Practical recommendation
        if top_five:
            first = top_five[0]
            rec_title = first.get("title", "")[:50]
            sections.append("💡 *Recommendation*: Explore {}", rec_title)

        # Join
        message = "\n".join(sections)

        # Truncate if needed
        if len(message) > 3800:
            message = message[:3800] + "..."

        return message

    async def _send_honest_report(self):
        """Send a honest report when no qualifying discoveries exist."""
        from datetime import datetime
        ist_now = datetime.now().strftime("%Y-%m-%d %H:%M:%S IST")

        message = f"*AI Radar Daily Digest – {ist_now}*\n\n"
        message += "No discoveries with priority 3/5 or higher in the last research run.\n"
        message += "Keep monitoring – quality discoveries will surface soon.\n"

        await self._send_telegram(message)