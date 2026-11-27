"""Priority scoring: weights, monotonicity and the 1/5-5/5 mapping."""

from __future__ import annotations

from datetime import timedelta

import pytest

from ai_radar.models.core import Category, PricingStatus, SourceTier, Verdict
from ai_radar.processing.scoring import priority_label, score_priority
from ai_radar.settings import ScoringConfig, load_config
from ai_radar.utils.timeutil import utcnow


@pytest.fixture(scope="module")
def scoring_config() -> ScoringConfig:
    return load_config().scoring


def _score(config, **overrides):
    defaults = dict(
        category=Category.AI_MODELS,
        text="Introducing a new model for agentic coding with multimodal reasoning",
        published_at=utcnow(),
        pricing_status=PricingStatus.FREE_TIER,
        verdict=Verdict.CONFIRMED,
        tiers={SourceTier.A},
        distinct_domains=1,
    )
    defaults.update(overrides)
    return score_priority(config=config, **defaults)


@pytest.mark.parametrize(
    ("score", "label"),
    [(0, "1/5"), (19, "1/5"), (20, "2/5"), (39, "2/5"), (40, "3/5"), (59, "3/5"),
     (60, "4/5"), (79, "4/5"), (80, "5/5"), (100, "5/5")],
)
def test_priority_label_boundaries(score, label):
    assert priority_label(score) == label


def test_weights_must_total_one_hundred(scoring_config):
    weights = scoring_config.weights
    total = (
        weights.technical_significance
        + weights.personal_usefulness
        + weights.availability_accessibility
        + weights.novelty
        + weights.evidence_quality
    )
    assert total == 100


def test_score_is_bounded_and_has_five_components(scoring_config):
    breakdown = _score(scoring_config)
    assert 0 <= breakdown.total <= 100
    assert len(breakdown.components) == 5
    assert sum(component.points for component in breakdown.components) == pytest.approx(
        breakdown.total, abs=1
    )


def test_free_access_scores_higher_than_paid(scoring_config):
    free = _score(scoring_config, pricing_status=PricingStatus.FREE)
    paid = _score(scoring_config, pricing_status=PricingStatus.PAID)
    assert free.total > paid.total


def test_routine_update_scores_lower_than_major_release(scoring_config):
    major = _score(scoring_config, text="Introducing a new model, state of the art reasoning")
    minor = _score(scoring_config, text="Patch release: bug fix and documentation typo")
    assert major.total > minor.total


def test_old_discovery_scores_lower_on_novelty(scoring_config):
    fresh = _score(scoring_config, published_at=utcnow())
    old = _score(scoring_config, published_at=utcnow() - timedelta(days=200))
    assert fresh.total > old.total


def test_unknown_date_is_bounded_not_maximal(scoring_config):
    unknown = _score(scoring_config, published_at=None)
    fresh = _score(scoring_config, published_at=utcnow())
    assert unknown.total < fresh.total


def test_evidence_quality_reflects_verdict_and_tier(scoring_config):
    strong = _score(scoring_config, verdict=Verdict.CONFIRMED, tiers={SourceTier.A})
    weak = _score(scoring_config, verdict=Verdict.PARTIAL, tiers={SourceTier.C})
    assert strong.total > weak.total


def test_breakdown_records_a_rationale_per_component(scoring_config):
    breakdown = _score(scoring_config)
    assert all(component.rationale for component in breakdown.components)
    assert breakdown.label == priority_label(breakdown.total)
