"""Telegram message rendering.

Uses ``parse_mode=HTML`` rather than MarkdownV2. MarkdownV2 requires escaping 18
reserved characters including ``.``, ``-`` and ``!``; missing one makes Telegram
reject the whole message. In HTML only ``&``, ``<`` and ``>`` need escaping, and
``html.escape`` cannot get that wrong.

Every dynamic value passes through :func:`escape`, including model-generated text
and source names, because both are untrusted.
"""

from __future__ import annotations

from datetime import datetime
from html import escape
from typing import Any, Mapping, Sequence

from ..utils.text import truncate
from ..utils.timeutil import to_ist

MAX_MESSAGE_LENGTH = 3800

PRIORITY_ICON = {"5/5": "🔥", "4/5": "⭐", "3/5": "📋", "2/5": "📄", "1/5": "⚪"}

PRICING_LABEL = {
    "FREE": "Free",
    "FREE_TIER": "Free tier",
    #: Only set when a licence or an explicit open-source claim was found. Open
    #: weights without a licence classify as UNVERIFIED, so this label must not
    #: imply that open weights are open source.
    "OPEN_SOURCE": "Open source",
    "PAID": "Paid",
    "UNVERIFIED": "Unverified",
}


def esc(value: Any) -> str:
    """Escape a value for Telegram HTML text nodes."""
    if value is None:
        return ""
    return escape(str(value), quote=False)


def esc_attr(value: Any) -> str:
    """Escape a value for an HTML attribute (``href="..."``).

    A text-node escape deliberately leaves ``"`` alone, which would terminate the
    attribute early if a URL contained one. Telegram then rejects the whole
    message with ``bad_request``, which is permanent for that item, so attribute
    values get the quote-aware escape.
    """
    if value is None:
        return ""
    return escape(str(value), quote=True)


def _iso_date(value: Any) -> str:
    if isinstance(value, datetime):
        return to_ist(value).strftime("%d %b %Y")
    if isinstance(value, str) and value:
        return value[:10]
    return "unknown"


def _join(items: Sequence[str], limit: int = 3) -> str:
    cleaned = [item for item in items if item]
    return ", ".join(cleaned[:limit])


def build_alert_message(
    row: Mapping[str, Any],
    *,
    category_names: Mapping[str, str] | None = None,
    max_length: int = MAX_MESSAGE_LENGTH,
) -> str:
    """Render one discovery alert.

    ``row`` is the joined discovery row (with ``analysis`` and ``source_name``).
    Missing analysis fields degrade to an explicit "not stated" rather than
    silence, so an alert can never look more certain than the data behind it.
    """
    analysis: Mapping[str, Any] = row.get("analysis") or {}
    category = str(row.get("category", ""))
    category_label = (category_names or {}).get(category, category or "unknown")
    priority_label = str(row.get("priority_label") or "")
    icon = PRIORITY_ICON.get(priority_label, "•")
    confidence = float(row.get("confidence") or 0.0)
    pricing = str(row.get("pricing_status") or "UNVERIFIED")
    restricted = bool(row.get("pricing_restricted"))
    pricing_text = PRICING_LABEL.get(pricing, pricing)
    if restricted:
        pricing_text += " (restricted licence)"

    lines: list[str] = []
    lines.append(f"<b>{esc(row.get('title'))}</b>")
    lines.append(
        f"{icon} <b>{esc(priority_label)}</b> · {esc(category_label)} · "
        f"confidence {confidence:.0%}"
    )

    summary = str(analysis.get("summary") or row.get("summary") or "").strip()
    if summary:
        lines.append("")
        lines.append("<b>Summary</b>")
        lines.append(esc(truncate(summary, 320)))

    what_is_new = str(analysis.get("what_is_new") or "").strip()
    if what_is_new:
        lines.append("")
        lines.append("<b>What is new</b>")
        lines.append(esc(truncate(what_is_new, 220)))

    why = str(analysis.get("why_it_matters") or "").strip()
    if why:
        lines.append("")
        lines.append("<b>Why it matters</b>")
        lines.append(esc(truncate(why, 200)))

    use_case = str(analysis.get("practical_use_case") or "").strip()
    if use_case:
        lines.append("")
        lines.append("<b>Practical use case</b>")
        lines.append(esc(truncate(use_case, 200)))

    capabilities = analysis.get("capabilities") or []
    if isinstance(capabilities, list) and capabilities:
        lines.append("")
        lines.append(f"<b>Capabilities</b> {esc(_join([str(c) for c in capabilities], 4))}")

    lines.append("")
    lines.append(f"<b>Pricing / licence</b> {esc(pricing_text)}")
    if pricing == "UNVERIFIED":
        lines.append("<i>No pricing statement was found in the collected evidence.</i>")

    lines.append(f"<b>Published</b> {esc(_iso_date(row.get('published_at')))}")

    url = str(row.get("url") or "").strip()
    source_name = str(row.get("source_name") or row.get("source_id") or "source")
    if url.startswith(("http://", "https://")):
        lines.append(f'<b>Evidence</b> <a href="{esc_attr(url)}">{esc(source_name)}</a>')
    else:
        lines.append(f"<b>Evidence</b> {esc(source_name)}")

    verdict = str(row.get("verdict") or "")
    if verdict:
        lines.append(f"<i>Verdict {esc(verdict)} · verified against live sources</i>")

    text = "\n".join(lines)
    if len(text) > max_length:
        text = text[: max_length - 3].rstrip() + "…"
    return text


def build_digest_header(digest_date: datetime | None = None) -> str:
    moment = to_ist(digest_date) if digest_date else None
    stamp = moment.strftime("%d %b %Y") if moment else ""
    return f"<b>AI Radar — daily digest</b>{f' · {stamp}' if stamp else ''}"
