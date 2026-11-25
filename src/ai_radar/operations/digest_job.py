"""Daily digest job: build from stored confirmed discoveries, send once per day."""

from __future__ import annotations

from typing import Any

from ..models.core import DeliveryStatus
from ..notifications import TelegramClient, TelegramError
from ..reporting import DigestBuilder
from ..utils.logging import get_logger
from ..utils.timeutil import day_bounds_ist, today_ist
from .heartbeat import HeartbeatReporter

logger = get_logger(__name__)


class DigestService:
    def __init__(
        self,
        *,
        repository,
        telegram: TelegramClient | None,
        builder: DigestBuilder,
        min_confidence: float,
    ) -> None:
        self._repo = repository
        self._telegram = telegram
        self._builder = builder
        self._min_confidence = min_confidence

    async def run(self, *, dry_run: bool = False, resend: bool = False) -> dict[str, Any]:
        digest_date = today_ist()
        start, end = day_bounds_ist(digest_date)

        rows = await self._repo.discoveries_for_digest(
            start, end, min_confidence=self._min_confidence, limit=40
        )
        content = self._builder.build(rows, digest_date=digest_date)

        existing = await self._repo.get_digest(digest_date)
        if existing and existing.get("telegram_message_id") and not resend:
            logger.info("digest_already_sent", digest_date=str(digest_date))
            return {
                "digest_date": str(digest_date),
                "status": "already_sent",
                "discoveries": content.top_count,
                "empty": content.honest_empty,
            }

        message_id: int | None = None
        status = "generated"
        if dry_run:
            status = "dry_run"
        elif self._telegram is None or not self._telegram.configured:
            status = "generated_not_sent"
            logger.warning("digest_not_sent_telegram_unconfigured")
        else:
            try:
                message_id = await self._telegram.send_message(content.text)
                status = "sent"
            except TelegramError as exc:
                status = f"send_failed:{exc.kind}"
                logger.error("digest_send_failed", kind=exc.kind, error=str(exc))

        await self._repo.save_digest(
            digest_date=digest_date,
            text=content.text,
            discovery_keys=content.discovery_keys,
            count=content.top_count,
            telegram_message_id=message_id,
        )

        logger.info(
            "digest_completed",
            digest_date=str(digest_date),
            status=status,
            discoveries=content.top_count,
            empty=content.honest_empty,
        )
        return {
            "digest_date": str(digest_date),
            "status": status,
            "discoveries": content.top_count,
            "empty": content.honest_empty,
            "message_id": message_id,
            "text": content.text,
        }
