"""Evidence verification: verdicts, confidence coupling and probe behaviour.

The verifier is where "evidence-confirmed" is decided, so these tests pin the
rules the specification is explicit about rather than only the happy path:

* Tier C alone can never confirm a claim.
* A 404 disproves; a 403 is gated-but-present (PARTIAL).
* Availability is checked for the artefact URL itself, not its domain.
* Missing pricing stays UNVERIFIED without blocking an otherwise confirmed claim.
"""

from __future__ import annotations

from datetime import timedelta

import httpx
import pytest

from ai_radar.models.core import PricingStatus, SourceTier, Verdict
from ai_radar.settings import load_config
from ai_radar.utils.timeutil import utcnow
from ai_radar.verification.verifier import EvidenceVerifier
from support.helpers import make_entry, mock_client


def _verifier(statuses: dict[str, int], *, default: int = 200, **kwargs):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(statuses.get(str(request.url), default))

    verifier = EvidenceVerifier(mock_client(handler, **kwargs), load_config().scoring.signals)
    return verifier, calls


async def test_tier_a_reachable_and_dated_is_confirmed():
    entry = make_entry(url="https://example.com/blog/model-x")
    verifier, _ = _verifier({entry.canonical_url: 200})

    outcome = await verifier.verify(entry)

    assert outcome.verdict is Verdict.CONFIRMED
    assert outcome.confidence.total >= load_config().confidence_threshold
    assert outcome.detail.date_check == "CONFIRMED"
    assert outcome.detail.availability_check == "CONFIRMED"


async def test_tier_c_alone_cannot_confirm():
    entry = make_entry(url="https://reddit.example/r/ai/1", tier=SourceTier.C)
    verifier, _ = _verifier({entry.canonical_url: 200})

    outcome = await verifier.verify(entry)

    assert outcome.verdict is Verdict.INSUFFICIENT
    assert outcome.confidence.total <= 0.49
    # The verdict alone already keeps it low; the cap is the backstop if the
    # formula ever changes to award more credit.
    assert outcome.confidence.cap_applied in (None, "tier_c_only")


async def test_missing_page_disproves_the_finding():
    entry = make_entry(url="https://example.com/gone")
    verifier, _ = _verifier({entry.canonical_url: 404})

    outcome = await verifier.verify(entry)

    assert outcome.verdict is Verdict.DISPROVED
    assert outcome.detail.availability_check == "DISPROVED"
    assert outcome.confidence.total == 0.0


async def test_future_publication_date_is_disproved():
    entry = make_entry(url="https://example.com/tomorrow", published_at=utcnow() + timedelta(days=5))
    verifier, _ = _verifier({entry.canonical_url: 200})

    outcome = await verifier.verify(entry)

    assert outcome.verdict is Verdict.DISPROVED
    assert outcome.detail.date_check == "DISPROVED"


async def test_gated_page_is_partial_not_confirmed():
    entry = make_entry(url="https://example.com/private")
    verifier, _ = _verifier({entry.canonical_url: 403})

    outcome = await verifier.verify(entry)

    assert outcome.verdict is Verdict.PARTIAL
    assert outcome.detail.availability_check == "PARTIAL"
    assert outcome.evidence[0].reachable is True


async def test_availability_is_checked_per_url_not_per_domain():
    live = make_entry(url="https://example.com/blog/new-model", key="k-live")
    dead = make_entry(url="https://example.com/blog/removed-model", key="k-dead")
    verifier, calls = _verifier({dead.canonical_url: 410})

    first = await verifier.verify(live)
    second = await verifier.verify(dead)

    assert first.verdict is Verdict.CONFIRMED
    assert second.verdict is Verdict.DISPROVED
    assert len(calls) == 2, "a domain-level cache would have reused the live page for the dead one"


async def test_repeated_url_is_probed_once():
    entry = make_entry(url="https://example.com/blog/repeat")
    verifier, calls = _verifier({entry.canonical_url: 200})

    await verifier.verify(entry)
    await verifier.verify(entry)

    assert len(calls) == 1


async def test_probe_budget_degrades_to_unprobed_not_confirmed():
    first = make_entry(url="https://a.example/blog/one", key="k1")
    second = make_entry(url="https://b.example/blog/two", key="k2")
    verifier, calls = _verifier({}, default=200)
    verifier.MAX_PROBES_PER_RUN = 1

    await verifier.verify(first)
    outcome = await verifier.verify(second)

    assert len(calls) == 1
    assert outcome.detail.availability_check == "INSUFFICIENT"
    assert outcome.verdict is not Verdict.CONFIRMED


async def test_pricing_stays_unverified_when_the_text_is_silent():
    entry = make_entry(
        url="https://example.com/blog/silent",
        title="We shipped a faster endpoint",
        summary="Latency is down 40% for everyone.",
    )
    verifier, _ = _verifier({entry.canonical_url: 200})

    outcome = await verifier.verify(entry)

    # Silence about money is not evidence of "free" - but it does not block the
    # factual claim either.
    assert outcome.pricing.status is PricingStatus.UNVERIFIED
    assert outcome.verdict is Verdict.CONFIRMED
    assert outcome.detail.pricing_check == "INSUFFICIENT"


async def test_free_tier_text_is_recorded_as_free_tier():
    entry = make_entry(
        url="https://example.com/blog/tier",
        title="New plan",
        summary="Available on the free tier for everyone.",
    )
    verifier, _ = _verifier({entry.canonical_url: 200})

    outcome = await verifier.verify(entry)

    assert outcome.pricing.status is PricingStatus.FREE_TIER
    assert outcome.detail.pricing_check == "CONFIRMED"


async def test_corroboration_across_domains_raises_confidence():
    primary = make_entry(url="https://openai.example/blog/model", key="k-primary")
    secondary = make_entry(
        url="https://news.example/story/model", source_id="techcrunch", key="k-secondary"
    )
    verifier, _ = _verifier({primary.canonical_url: 200, secondary.canonical_url: 200})

    alone = await verifier.verify(primary)
    together = await verifier.verify(primary, [secondary])

    assert together.confidence.total >= alone.confidence.total
    assert together.distinct_domains == 2
    assert any("corroborated across 2 domains" in note for note in together.detail.notes)
