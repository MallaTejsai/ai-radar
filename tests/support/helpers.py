"""Small builders shared by tests."""

from __future__ import annotations

from datetime import datetime, timezone

import httpx

from ai_radar.models.core import Category, SourceTier
from ai_radar.models.discovery import NormalizedEntry, RawEntry
from ai_radar.processing.canonical import canonical_key, canonical_url
from ai_radar.utils.net import SafeHttpClient
from ai_radar.utils.text import title_fingerprint
from ai_radar.utils.timeutil import utcnow


def make_entry(
    *,
    title: str = "Introducing Model X, a new coding model",
    url: str = "https://example.com/blog/model-x",
    summary: str = "A new model with a free tier and long context support.",
    category: Category = Category.AI_MODELS,
    tier: SourceTier = SourceTier.A,
    source_id: str = "openai_news",
    published_at: datetime | None = None,
    key: str | None = None,
) -> NormalizedEntry:
    return NormalizedEntry(
        source_id=source_id,
        title=title,
        url=url,
        canonical_url=canonical_url(url),
        summary=summary,
        published_at=published_at or utcnow(),
        canonical_key=key or canonical_key(url),
        title_fingerprint=title_fingerprint(title),
        category=category,
        tier=tier,
    )


def make_raw(**overrides) -> RawEntry:
    payload = {
        "source_id": "openai_news",
        "title": "Introducing Model X",
        "url": "https://example.com/blog/model-x",
        "summary": "A new model.",
        "published_raw": "Mon, 23 Nov 2026 06:00:00 GMT",
    }
    payload.update(overrides)
    return RawEntry(**payload)


def mock_client(handler, **kwargs) -> SafeHttpClient:
    """A SafeHttpClient wired to an in-process transport (no network, no DNS)."""
    return SafeHttpClient(
        transport=httpx.MockTransport(handler),
        validate_dns=False,
        retries=kwargs.pop("retries", 1),
        **kwargs,
    )
