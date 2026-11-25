"""Delivery state machine.

Invariants enforced here:

1. The intent row exists before the network call, so nothing can be sent that the
   database does not know about.
2. A message is claimed exactly once (``FOR UPDATE SKIP LOCKED``); a second runner
   cannot take it.
3. An ambiguous outcome becomes ``UNKNOWN`` and is **not** retried automatically.
   Telegram offers no idempotency key, so a retry could double-alert the user. The
   row is surfaced in the heartbeat instead.
4. The per-run cap is enforced by the claim query, not by a counter in memory.
"""

from __future__ import annotations

from typing import Mapping

from ..models.core import DeliveryStatus
from ..models.delivery import DispatchOutcome
from ..utils.logging import get_logger
from ..utils.redact import scrub
from ..utils.text import stable_sha256
from .format import build_alert_message
from .telegram import TelegramClient, TelegramError

logger = get_logger(__name__)

MAX_BACKOFF_SECONDS = 3600
RATE_LIMIT_DEFER_SECONDS = 900


def message_hash(text: str) -> str:
    return stable_sha256(text)[:32]


class DeliveryDispatcher:
    """Queues and dispatches Telegram alerts."""

    def __init__(
        self,
        repository,
        telegram: TelegramClient,
        *,
        worker_id: str,
        category_names: Mapping[str, str] | None = None,
        max_per_run: int = 5,
        alert_max_age_hours: int | None = None,
    ) -> None:
        self._repo = repository
        self._telegram = telegram
        self._worker_id = worker_id
        self._category_names = dict(category_names or {})
        self._max_per_run = max_per_run
        #: Re-checked at claim time so a starved queue cannot deliver stale news
        #: days after it was fresh enough to be queued.
        self._alert_max_age_hours = alert_max_age_hours

    async def queue(self, discovery_id: str, row: Mapping) -> bool:
        """Persist a delivery intent for one verified discovery."""
        text = build_alert_message(row, category_names=self._category_names)
        delivery_id = await self._repo.queue_delivery(
            discovery_id, text, message_hash(text)
        )
        if delivery_id is None:
            logger.debug("delivery_intent_already_exists", discovery_id=discovery_id)
            return False
        return True

    async def dispatch(self, limit: int | None = None) -> DispatchOutcome:
        """Send at most ``limit`` queued alerts."""
        cap = min(limit or self._max_per_run, self._max_per_run)
        outcome = DispatchOutcome()

        released = await self._repo.release_stale_claims()
        if released:
            logger.warning("stale_delivery_claims_released", count=released)

        if self._alert_max_age_hours is not None:
            expired = await self._repo.expire_stale_deliveries(self._alert_max_age_hours)
            if expired:
                logger.warning(
                    "stale_deliveries_expired",
                    count=expired,
                    max_age_hours=self._alert_max_age_hours,
                )

        claimed = await self._repo.claim_deliveries(
            self._worker_id, cap, max_age_hours=self._alert_max_age_hours
        )
        if not claimed:
            return outcome

        stop_reason: str | None = None
        for index, intent in enumerate(claimed):
            if stop_reason is not None:
                await self._repo.defer_delivery(
                    str(intent.id), delay_seconds=600, reason=stop_reason
                )
                outcome.deferred += 1
                continue
            assert intent.id is not None
            try:
                message_id = await self._telegram.send_message(intent.message_text)
            except TelegramError as exc:
                stop_reason = await self._handle_error(intent, exc, outcome)
                continue

            await self._repo.mark_delivery_sent(str(intent.id), message_id)
            await self._repo.mark_notified(intent.discovery_key)
            outcome.sent += 1
            logger.info(
                "alert_sent",
                delivery_id=str(intent.id),
                telegram_message_id=message_id,
                attempt=intent.attempts,
            )

        return outcome

    async def _handle_error(self, intent, exc: TelegramError, outcome: DispatchOutcome) -> str | None:
        """Classify one failure. Returns a stop reason when the run should stop."""
        delivery_id = str(intent.id)
        logger.warning(
            "alert_send_failed",
            delivery_id=delivery_id,
            kind=exc.kind,
            attempts=intent.attempts,
            error=str(exc),
        )

        if exc.ambiguous:
            # Telegram errors embed the request URL, which contains the bot token.
            # The log processor redacts it, but this text is written to the
            # database and then exported in backups, so it must be scrubbed here.
            await self._repo.mark_delivery_unknown(delivery_id, scrub(str(exc)))
            outcome.unknown += 1
            return "previous send outcome unknown - paused to avoid a duplicate"

        if exc.kind == "rate_limit":
            wait = int(min(exc.retry_after or RATE_LIMIT_DEFER_SECONDS, MAX_BACKOFF_SECONDS))
            await self._repo.defer_delivery(
                delivery_id, delay_seconds=wait, reason="telegram rate limit"
            )
            outcome.skipped_rate_limited += 1
            return "telegram rate limit"

        if exc.kind == "unauthorized":
            await self._repo.defer_delivery(
                delivery_id, delay_seconds=MAX_BACKOFF_SECONDS, reason="telegram credentials rejected"
            )
            outcome.deferred += 1
            return "telegram credentials rejected - fix TELEGRAM_BOT_TOKEN/CHAT_ID"

        if exc.kind == "bad_request":
            # Permanent for this message: retrying would fail identically.
            await self._repo.mark_delivery_unknown(delivery_id, scrub(f"permanent: {exc}"))
            outcome.unknown += 1
            return None

        delay = int(min(60 * (2 ** max(0, intent.attempts - 1)), MAX_BACKOFF_SECONDS))
        await self._repo.defer_delivery(
            delivery_id, delay_seconds=delay, reason=scrub(str(exc))
        )
        outcome.retry_wait += 1
        return None

    async def send_test_message(self, text: str) -> int:
        """Send one ad-hoc message (used by ``ai-radar notify --test``)."""
        return await self._telegram.send_message(text)
