"""Pricing / licensing classification.

Rules that the specification is explicit about, and which this module enforces:

* A free *trial* is never evidence of a free offering.
* Open **weights** are not the same thing as an open-source *license*.
* When the text does not say, the answer is ``UNVERIFIED`` - never a guess.

Classification runs on collector text only (headline + summary). Model output is
stored as advisory and never overwrites this result.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..models.core import PricingStatus
from ..settings import SignalsConfig

#: Strings that indicate an actual licence grant.
LICENSE_TERMS: tuple[str, ...] = (
    "apache 2.0",
    "apache-2.0",
    "apache license",
    "mit license",
    "bsd-3",
    "bsd 3",
    "gpl-3",
    "gplv3",
    "lgpl",
    "mozilla public license",
    "cc-by-4.0",
    "cc by 4.0",
)

#: Licences that permit use but carry restrictions worth flagging to the reader.
RESTRICTED_LICENSE_TERMS: tuple[str, ...] = (
    "llama community license",
    "non-commercial",
    "noncommercial",
    "research only",
    "cc-by-nc",
    "openrail",
    "source-available",
)


@dataclass(frozen=True, slots=True)
class PricingAssessment:
    status: PricingStatus
    reason: str
    matched: tuple[str, ...] = ()
    restricted: bool = False

    @property
    def is_free_access(self) -> bool:
        return self.status in (PricingStatus.FREE, PricingStatus.FREE_TIER, PricingStatus.OPEN_SOURCE)


def _hits(haystack: str, terms: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    return tuple(term for term in terms if term and term.lower() in haystack)


def classify_pricing(text: str, signals: SignalsConfig) -> PricingAssessment:
    """Classify pricing status from announcement text."""
    haystack = " " + " ".join((text or "").lower().split()) + " "

    free_tier = _hits(haystack, signals.free_signal)
    trial = _hits(haystack, signals.free_trial_signal)
    paid = _hits(haystack, signals.paid_signal)
    license_hits = _hits(haystack, LICENSE_TERMS)
    restricted_hits = _hits(haystack, RESTRICTED_LICENSE_TERMS)
    # "open weights" is a distribution choice, not a licence grant.
    weights_only = _hits(haystack, ("open weights", "open-weights", "weights released", "open-weight"))
    open_source_literal = _hits(haystack, ("open source", "open-source"))

    # 1. An explicit free tier/plan is the strongest free-access signal.
    tier_terms = _hits(haystack, ("free tier", "free plan", "free level", "free usage tier"))
    if tier_terms:
        return PricingAssessment(
            status=PricingStatus.FREE_TIER,
            reason="explicit free tier/plan stated in the announcement",
            matched=tier_terms,
        )

    # 2. An actual licence (or an explicit open-source claim).
    if license_hits or restricted_hits or open_source_literal:
        matched = license_hits + restricted_hits + open_source_literal
        restricted = bool(restricted_hits)
        return PricingAssessment(
            status=PricingStatus.OPEN_SOURCE,
            reason=(
                "published under a licence with restrictions - read it before commercial use"
                if restricted
                else "open-source licence stated"
            ),
            matched=matched,
            restricted=restricted,
        )

    # 3. Weights without a licence are *not* open source, and not free either.
    if weights_only:
        return PricingAssessment(
            status=PricingStatus.UNVERIFIED,
            reason="weights were published but no licence was stated",
            matched=weights_only,
        )

    # 3. A trial is not a free offering.
    if trial and not free_tier:
        return PricingAssessment(
            status=PricingStatus.PAID,
            reason="trial-only access mentioned; a trial does not make the product free",
            matched=trial,
        )

    # 4. Genuine no-cost language.
    if free_tier:
        return PricingAssessment(
            status=PricingStatus.FREE,
            reason="no-cost access stated explicitly",
            matched=free_tier,
        )

    # 5. Explicit commercial language.
    if paid:
        return PricingAssessment(
            status=PricingStatus.PAID,
            reason="commercial pricing language present",
            matched=paid,
        )

    return PricingAssessment(
        status=PricingStatus.UNVERIFIED,
        reason="no pricing or licence evidence in the collected text",
    )


def describe(assessment: PricingAssessment) -> str:
    """Human-readable one-liner for notifications and the digest."""
    label = {
        PricingStatus.FREE: "Free",
        PricingStatus.FREE_TIER: "Free tier",
        # Only assigned when a licence or explicit open-source claim exists; open
        # weights alone stay UNVERIFIED (see classify_pricing step 3).
        PricingStatus.OPEN_SOURCE: "Open source",
        PricingStatus.PAID: "Paid",
        PricingStatus.UNVERIFIED: "Pricing unverified",
    }[assessment.status]
    suffix = " (restricted licence)" if assessment.restricted else ""
    return f"{label}{suffix}"
