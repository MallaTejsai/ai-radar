"""Priority scoring (0-100) with an attributable breakdown.

Five sub-scores in ``0..1`` are multiplied by the configured weights
(30 / 25 / 20 / 15 / 10) and summed. The breakdown is persisted so any score can
be explained months later without re-running the pipeline.

Priority answers "how much does this matter to me?". Confidence answers "how sure
are we that it is true?". They are computed from different inputs on purpose.
"""

from __future__ import annotations

from datetime import datetime

from ..models.core import Category, PricingStatus, SourceTier, Verdict
from ..models.discovery import ScoreBreakdown, ScoreComponent
from ..settings import ScoringConfig
from ..utils.timeutil import age_days, utcnow

AVAILABILITY_RAW: dict[PricingStatus, float] = {
    PricingStatus.FREE: 1.0,
    PricingStatus.OPEN_SOURCE: 1.0,
    PricingStatus.FREE_TIER: 0.85,
    PricingStatus.PAID: 0.45,
    PricingStatus.UNVERIFIED: 0.20,
}

VERDICT_RAW: dict[Verdict, float] = {
    Verdict.CONFIRMED: 1.0,
    Verdict.PARTIAL: 0.6,
    Verdict.INSUFFICIENT: 0.2,
    Verdict.DISPROVED: 0.0,
    Verdict.ERROR: 0.0,
}

TIER_RAW: dict[SourceTier, float] = {
    SourceTier.A: 1.0,
    SourceTier.B: 0.6,
    SourceTier.C: 0.2,
}


def priority_label(score: int) -> str:
    """Map a 0-100 score onto the required 1/5 - 5/5 scale."""
    if score >= 80:
        return "5/5"
    if score >= 60:
        return "4/5"
    if score >= 40:
        return "3/5"
    if score >= 20:
        return "2/5"
    return "1/5"


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def _matches(text: str, terms: list[str]) -> list[str]:
    return [term for term in terms if term in text]


def _novelty_raw(published_at: datetime | None, now: datetime) -> tuple[float, str]:
    days = age_days(published_at, reference=now)
    if days is None:
        return 0.35, "publication date unknown"
    if days < 0:
        return 0.15, "publication date in the future"
    if days <= 2:
        return 1.0, f"published {days:.0f}d ago"
    if days <= 7:
        return 0.85, f"published {days:.0f}d ago"
    if days <= 30:
        return 0.6, f"published {days:.0f}d ago"
    if days <= 90:
        return 0.35, f"published {days:.0f}d ago"
    return 0.1, f"published {days:.0f}d ago"


def score_priority(
    *,
    category: Category,
    text: str,
    published_at: datetime | None,
    pricing_status: PricingStatus,
    verdict: Verdict,
    tiers: set[SourceTier],
    distinct_domains: int,
    config: ScoringConfig,
    now: datetime | None = None,
) -> ScoreBreakdown:
    """Compute the weighted priority score for one discovery."""
    now = now or utcnow()
    haystack = " " + " ".join((text or "").lower().split()) + " "
    signals = config.signals

    major = _matches(haystack, signals.major_release)
    capability = _matches(haystack, signals.capability)
    minor = _matches(haystack, signals.minor_update)

    technical = 0.30
    if major:
        technical += 0.40
    if capability:
        technical += 0.20
    if minor:
        technical -= 0.30
    technical = _clamp(technical)
    technical_reason = ", ".join(filter(None, [
        f"release signals: {len(major)}" if major else "",
        f"capability signals: {len(capability)}" if capability else "",
        f"routine-update signals: {len(minor)}" if minor else "",
    ])) or "no strong release signal"

    usefulness = config.category_usefulness.get(category, 0.5)

    availability = AVAILABILITY_RAW[pricing_status]

    novelty, novelty_reason = _novelty_raw(published_at, now)

    best_tier = SourceTier.A if SourceTier.A in tiers else (
        SourceTier.B if SourceTier.B in tiers else SourceTier.C
    )
    corroboration_raw = min(1.0, max(0.0, (distinct_domains - 1) / 2))
    evidence_raw = _clamp(
        0.25 * VERDICT_RAW[verdict] + 0.50 * TIER_RAW[best_tier] + 0.25 * corroboration_raw
    )

    weights = config.weights
    components = [
        ScoreComponent(
            name="technical_significance",
            raw=technical,
            weight=weights.technical_significance,
            points=round(technical * weights.technical_significance, 2),
            rationale=technical_reason,
        ),
        ScoreComponent(
            name="personal_usefulness",
            raw=usefulness,
            weight=weights.personal_usefulness,
            points=round(usefulness * weights.personal_usefulness, 2),
            rationale=f"configured relevance for {category.value}",
        ),
        ScoreComponent(
            name="availability_accessibility",
            raw=availability,
            weight=weights.availability_accessibility,
            points=round(availability * weights.availability_accessibility, 2),
            rationale=f"pricing status {pricing_status.value}",
        ),
        ScoreComponent(
            name="novelty",
            raw=novelty,
            weight=weights.novelty,
            points=round(novelty * weights.novelty, 2),
            rationale=novelty_reason,
        ),
        ScoreComponent(
            name="evidence_quality",
            raw=evidence_raw,
            weight=weights.evidence_quality,
            points=round(evidence_raw * weights.evidence_quality, 2),
            rationale=(
                f"verdict {verdict.value}, best tier {best_tier.value}, "
                f"{distinct_domains} distinct domain(s)"
            ),
        ),
    ]

    total = int(round(sum(component.points for component in components)))
    total = max(0, min(100, total))
    return ScoreBreakdown(components=components, total=total, label=priority_label(total))
