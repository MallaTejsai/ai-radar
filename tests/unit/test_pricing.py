"""Pricing and licence classification."""

from __future__ import annotations

import pytest

from ai_radar.models.core import PricingStatus
from ai_radar.processing.pricing import classify_pricing, describe
from ai_radar.settings import SignalsConfig, load_config


@pytest.fixture(scope="module")
def signals() -> SignalsConfig:
    return load_config().scoring.signals


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("The new model has a generous free tier for developers", PricingStatus.FREE_TIER),
        ("Open weights released under the Apache 2.0 licence", PricingStatus.OPEN_SOURCE),
        ("Now available under the MIT license", PricingStatus.OPEN_SOURCE),
        ("Pricing starts at $20 per seat", PricingStatus.PAID),
        ("An unremarkable changelog entry with no commercial detail", PricingStatus.UNVERIFIED),
    ],
)
def test_classification_table(signals, text, expected):
    assert classify_pricing(text, signals).status is expected


def test_free_trial_is_not_reported_as_free(signals):
    result = classify_pricing("Start a 14-day free trial today", signals)
    assert result.status is PricingStatus.PAID
    assert "trial" in result.reason


def test_open_weights_without_a_licence_is_unverified(signals):
    result = classify_pricing("We released the open weights for Model Y", signals)
    assert result.status is PricingStatus.UNVERIFIED
    assert "no licence" in result.reason


def test_restricted_licence_is_flagged(signals):
    result = classify_pricing("Released under the Llama Community License", signals)
    assert result.status is PricingStatus.OPEN_SOURCE
    assert result.restricted is True


def test_always_free_language_is_free_not_free_tier(signals):
    result = classify_pricing("The tool is free to use, no cost at all", signals)
    assert result.status is PricingStatus.FREE


def test_unverified_is_the_default_and_is_explicit(signals):
    result = classify_pricing("", signals)
    assert result.status is PricingStatus.UNVERIFIED
    assert result.matched == ()


def test_describe_includes_restriction_note(signals):
    result = classify_pricing("Released under the Llama Community License", signals)
    assert "restricted" in describe(result)
