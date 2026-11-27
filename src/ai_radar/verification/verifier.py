"""Evidence verification (the VERIFY stage).

What this module can and cannot do is stated explicitly in the design:

* It **can** confirm that an artefact exists, that its page is reachable, that the
  publication date is plausible and parseable, that the claim is corroborated by
  independent domains, and that the collected text contains an explicit pricing
  or licence statement.
* It **cannot** read a pricing table behind JavaScript, negotiate a login wall or
  judge marketing claims. Anything it cannot check is recorded as UNVERIFIED, and
  an UNVERIFIED field is never turned into a confident sentence.

Verdicts:
  CONFIRMED    date verified + artefact reachable + at least one A/B tier source
  PARTIAL      date verified but reachability unknown, or only Tier B evidence
  INSUFFICIENT no usable date, no reachable evidence, or Tier C only
  DISPROVED    gone (404/410) or a publication date in the future
"""

from __future__ import annotations

from dataclasses import dataclass

from ..models.core import SourceTier, Verdict
from ..models.discovery import Evidence, NormalizedEntry, VerificationDetail
from ..utils.logging import get_logger
from ..utils.net import SafeHttpClient
from ..utils.text import domain_of
from ..utils.timeutil import age_days, utcnow
from ..processing.confidence import ConfidenceBreakdown, explain_confidence
from ..processing.pricing import LICENSE_TERMS, RESTRICTED_LICENSE_TERMS, PricingAssessment, classify_pricing
from ..settings import SignalsConfig

logger = get_logger(__name__)

GONE_STATUSES = frozenset({404, 410})


@dataclass(slots=True)
class VerificationOutcome:
    verdict: Verdict
    detail: VerificationDetail
    evidence: list[Evidence]
    pricing: PricingAssessment
    confidence: ConfidenceBreakdown

    @property
    def distinct_domains(self) -> int:
        return len({item.domain for item in self.evidence if item.domain})

    @property
    def tiers(self) -> set[SourceTier]:
        return {item.tier for item in self.evidence}


class EvidenceVerifier:
    """Verifies discoveries, caching availability probes per exact URL.

    The cache is keyed by the canonical URL, not the domain: a feed link can be
    gone (404) while the publisher's home page still answers 200, and a domain
    cache would report that dead artefact as reachable. Repeated URLs within one
    run still cost a single request.

    Probes are capped per run so a feed flood cannot turn verification into a
    crawler. Past the cap a URL is recorded as unprobed (availability
    ``INSUFFICIENT``), which lowers confidence instead of inventing evidence.
    """

    #: Hard ceiling on availability probes per run (~4 concurrent, HEAD first).
    MAX_PROBES_PER_RUN = 120

    def __init__(self, client: SafeHttpClient, signals: SignalsConfig) -> None:
        self._client = client
        self._signals = signals
        self._probe_cache: dict[str, int | None] = {}
        self._probes_used = 0
        self._budget_warned = False

    async def verify(
        self,
        primary: NormalizedEntry,
        corroborating: list[NormalizedEntry] | None = None,
    ) -> VerificationOutcome:
        corroborating = corroborating or []
        now = utcnow()
        notes: list[str] = []

        # --- date -----------------------------------------------------------
        age = age_days(primary.published_at, reference=now)
        if primary.published_at is None:
            date_check = "INSUFFICIENT"
            notes.append("no parseable publication date")
        elif age is not None and age < -2:
            date_check = "DISPROVED"
            notes.append(f"publication date is {abs(age):.0f} days in the future")
        elif age is not None and age > 400:
            date_check = "PARTIAL"
            notes.append(f"publication is {age:.0f} days old")
        else:
            date_check = "CONFIRMED"

        # --- availability ---------------------------------------------------
        status = await self._probe_url(primary.canonical_url)
        if status is None:
            availability_check = "INSUFFICIENT"
            notes.append("primary URL could not be reached during verification")
            reachable: bool | None = None
        elif status in GONE_STATUSES:
            availability_check = "DISPROVED"
            reachable = False
            notes.append(f"primary URL returned {status}")
        elif 200 <= status < 400:
            availability_check = "CONFIRMED"
            reachable = True
        elif status in (401, 403, 429):
            availability_check = "PARTIAL"
            reachable = True
            notes.append(f"primary URL returned {status} (exists but access is gated)")
        else:
            availability_check = "PARTIAL"
            reachable = status is not None
            notes.append(f"primary URL returned {status}")

        # --- pricing / licence (advisory fields, never verdict drivers) -----
        text = f"{primary.title} {primary.summary}"
        pricing = classify_pricing(text, self._signals)
        pricing_check = "CONFIRMED" if pricing.status.value != "UNVERIFIED" else "INSUFFICIENT"
        license_hits = [term for term in LICENSE_TERMS if term in text.lower()]
        restricted_hits = [term for term in RESTRICTED_LICENSE_TERMS if term in text.lower()]
        license_check = "CONFIRMED" if license_hits else "INSUFFICIENT"
        if not license_hits and restricted_hits:
            license_check = "PARTIAL"

        # --- build evidence rows -------------------------------------------
        evidence: list[Evidence] = []
        seen_domains: set[str] = set()
        for entry in [primary, *corroborating]:
            domain = domain_of(entry.canonical_url)
            evidence.append(
                Evidence(
                    url=entry.canonical_url,
                    source_id=entry.source_id,
                    tier=entry.tier,
                    domain=domain,
                    http_status=status if entry is primary else None,
                    checked_at=now,
                    reachable=reachable if entry is primary else None,
                )
            )
            if domain:
                seen_domains.add(domain)

        tiers = {item.tier for item in evidence}
        tier_a_present = SourceTier.A in tiers

        # --- verdict --------------------------------------------------------
        if date_check == "DISPROVED" or availability_check == "DISPROVED":
            verdict = Verdict.DISPROVED
        elif not tier_a_present and SourceTier.B not in tiers:
            verdict = Verdict.INSUFFICIENT
            notes.append("Tier C evidence alone cannot establish a factual claim")
        elif date_check == "INSUFFICIENT":
            verdict = Verdict.INSUFFICIENT
        elif availability_check == "CONFIRMED" and date_check == "CONFIRMED":
            verdict = Verdict.CONFIRMED
        elif availability_check == "PARTIAL":
            verdict = Verdict.PARTIAL
        else:
            verdict = Verdict.PARTIAL

        if len(seen_domains) > 1:
            notes.append(f"corroborated across {len(seen_domains)} domains")

        detail = VerificationDetail(
            date_check=date_check,
            availability_check=availability_check,
            pricing_check=pricing_check,
            license_check=license_check,
            corroboration=max(0, len(seen_domains)),
            tier_a_present=tier_a_present,
            notes=notes,
        )
        confidence = explain_confidence(
            verdict=verdict,
            tiers=tiers,
            distinct_domains=len(seen_domains),
            reachable=reachable,
            date_verified=date_check == "CONFIRMED",
        )
        return VerificationOutcome(
            verdict=verdict,
            detail=detail,
            evidence=evidence,
            pricing=pricing,
            confidence=confidence,
        )

    async def _probe_url(self, url: str) -> int | None:
        if url in self._probe_cache:
            return self._probe_cache[url]
        if self._probes_used >= self.MAX_PROBES_PER_RUN:
            if not self._budget_warned:
                logger.warning(
                    "verification_probe_budget_exhausted",
                    limit=self.MAX_PROBES_PER_RUN,
                    note="remaining URLs are verified as unreachable and cannot be confirmed",
                )
                self._budget_warned = True
            return None
        self._probes_used += 1
        result = await self._client.probe(url)
        self._probe_cache[url] = result.status
        if not result.ok and result.note:
            logger.debug("probe_not_ok", url=url, note=result.note)
        return result.status
