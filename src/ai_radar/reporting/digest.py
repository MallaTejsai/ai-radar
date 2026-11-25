"""Daily digest rendering.

The digest is built from stored, CONFIRMED discoveries only - never from a raw
feed and never from a PARTIAL verdict. Each item is marked when it was already
sent as an hourly alert, so the digest complements the alerts instead of
repeating them verbatim.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Mapping, Sequence

from ..models.backup import DigestContent
from ..notifications.format import (
    PRIORITY_ICON,
    PRICING_LABEL,
    build_digest_header,
    esc,
    esc_attr,
)
from ..utils.text import truncate
from ..utils.timeutil import today_ist, to_ist, utcnow

MAX_TELEGRAM_LENGTH = 3800


def _title(row: Mapping[str, Any], limit: int = 90) -> str:
    return truncate(str(row.get("title") or "Untitled"), limit)


def _link(row: Mapping[str, Any]) -> str:
    url = str(row.get("url") or "")
    if not url.startswith(("http://", "https://")):
        return ""
    label = str(row.get("source_name") or "source")
    return f'<a href="{esc_attr(url)}">{esc(truncate(label, 28))}</a>'


def _item(row: Mapping[str, Any], index: int | None = None) -> str:
    prefix = f"{index}. " if index else "• "
    icon = PRIORITY_ICON.get(str(row.get("priority_label") or ""), "•")
    pricing = PRICING_LABEL.get(str(row.get("pricing_status") or "UNVERIFIED"), "Unverified")
    alerted = " ✓" if row.get("already_alerted") else ""
    parts = [f"{prefix}{icon} <b>{esc(_title(row))}</b>{alerted}"]
    parts.append(f"   {esc(pricing)} · {esc(row.get('category') or '')}")
    link = _link(row)
    if link:
        parts.append(f"   {link}")
    return "\n".join(parts)


class DigestBuilder:
    """Assembles the seven required digest sections."""

    def __init__(self, *, category_names: Mapping[str, str] | None = None) -> None:
        self._category_names = dict(category_names or {})

    def build(
        self, rows: Sequence[Mapping[str, Any]], *, digest_date: date | None = None
    ) -> DigestContent:
        digest_date = digest_date or today_ist()
        if not rows:
            return DigestContent(
                digest_date=digest_date,
                text=self._empty_text(digest_date),
                discovery_keys=[],
                top_count=0,
                honest_empty=True,
            )

        ordered = sorted(rows, key=lambda row: row.get("priority_score") or 0, reverse=True)
        blocks: list[str] = [build_digest_header(), ""]
        budget = MAX_TELEGRAM_LENGTH

        def add(block: str) -> bool:
            nonlocal budget
            if len(block) + 2 > budget:
                return False
            blocks.append(block)
            budget -= len(block) + 2
            return True

        top = ordered[:5]
        add("<b>Top discoveries</b>")
        for index, row in enumerate(top, 1):
            if not add(_item(row, index)):
                break

        sections: list[tuple[str, str]] = [
            ("ai_models", "Important model releases"),
            ("ai_coding_tools", "Best AI coding tools"),
            ("__free__", "Useful free / open-source findings"),
            ("ai_industry_updates", "Significant industry developments"),
        ]

        for key, heading in sections:
            if key == "__free__":
                subset = [
                    row
                    for row in ordered
                    if str(row.get("pricing_status")) in {"FREE", "FREE_TIER", "OPEN_SOURCE"}
                ][:3]
            else:
                subset = [row for row in ordered if str(row.get("category")) == key][:3]
            if not subset:
                continue
            block_lines = [f"<b>{esc(heading)}</b>"]
            block_lines.extend(_item(row) for row in subset)
            add("\n".join(block_lines))

        recommendation = self._recommendation(ordered)
        if recommendation:
            add(f"<b>Recommendation</b>\n{esc(recommendation)}")

        links = [
            f'• <a href="{esc_attr(row.get("url"))}">{esc(truncate(_title(row), 60))}</a>'
            for row in top[:5]
            if str(row.get("url") or "").startswith(("http://", "https://"))
        ]
        if links:
            add("<b>Sources</b>\n" + "\n".join(links))

        if len(ordered) > len(top):
            add(f"<i>{len(ordered) - len(top)} more confirmed discoveries are stored in the database.</i>")

        text = "\n\n".join(blocks)
        return DigestContent(
            digest_date=digest_date,
            text=text[:MAX_TELEGRAM_LENGTH],
            discovery_keys=[str(row.get("canonical_key")) for row in top],
            top_count=len(top),
            honest_empty=False,
        )

    @staticmethod
    def _recommendation(rows: Sequence[Mapping[str, Any]]) -> str:
        if not rows:
            return ""
        free_first = [
            row
            for row in rows
            if str(row.get("pricing_status")) in {"FREE", "FREE_TIER", "OPEN_SOURCE"}
        ]
        pick = free_first[0] if free_first else rows[0]
        analysis = pick.get("analysis") or {}
        use_case = str(analysis.get("practical_use_case") or "").strip()
        title = _title(pick, 70)
        if use_case:
            return f"{title} — {truncate(use_case, 200)}"
        return f"Try {title} on one small, reversible task before adopting it more widely."

    @staticmethod
    def _empty_text(digest_date: date) -> str:
        stamp = to_ist(utcnow()).strftime("%d %b %Y")
        return (
            f"<b>AI Radar — daily digest · {stamp}</b>\n\n"
            "No discovery met the evidence bar today: nothing reached a CONFIRMED "
            "verdict with priority 3/5 or higher.\n\n"
            "This is reported honestly rather than padded. The hourly research runs "
            "continue, and the heartbeat monitor confirms the pipeline is alive."
        )
