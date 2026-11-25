"""Daily digest assembly."""

from __future__ import annotations

from datetime import date

from ai_radar.reporting.digest import DigestBuilder


def row(**overrides) -> dict:
    payload = {
        "canonical_key": "url:abc",
        "title": "Introducing Model X",
        "category": "ai_models",
        "priority_score": 72,
        "priority_label": "4/5",
        "pricing_status": "FREE_TIER",
        "url": "https://example.com/model-x",
        "source_name": "OpenAI News",
        "already_alerted": False,
        "analysis": {"practical_use_case": "Summarise a large repository."},
    }
    payload.update(overrides)
    return payload


def test_empty_digest_is_honest():
    content = DigestBuilder().build([], digest_date=date(2026, 11, 23))
    assert content.honest_empty is True
    assert content.top_count == 0
    assert "No discovery met the evidence bar" in content.text
    assert "reported honestly" in content.text


def test_digest_lists_the_top_five():
    rows = [row(canonical_key=f"url:{i}", title=f"Story {i}", priority_score=90 - i) for i in range(8)]
    content = DigestBuilder().build(rows, digest_date=date(2026, 11, 23))
    assert content.top_count == 5
    assert content.discovery_keys[:2] == ["url:0", "url:1"]
    assert "Story 0" in content.text
    assert "3 more confirmed discoveries" in content.text


def test_digest_sorts_by_priority():
    rows = [row(title="Low", priority_score=41, canonical_key="a"), row(title="High", priority_score=95, canonical_key="b")]
    content = DigestBuilder().build(rows, digest_date=date(2026, 11, 23))
    assert content.text.index("High") < content.text.index("Low")


def test_digest_marks_already_alerted_items():
    content = DigestBuilder().build(
        [row(already_alerted=True)], digest_date=date(2026, 11, 23)
    )
    assert "✓" in content.text


def test_digest_has_the_required_sections():
    rows = [
        row(canonical_key="m", category="ai_models", title="Model release"),
        row(canonical_key="c", category="ai_coding_tools", title="Coding tool"),
        row(canonical_key="f", category="image_video_ai", title="Free image model", pricing_status="FREE"),
        row(canonical_key="i", category="ai_industry_updates", title="Funding round"),
    ]
    text = DigestBuilder().build(rows, digest_date=date(2026, 11, 23)).text
    assert "Top discoveries" in text
    assert "Important model releases" in text
    assert "Best AI coding tools" in text
    assert "Useful free / open-source findings" in text
    assert "Significant industry developments" in text
    assert "Recommendation" in text


def test_recommendation_prefers_a_free_finding():
    rows = [
        row(canonical_key="paid", title="Expensive tool", pricing_status="PAID", priority_score=99),
        row(canonical_key="free", title="Free tool", pricing_status="FREE", priority_score=50,
            analysis={"practical_use_case": "Automate a weekly report."}),
    ]
    text = DigestBuilder().build(rows, digest_date=date(2026, 11, 23)).text
    recommendation = text.split("Recommendation", 1)[1]
    assert "Free tool" in recommendation


def test_digest_respects_the_telegram_length_limit():
    rows = [row(canonical_key=f"k{i}", title="Very long headline " * 10, priority_score=90) for i in range(20)]
    content = DigestBuilder().build(rows, digest_date=date(2026, 11, 23))
    assert len(content.text) <= 3800


def test_digest_escapes_html_in_titles():
    content = DigestBuilder().build([row(title="<b>bold</b> claim")], digest_date=date(2026, 11, 23))
    assert "<b>bold</b>" not in content.text
    assert "&lt;b&gt;" in content.text
