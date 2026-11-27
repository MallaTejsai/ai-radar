"""Confidence scoring - the measurement that gates discards."""

from __future__ import annotations

from ai_radar.models.core import SourceTier, Verdict
from ai_radar.processing.confidence import (
    CAP_TIER_B_ONLY,
    CAP_TIER_C_ONLY,
    explain_confidence,
)


def test_confirmed_tier_a_reaches_full_band():
    breakdown = explain_confidence(
        verdict=Verdict.CONFIRMED,
        tiers={SourceTier.A},
        distinct_domains=2,
        reachable=True,
        date_verified=True,
    )
    assert breakdown.total == 0.94
    assert breakdown.cap_applied is None


def test_tier_c_alone_can_never_pass_the_gate():
    breakdown = explain_confidence(
        verdict=Verdict.CONFIRMED,
        tiers={SourceTier.C},
        distinct_domains=5,
        reachable=True,
        date_verified=True,
    )
    assert breakdown.total == CAP_TIER_C_ONLY
    assert breakdown.cap_applied == "tier_c_only"


def test_tier_b_alone_is_capped_below_the_top_band():
    breakdown = explain_confidence(
        verdict=Verdict.CONFIRMED,
        tiers={SourceTier.B},
        distinct_domains=4,
        reachable=True,
        date_verified=True,
    )
    assert breakdown.total == CAP_TIER_B_ONLY
    assert breakdown.cap_applied == "tier_b_only"


def test_disproved_scores_zero():
    breakdown = explain_confidence(verdict=Verdict.DISPROVED, tiers={SourceTier.A})
    assert breakdown.total == 0.0
    assert breakdown.cap_applied == "disproved_or_error"


def test_missing_date_and_unreachable_evidence_lowers_the_score():
    full = explain_confidence(
        verdict=Verdict.CONFIRMED, tiers={SourceTier.A}, reachable=True, date_verified=True
    )
    reduced = explain_confidence(
        verdict=Verdict.CONFIRMED, tiers={SourceTier.A}, reachable=None, date_verified=False
    )
    assert reduced.total < full.total
    assert (full.total - reduced.total) == 0.13


def test_corroboration_bonus_is_capped():
    many = explain_confidence(
        verdict=Verdict.CONFIRMED, tiers={SourceTier.A}, distinct_domains=10, reachable=True
    )
    assert many.corroboration_bonus == 0.12


def test_breakdown_is_serialisable_for_storage():
    payload = explain_confidence(verdict=Verdict.PARTIAL, tiers={SourceTier.A}).as_dict()
    assert payload["total"] <= 1.0
    assert set(payload) >= {"total", "base", "tier_bonus", "cap_applied"}
