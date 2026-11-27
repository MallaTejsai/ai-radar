"""Text cleaning and deterministic identity helpers.

Deduplication depends on these functions being pure and stable: the same input
must always produce the same key, on any machine, in any process.
"""

from __future__ import annotations

import hashlib
import html
import re
import unicodedata
from html.parser import HTMLParser
from urllib.parse import urlsplit

_WS = re.compile(r"\s+")
_ZERO_WIDTH = re.compile(r"[\u200b-\u200f\u202a-\u202e\ufeff]")
_NON_TEXT = re.compile(r"[^\w\s]+", re.UNICODE)
_LEADING_VERSION = re.compile(r"^(?:v|version|release)\s*\d+(?:[.\-_]\d+)*\s*", re.IGNORECASE)

#: Words that carry no identity signal in a release headline.
_STOPWORDS = frozenset(
    {"a", "an", "the", "and", "or", "of", "for", "to", "in", "on", "with", "is", "are", "now"}
)


class _TextExtractor(HTMLParser):
    """Strip tags while dropping script/style content entirely."""

    SKIP = {"script", "style", "noscript", "template"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.SKIP:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self._parts.append(data)

    def text(self) -> str:
        # Join without inserting separators: the source already contains the
        # whitespace that matters, and added spaces would double it up.
        return _WS.sub(" ", "".join(self._parts)).strip()


def strip_html(value: str | None) -> str:
    if not value:
        return ""
    extractor = _TextExtractor()
    try:
        extractor.feed(value)
        extractor.close()
    except Exception:  # pragma: no cover - malformed markup fallback
        return html.unescape(re.sub(r"<[^>]+>", " ", value))
    return extractor.text()


def clean_text(value: str | None, *, max_length: int = 1200) -> str:
    """Unescape, strip markup, collapse whitespace, bound the length."""
    if not value:
        return ""
    text = strip_html(value)
    text = html.unescape(text)
    text = _ZERO_WIDTH.sub("", text)
    text = text.replace("\xa0", " ")
    text = _WS.sub(" ", text).strip()
    if len(text) > max_length:
        cut = text[:max_length]
        boundary = cut.rfind(" ")
        if boundary > max_length * 0.6:
            cut = cut[:boundary]
        text = cut.rstrip() + "…"
    return text


def normalize_title(title: str | None) -> str:
    """Case-, punctuation- and whitespace-insensitive form of a headline.

    Only a *leading* version token is removed (``v2.1 Foo`` -> ``foo``); the old
    skeleton deleted those tokens anywhere in the string, which mapped distinct
    releases onto one another.
    """
    if not title:
        return ""
    text = unicodedata.normalize("NFKD", html.unescape(title)).lower()
    text = _LEADING_VERSION.sub("", text)
    # A "v" glued to a digit is a version prefix wherever it appears,
    # so "Acme v2.1" and "Acme 2.1" describe the same release.
    text = re.sub(r"\bv(?=\d)", "", text)
    text = _NON_TEXT.sub(" ", text)
    text = _WS.sub(" ", text).strip()
    words = [word for word in text.split(" ") if word and word not in _STOPWORDS]
    return " ".join(words)


def title_fingerprint(title: str | None) -> str:
    """Stable 32-hex-character fingerprint of a normalised title."""
    return stable_sha256(normalize_title(title))[:32]


def stable_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()


def truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 1)].rstrip() + "…"


def domain_of(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def slugify(value: str, *, limit: int = 60) -> str:
    text = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text[:limit] or "item"


def first_paragraph(value: str, *, limit: int = 320) -> str:
    """Take the first meaningful sentence run from a body of text."""
    text = clean_text(value, max_length=4000)
    if not text:
        return ""
    for separator in ("\n\n", ". "):
        if separator in text:
            text = text.split(separator)[0]
            break
    return truncate(text.strip(), limit)
