"""Delivery lifecycle models (Telegram)."""

from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field

from .core import Channel, DeliveryStatus


class DeliveryIntent(BaseModel):
    """A persisted intent to send one message.

    The intent is written to the database *before* the network call so a crash
    between "Telegram accepted" and "we recorded it" leaves a recoverable row.
    """

    model_config = ConfigDict(extra="ignore")

    id: str | None = None
    discovery_key: str
    channel: Channel = Channel.TELEGRAM
    status: DeliveryStatus = DeliveryStatus.PENDING
    attempts: int = Field(default=0, ge=0)
    message_text: str = Field(max_length=8000)
    message_hash: str = Field(min_length=16, max_length=64)
    telegram_message_id: int | None = None
    last_error: str | None = None
    claimed_by: str | None = None
    claimed_at: datetime | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class DispatchOutcome(BaseModel):
    """Per-run delivery result counts, for the workflow run row."""

    sent: int = 0
    retry_wait: int = 0
    unknown: int = 0
    skipped_rate_limited: int = 0
    deferred: int = 0
