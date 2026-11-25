"""Telegram message rendering (HTML parse mode)."""

from __future__ import annotations

from datetime import datetime, timezone

from ai_radar.notifications.format import build_alert_message, esc, esc_attr


def row(**overrides) -> dict:
    payload = {
        "title": "Introducing Model X",
        "category": "ai_models",
        "priority_label": "4/5",
        "priority_score": 72,
        "confidence": 0.94,
        "pricing_status": "FREE_TIER",
        "pricing_restricted": False,
        "published_at": datetime(2026, 11, 23, 6, 0, tzinfo=timezone.utc),
        "url": "https://example.com/model-x",
        "source_name": "OpenAI News",
        "verdict": "CONFIRMED",
        "summary": "fallback summary",
        "analysis": {
            "summary": "A new model with long context support.",
            "what_is_new": "Long context and agentic coding.",
            "why_it_matters": "Cheaper long-document work.",
            "practical_use_case": "Summarise a large repository.",
            "capabilities": ["long context", "agentic coding"],
        },
    }
    payload.update(overrides)
    return payload


def test_escape_handles_all_html_metacharacters():
    assert esc("a < b & c > d") == "a &lt; b &amp; c &gt; d"


def test_message_contains_every_required_section():
    message = build_alert_message(row(), category_names={"ai_models": "AI Models"})
    for fragment in (
        "Introducing Model X",
        "AI Models",
        "4/5",
        "Summary",
        "What is new",
        "Why it matters",
        "Practical use case",
        "Pricing / licence",
        "Published",
        "Evidence",
    ):
        assert fragment in message


def test_unverified_pricing_is_stated_explicitly():
    message = build_alert_message(row(pricing_status="UNVERIFIED"))
    assert "Unverified" in message
    assert "No pricing statement was found" in message


def test_restricted_licence_is_called_out():
    message = build_alert_message(row(pricing_status="OPEN_SOURCE", pricing_restricted=True))
    assert "restricted licence" in message


def test_hostile_content_is_escaped_not_executed():
    message = build_alert_message(
        row(title="<script>alert(1)</script>", source_name='<b>evil</b>')
    )
    assert "<script>" not in message
    assert "&lt;script&gt;" in message
    assert "<b>evil</b>" not in message


def test_message_is_truncated_below_the_telegram_limit():
    long_text = "word " * 2000
    message = build_alert_message(
        row(
            summary="",
            analysis={
                "summary": long_text,
                "what_is_new": long_text,
                "why_it_matters": long_text,
                "practical_use_case": long_text,
            },
        ),
        max_length=600,
    )
    assert len(message) <= 600
    assert message.endswith("…")


def test_missing_analysis_degrades_gracefully():
    message = build_alert_message(row(analysis=None))
    assert "Introducing Model X" in message
    # Falls back to the collected summary rather than inventing one.
    assert "fallback summary" in message
    assert "What is new" not in message


def test_only_http_urls_become_links():
    message = build_alert_message(row(url="javascript:alert(1)"))
    assert "javascript:" not in message
    assert "<a href" not in message


def test_attribute_escape_quotes_urls_inside_href():
    # A text-node escape leaves `"` intact, which would close the attribute early
    # and make Telegram reject the whole message permanently for that item.
    quote_url = 'https://example.com/a"b?x=1&y=2'
    message = build_alert_message(row(url=quote_url))
    assert 'href="https://example.com/a&quot;b?x=1&amp;y=2"' in message
    assert '" a' not in message


def test_open_weights_are_not_labelled_open_source():
    assert esc_attr('/a"b') == "/a&quot;b"
    message = build_alert_message(row(pricing_status="OPEN_SOURCE"))
    assert "Open source" in message
    assert "open weights" not in message.lower()
