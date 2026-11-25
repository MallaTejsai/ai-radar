"""Discovery models: raw feed entries through to persisted, scored records."""

from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .analysis import AnalysisPayload
from .core import Category, DiscoveryStatus, PricingStatus, SourceTier, Verdict


class RawEntry(BaseModel):
    """Exactly what a collector extracted, before any interpretation."""

    model_config = ConfigDict(extra="forbid")

    source_id: str
    title: str = ""
    url: str = ""
    summary: str = ""
    published_raw: str | None = None
    author: str | None = None
    external_id: str | None = None
    kind_hint: str | None = None


class NormalizedEntry(BaseModel):
    """Cleaned, canonicalised entry with its deterministic identity keys."""

    model_config = ConfigDict(extra="forbid")

    source_id: str
    title: str
    url: str
    canonical_url: str
    summary: str = ""
    published_at: datetime | None = None
    author: str | None = None
    external_id: str | None = None
    canonical_key: str
    title_fingerprint: str
    category: Category
    tier: SourceTier


class Evidence(BaseModel):
    """One evidence row: where a claim can be checked, and how it scored."""

    model_config = ConfigDict(extra="forbid")

    url: str
    source_id: str
    tier: SourceTier
    domain: str = ""
    http_status: int | None = None
    checked_at: datetime | None = None
    reachable: bool | None = None


class ScoreComponent(BaseModel):
    name: str
    raw: float = Field(ge=0.0, le=1.0)
    weight: float
    points: float
    rationale: str = ""


class ScoreBreakdown(BaseModel):
    """Explainable priority score. Every point is attributable."""

    model_config = ConfigDict(extra="forbid")

    components: list[ScoreComponent]
    total: int = Field(ge=0, le=100)
    label: str

    @property
    def by_name(self) -> dict[str, ScoreComponent]:
        return {component.name: component for component in self.components}


class VerificationDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    date_check: str = "INSUFFICIENT"
    availability_check: str = "INSUFFICIENT"
    pricing_check: str = "INSUFFICIENT"
    license_check: str = "INSUFFICIENT"
    corroboration: int = 0
    tier_a_present: bool = False
    notes: list[str] = Field(default_factory=list)


class DiscoveryRecord(BaseModel):
    """The row shape written to ``discoveries`` plus its joined children."""

    model_config = ConfigDict(extra="ignore")

    canonical_key: str = Field(min_length=8, max_length=80)
    title_fingerprint: str = Field(default="", max_length=64)
    title: str = Field(min_length=1, max_length=500)
    category: Category
    summary: str = Field(default="", max_length=2000)
    url: str = Field(default="", max_length=1000)
    source_id: str
    tier: SourceTier
    published_at: datetime | None = None
    discovered_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    last_verified_at: datetime | None = None
    priority_score: int = Field(default=0, ge=0, le=100)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    status: DiscoveryStatus = DiscoveryStatus.VERIFIED
    pricing_status: PricingStatus = PricingStatus.UNVERIFIED
    pricing_restricted: bool = False
    verdict: Verdict = Verdict.INSUFFICIENT
    evidence: list[Evidence] = Field(default_factory=list)
    verification: VerificationDetail = Field(default_factory=VerificationDetail)
    score_breakdown: ScoreBreakdown | None = None
    analysis: AnalysisPayload | None = None

    @model_validator(mode="after")
    def _fill_fingerprint(self) -> "DiscoveryRecord":
        if not self.title_fingerprint:
            from ..utils.text import title_fingerprint

            self.title_fingerprint = title_fingerprint(self.title)
        return self

    def qualifies_for_alert(self, min_priority: int, min_confidence: float) -> bool:
        """Single source of truth for the alert gate.

        Deliberately stricter than "priority >= 3/5": a discovery must also be
        CONFIRMED by evidence and clear the calibrated confidence threshold, with
        at least one Tier A evidence row. PARTIAL discoveries are stored but are
        never alerted or digested - they are the watchlist.
        """
        return (
            self.verdict is Verdict.CONFIRMED
            and self.priority_score >= min_priority
            and self.confidence >= min_confidence
            and any(item.tier is SourceTier.A for item in self.evidence)
        )
