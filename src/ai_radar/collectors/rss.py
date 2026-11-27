"""RSS / Atom collection via feedparser.

Conditional GET (ETag / Last-Modified) is used whenever the previous response
provided the validators, so an unchanged feed costs one 304 instead of a full
body. Parsing failures are surfaced as errors, not as an empty success.
"""

from __future__ import annotations

import feedparser

from ..models.discovery import RawEntry
from ..models.source import SourceSpec, SourceState
from ..utils.net import FetchError, SafeHttpClient
from .base import CollectResult


def _text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def parse_feed(text: str, spec: SourceSpec) -> list[RawEntry]:
    """Parse feed text into raw entries, bounded by ``spec.max_items``."""
    parsed = feedparser.parse(text)
    entries: list[RawEntry] = []

    for entry in parsed.entries[: spec.max_items]:
        link = _text(entry.get("link")) or _text(entry.get("id"))
        title = _text(entry.get("title"))
        summary = _text(entry.get("summary")) or _text(entry.get("description"))
        if not summary:
            content = entry.get("content")
            if isinstance(content, list) and content:
                summary = _text(content[0].get("value"))
        published = (
            _text(entry.get("published"))
            or _text(entry.get("updated"))
            or _text(entry.get("created"))
        )
        author = _text(entry.get("author"))
        if not author:
            authors = entry.get("authors")
            if isinstance(authors, list) and authors:
                author = _text(authors[0].get("name"))

        entries.append(
            RawEntry(
                source_id=spec.id,
                title=title,
                url=link,
                summary=summary,
                published_raw=published or None,
                author=author or None,
                external_id=_text(entry.get("id")) or None,
                kind_hint="rss",
            )
        )

    return entries


async def collect_rss(
    spec: SourceSpec, state: SourceState, client: SafeHttpClient
) -> CollectResult:
    """Collect one RSS/Atom feed."""
    try:
        response = await client.get(
            spec.url,
            headers={"Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.5"},
            etag=state.etag,
            last_modified=state.last_modified,
        )
    except FetchError as exc:
        return CollectResult(
            source_id=spec.id,
            error=f"{exc.kind}: {exc}",
            http_status=exc.status,
        )

    if response.not_modified:
        return CollectResult(
            source_id=spec.id,
            not_modified=True,
            etag=state.etag,
            last_modified=state.last_modified,
            http_status=response.status,
        )

    entries = parse_feed(response.text, spec)
    if not entries:
        return CollectResult(
            source_id=spec.id,
            http_status=response.status,
            error="feed parsed but contained no entries",
        )

    return CollectResult(
        source_id=spec.id,
        entries=entries,
        etag=response.etag,
        last_modified=response.last_modified,
        http_status=response.status,
    )
