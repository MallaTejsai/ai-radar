"""Evidence confidence (0..1) - a measurement, not a rescaled priority score.

The score is additive and fully explainable, so a reviewer can see exactly which
evidence produced it. Hard caps encode two policy decisions:

* Tier C alone can never pass the gate (cap 0.49, below any sane threshold).
* Tier B alone cannot reach the top band (cap 0.69) because it is reporting,
  not the publisher's own announcement.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..models.core import SourceTier, Verdict

VERDICT_BASE: dict[Verdict, float] = {
    Verdict.CONFIRMED: 0.55,
    Verdict.PARTIAL: 0.38,
    Verdict.INSUFFICIENT: 0.18,
    Verdict.DISPROVED: 0.0,
    Verdict.ERROR: 0.0,
}

TIER_BONUS: dict[SourceTier, float] = {
    SourceTier.A: 0.20,
    SourceTier.B: 0.10,
    SourceTier.C: 0.0,
}

CAP_TIER_C_ONLY = 0.49
CAP_TIER_B_ONLY = 0.69
MAX_CORROBORATION_BONUS = 0.12


@dataclass(frozen=True, slots=True)
class ConfidenceBreakdown:
    total: float
    base: float
    tier_bonus: float
    corroboration_bonus: float
    availability_bonus: float
    date_bonus: float
    cap_applied: str | None = None

    def as_dict(self) -> dict[str, float | str | None]:
        return {
            "total": round(self.total, 3),
            "base": self.base,
            "tier_bonus": self.tier_bonus,
            "corroboration_bonus": round(self.corroboration_bonus, 3),
            "availability_bonus": self.availability_bonus,
            "date_bonus": self.date_bonus,
            "cap_applied": self.cap_applied,
        }


def explain_confidence(
    *,
    verdict: Verdict,
    tiers: set[SourceTier],
    distinct_domains: int = 1,
    reachable: bool | None = None,
    date_verified: bool = False,
) -> ConfidenceBreakdown:
    """Compute the confidence score with every contribution itemised."""
    base = VERDICT_BASE.get(verdict, 0.0)
    if base == 0.0:
        return ConfidenceBreakdown(
            total=0.0,
            base=0.0,
            tier_bonus=0.0,
            corroboration_bonus=0.0,
            availability_bonus=0.0,
            date_bonus=0.0,
            cap_applied="disproved_or_error",
        )

    best_tier = SourceTier.A if SourceTier.A in tiers else (
        SourceTier.B if SourceTier.B in tiers else SourceTier.C
    )
    tier_bonus = TIER_BONUS[best_tier]

    corroboration_bonus = 0.0
    if distinct_domains > 1:
        corroboration_bonus = min(MAX_CORROBORATION_BONUS, 0.06 * (distinct_domains - 1))

    availability_bonus = 0.08 if reachable else 0.0
    date_bonus = 0.05 if date_verified else 0.0

    total = base + tier_bonus + corroboration_bonus + availability_bonus + date_bonus
    cap: str | None = None
    if best_tier is SourceTier.C:
        if total > CAP_TIER_C_ONLY:
            total = CAP_TIER_C_ONLY
            cap = "tier_c_only"
    elif best_tier is SourceTier.B:
        if total > CAP_TIER_B_ONLY:
            total = CAP_TIER_B_ONLY
            cap = "tier_b_only"

    return ConfidenceBreakdown(
        total=round(min(total, 1.0), 3),
        base=base,
        tier_bonus=tier_bonus,
        corroboration_bonus=corroboration_bonus,
        availability_bonus=availability_bonus,
        date_bonus=date_bonus,
        cap_applied=cap,
    )


def compute_confidence(**kwargs: object) -> float:
    """Convenience wrapper returning only the numeric confidence."""
    return explain_confidence(**kwargs).total  # type: ignore[arg-type]
