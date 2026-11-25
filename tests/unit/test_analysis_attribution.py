"""F2 regression tests: paid analysis attribution and persistence.

Two failures are pinned here:

* A paid analysis was spent on a candidate the confidence gate later discarded,
  and the paid result existed only in the cache. The gate now runs first, so a
  low-confidence candidate costs nothing.
* A successful paid analysis must be persisted with its provider attribution,
  not silently replaced by the deterministic fallback.
"""

from __future__ import annotations

import pytest

from ai_radar.intelligence.base import ProviderCall, ProviderError
from ai_radar.models.analysis import AnalysisPayload
from ai_radar.models.core import Provider
from ai_radar.operations import pipeline as pipeline_module
from support.fakes import FakeRepository
from support.pipeline import AlwaysLocked, build_pipeline, feed, make_handler, one_source_config


class _PaidStub:
    """A provider that behaves like a successful Sarvam call, without a network."""

    name = Provider.SARVAM

    def __init__(self) -> None:
        self.calls = 0

    async def resolve_model(self) -> str:
        return "sarvam-105b"

    async def analyze(self, prompt: str) -> ProviderCall:
        self.calls += 1
        return ProviderCall(
            payload=AnalysisPayload(
                summary="A paid-provider summary that is long enough to be displayed.",
                what_is_new="It introduces a cheaper inference tier for small teams.",
                suggested_priority=55,
            ),
            provider=Provider.SARVAM,
            model_id="sarvam-105b",
            latency_ms=4702,
            prompt_tokens=422,
            completion_tokens=800,
            http_status=200,
        )


class _FailingPaidStub(_PaidStub):
    async def analyze(self, prompt: str) -> ProviderCall:
        self.calls += 1
        raise ProviderError("Sarvam per-run cap reached (1 call(s))", kind="quota")


@pytest.fixture
def base_config(config):
    return one_source_config(config)


async def test_successful_paid_analysis_is_persisted_with_its_provider(
    base_config, fake_repo, monkeypatch
):
    monkeypatch.setattr(pipeline_module, "RunLock", AlwaysLocked)
    provider = _PaidStub()
    http, pipeline = build_pipeline(
        base_config, fake_repo, make_handler(feed()), providers=[provider]
    )
    try:
        report = await pipeline.run(trigger="test")
    finally:
        await http.aclose()

    assert provider.calls == 1
    assert report.persisted_new == 1
    stored = next(iter(fake_repo.analyses.values()))
    assert stored.provider is Provider.SARVAM
    assert stored.model_id == "sarvam-105b"

    # The cache and the persisted analysis must agree about who produced it.
    cached = next(iter(fake_repo.cache.values()))
    assert cached.provider is Provider.SARVAM

    attribution = [
        call for call in fake_repo.provider_calls if call["provider"] == "sarvam" and call["ok"]
    ]
    assert len(attribution) == 1


async def test_low_confidence_candidate_is_rejected_before_any_paid_call(
    base_config, fake_repo, monkeypatch
):
    monkeypatch.setattr(pipeline_module, "RunLock", AlwaysLocked)
    monkeypatch.setattr(
        base_config,
        "calibration",
        base_config.calibration.__class__(calibrated=False, threshold=0.95),
    )
    monkeypatch.setattr(base_config.scoring.confidence, "discard_below", 0.95)

    provider = _PaidStub()
    http, pipeline = build_pipeline(
        base_config, fake_repo, make_handler(feed()), providers=[provider]
    )
    try:
        report = await pipeline.run(trigger="test")
    finally:
        await http.aclose()

    assert provider.calls == 0, "a candidate that cannot be stored must not cost money"
    assert report.discarded_low_confidence == 1
    assert report.discard_reasons == {"low_confidence": 1}
    assert fake_repo.cache == {}
    assert fake_repo.analyses == {}
    assert fake_repo.discoveries == {}


async def test_paid_failure_falls_back_but_keeps_the_reason(base_config, fake_repo, monkeypatch):
    monkeypatch.setattr(pipeline_module, "RunLock", AlwaysLocked)
    provider = _FailingPaidStub()
    http, pipeline = build_pipeline(
        base_config, fake_repo, make_handler(feed()), providers=[provider]
    )
    try:
        report = await pipeline.run(trigger="test")
    finally:
        await http.aclose()

    assert provider.calls == 1
    assert report.persisted_new == 1
    stored = next(iter(fake_repo.analyses.values()))
    assert stored.provider is Provider.DETERMINISTIC
    assert stored.fallback_reason and "quota" in stored.fallback_reason
    assert report.discard_reasons == {}


async def test_cached_paid_analysis_is_persisted_once_its_candidate_survives(
    base_config, fake_repo, monkeypatch
):
    """The staging symptom: a paid analysis lived only in the cache."""
    monkeypatch.setattr(pipeline_module, "RunLock", AlwaysLocked)
    provider = _PaidStub()
    http, pipeline = build_pipeline(
        base_config, fake_repo, make_handler(feed()), providers=[provider]
    )
    try:
        report = await pipeline.run(trigger="test")
    finally:
        await http.aclose()

    assert report.persisted_new == 1
    assert any(
        result.provider is Provider.SARVAM for result in fake_repo.analyses.values()
    ), "a paid analysis must never exist only in the cache"
