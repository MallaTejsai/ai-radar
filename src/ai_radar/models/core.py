"""Enumerations shared across the AI Radar domain.

Every enum is a closed set: values that are not listed here are rejected by
Pydantic validation rather than silently coerced. That is deliberate - several
of the original skeleton's defects came from free-form string states.
"""

from __future__ import annotations

from enum import StrEnum


class Category(StrEnum):
    """The seven research categories the agent monitors."""

    AI_MODELS = "ai_models"
    AI_CODING_TOOLS = "ai_coding_tools"
    AI_AGENTS = "ai_agents"
    IMAGE_VIDEO_AI = "image_video_ai"
    AUDIO_SPEECH_AI = "audio_speech_ai"
    PRODUCTIVITY_TOOLS = "productivity_tools"
    AI_INDUSTRY_UPDATES = "ai_industry_updates"


class SourceTier(StrEnum):
    """Evidence weight of a source.

    A - official announcement, official docs, official release, publisher model card.
    B - established technical reporting / independent technical analysis.
    C - community, social, aggregator: may *identify* a candidate, never *establish* a fact.
    """

    A = "A"
    B = "B"
    C = "C"


class CollectionMethod(StrEnum):
    """Supported, bounded collection methods. No free-form scraping."""

    RSS = "rss"
    GITHUB_RELEASES = "github_releases"
    HUGGINGFACE_MODELS = "huggingface_models"


class PricingStatus(StrEnum):
    """Pricing classification. ``UNVERIFIED`` is the honest default, never ``FREE``."""

    FREE = "FREE"
    FREE_TIER = "FREE_TIER"
    OPEN_SOURCE = "OPEN_SOURCE"
    PAID = "PAID"
    UNVERIFIED = "UNVERIFIED"


class Verdict(StrEnum):
    """Outcome of evidence verification for a single discovery."""

    CONFIRMED = "CONFIRMED"
    PARTIAL = "PARTIAL"
    INSUFFICIENT = "INSUFFICIENT"
    DISPROVED = "DISPROVED"
    ERROR = "ERROR"


class DiscoveryStatus(StrEnum):
    """Lifecycle of a persisted discovery."""

    VERIFIED = "verified"
    NOTIFIED = "notified"
    ARCHIVED = "archived"


class DeliveryStatus(StrEnum):
    """Persistent Telegram delivery states required by the spec."""

    PENDING = "PENDING"
    SENDING = "SENDING"
    SENT = "SENT"
    RETRY_WAIT = "RETRY_WAIT"
    UNKNOWN = "UNKNOWN"


class RunStatus(StrEnum):
    """Workflow run lifecycle."""

    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED_LOCKED = "skipped_locked"
    DEGRADED = "degraded"


class Provider(StrEnum):
    """Intelligence providers in mandated priority order."""

    FREEAI = "freeai"
    GEMINI = "gemini"
    GROQ = "groq"
    GROK = "grok"
    SARVAM = "sarvam"
    DETERMINISTIC = "deterministic"


class Channel(StrEnum):
    """Outbound delivery channels."""

    TELEGRAM = "telegram"
