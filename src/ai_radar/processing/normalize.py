"""Raw collector output -> validated, canonicalised entries."""

from __future__ import annotations

from ..models.discovery import NormalizedEntry, RawEntry
from ..models.source import SourceSpec
from ..utils.logging import get_logger
from ..utils.text import clean_text, title_fingerprint
from ..utils.timeutil import parse_datetime
from .canonical import canonical_key, canonical_url

logger = get_logger(__name__)

MIN_TITLE_LENGTH = 6
MAX_TITLE_LENGTH = 300


def normalize_raw(raw: RawEntry, source: SourceSpec) -> NormalizedEntry | None:
    """Validate and canonicalise one raw entry.

    Returns ``None`` when the entry cannot be trusted as a discovery at all
    (no URL, no title, non-http URL). Rejections are logged at debug level with
    a reason so a noisy feed can be diagnosed without guessing.
    """
    title = clean_text(raw.title, max_length=MAX_TITLE_LENGTH)
    if len(title) < MIN_TITLE_LENGTH:
        logger.debug("entry_rejected", source_id=raw.source_id, reason="title_too_short")
        return None

    url = (raw.url or "").strip()
    if not url.startswith(("http://", "https://")):
        logger.debug("entry_rejected", source_id=raw.source_id, reason="url_not_http")
        return None

    canonical = canonical_url(url)
    if not canonical:
        logger.debug("entry_rejected", source_id=raw.source_id, reason="url_unparsable")
        return None

    summary = clean_text(raw.summary, max_length=1200)
    published_at = parse_datetime(raw.published_raw)
    if published_at is None and raw.published_raw:
        logger.debug(
            "entry_date_unparsed",
            source_id=raw.source_id,
            raw=str(raw.published_raw)[:40],
        )

    identity = raw.external_id if raw.kind_hint in {"github_releases", "huggingface_models"} else None

    return NormalizedEntry(
        source_id=source.id,
        title=title,
        url=url,
        canonical_url=canonical,
        summary=summary,
        published_at=published_at,
        author=clean_text(raw.author, max_length=120) or None,
        external_id=raw.external_id,
        canonical_key=canonical_key(url, kind=raw.kind_hint, identity=identity),
        title_fingerprint=title_fingerprint(title),
        category=source.category,
        tier=source.tier,
    )


def normalize_batch(
    entries: list[RawEntry], source: SourceSpec
) -> tuple[list[NormalizedEntry], int]:
    """Normalise a batch, returning (accepted, rejected_count)."""
    accepted: list[NormalizedEntry] = []
    rejected = 0
    for raw in entries:
        normalized = normalize_raw(raw, source)
        if normalized is None:
            rejected += 1
        else:
            accepted.append(normalized)
    return accepted, rejected
