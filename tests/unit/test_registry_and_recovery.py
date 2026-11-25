"""Recovery planning and the source registry contract."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from ai_radar.models.core import Category, CollectionMethod, SourceTier
from ai_radar.models.source import SourceRegistry, SourceSpec
from ai_radar.operations.recovery import RECOVERY_WIDEN_FACTOR, plan_recovery


# --------------------------------------------------------------------- recovery
def test_no_gap_means_no_widening():
    now = datetime(2026, 11, 23, 10, 30, tzinfo=timezone.utc)
    plan = plan_recovery(
        now=now,
        last_successful_run=now - timedelta(hours=1),
        successful_runs=[now - timedelta(hours=1), now],
        window_hours=24,
    )
    assert plan.recovering is False
    assert plan.widen_factor == 1
    assert plan.effective_max_items(40) == 40


def test_gap_widens_collection_and_is_reported():
    now = datetime(2026, 11, 23, 12, 30, tzinfo=timezone.utc)
    plan = plan_recovery(now=now, last_successful_run=now - timedelta(hours=8), window_hours=24)
    assert plan.recovering is True
    assert plan.widen_factor == RECOVERY_WIDEN_FACTOR
    assert plan.effective_max_items(40) == 80
    assert "missed slot" in plan.summary()


def test_effective_items_are_capped():
    now = datetime(2026, 11, 23, 12, 30, tzinfo=timezone.utc)
    plan = plan_recovery(now=now, last_successful_run=now - timedelta(hours=20), window_hours=24)
    assert plan.effective_max_items(90) == 100


def test_recovery_never_exceeds_the_window():
    now = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)
    plan = plan_recovery(now=now, last_successful_run=now - timedelta(days=10), window_hours=24)
    assert all(slot >= plan.window_start for slot in plan.missed_slots)


# --------------------------------------------------------------------- registry
def spec(**overrides) -> SourceSpec:
    payload = {
        "id": "example_source",
        "name": "Example",
        "category": Category.AI_MODELS,
        "tier": SourceTier.A,
        "method": CollectionMethod.RSS,
        "url": "https://example.com/feed.xml",
    }
    payload.update(overrides)
    return SourceSpec(**payload)


def test_shipped_registry_is_valid(config):
    assert config.sources.validate_registry() == []
    assert 15 <= len(config.sources.sources) <= 25


def test_shipped_registry_covers_all_seven_categories(config):
    covered = {source.category for source in config.sources.sources}
    assert covered == set(Category)


def test_registry_rejects_plain_http():
    with pytest.raises(ValidationError):
        spec(url="http://example.com/feed.xml")


def test_registry_rejects_unsafe_ids():
    with pytest.raises(ValidationError):
        spec(id="Bad Id!")


def test_registry_detects_duplicate_ids():
    registry = SourceRegistry(sources=[spec(), spec(name="Second")])
    assert any("duplicate" in problem for problem in registry.validate_registry())


def test_registry_detects_missing_category_coverage():
    registry = SourceRegistry(sources=[spec() for _ in range(15)])
    registry.sources = [spec(id=f"source_{i}") for i in range(15)]
    problems = registry.validate_registry()
    assert any("categories not covered" in problem for problem in problems)


def test_registry_detects_too_few_sources():
    registry = SourceRegistry(sources=[spec()])
    assert any("15-25" in problem for problem in registry.validate_registry())


def test_source_spec_forbids_unknown_fields():
    with pytest.raises(ValidationError):
        spec(unexpected="value")
