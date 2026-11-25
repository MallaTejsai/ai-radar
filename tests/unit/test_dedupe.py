"""Deterministic de-duplication."""

from __future__ import annotations

from support.helpers import make_entry
from ai_radar.models.core import Category, SourceTier
from ai_radar.processing.dedupe import dedupe_batch, merge_evidence


def test_identical_url_is_merged():
    first = make_entry(url="https://example.com/a", title="Model X released")
    second = make_entry(url="https://example.com/a?utm_source=rss", title="Model X released")
    result = dedupe_batch([first, second])
    assert len(result.unique) == 1
    assert result.removed == 1


def test_same_headline_on_two_sites_is_merged_within_a_category():
    first = make_entry(url="https://vendor.com/x", title="Introducing Model X", source_id="openai_news")
    second = make_entry(url="https://news.com/x", title="Introducing Model X", source_id="techcrunch_ai")
    result = dedupe_batch([first, second])
    assert len(result.unique) == 1
    assert len(result.merged[first.canonical_key]) == 1


def test_same_headline_across_categories_is_not_merged():
    first = make_entry(
        url="https://vendor.com/x", title="Introducing our new release", category=Category.AI_MODELS
    )
    second = make_entry(
        url="https://other.com/y",
        title="Introducing our new release",
        category=Category.AI_CODING_TOOLS,
    )
    result = dedupe_batch([first, second])
    assert len(result.unique) == 2


def test_order_is_preserved_for_unique_entries():
    entries = [
        make_entry(url=f"https://example.com/{index}", title=f"Distinct headline {index}")
        for index in range(5)
    ]
    result = dedupe_batch(entries)
    assert [entry.url for entry in result.unique] == [entry.url for entry in entries]


def test_merge_evidence_deduplicates_identical_rows():
    primary = make_entry(url="https://example.com/a")
    duplicate = make_entry(url="https://example.com/a")
    merged = merge_evidence(primary, [duplicate])
    assert len(merged) == 1


def test_merge_evidence_keeps_distinct_corroboration():
    primary = make_entry(url="https://example.com/a", tier=SourceTier.A)
    other = make_entry(url="https://news.com/a", source_id="techcrunch_ai", tier=SourceTier.B)
    merged = merge_evidence(primary, [other])
    assert {item.tier for item in merged} == {SourceTier.A, SourceTier.B}
