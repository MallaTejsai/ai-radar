"""Analysis payload validation and the provider router's fallback behaviour."""

from __future__ import annotations

import pytest

from ai_radar.intelligence import IntelligenceEngine, ProviderError
from ai_radar.intelligence.deterministic import DeterministicAnalyzer
from ai_radar.models.analysis import AnalysisPayload
from ai_radar.models.core import Category, PricingStatus, Provider
from support.fakes import FakeRepository
from support.helpers import make_entry, mock_client


# ------------------------------------------------------------- payload schema
def test_defaults_are_honest():
    payload = AnalysisPayload()
    assert payload.pricing_status_suggested is PricingStatus.UNVERIFIED
    assert payload.pricing_uncertain is True
    assert payload.summary == ""


def test_category_aliases_are_normalised():
    assert AnalysisPayload(category="coding").category is Category.AI_CODING_TOOLS
    assert AnalysisPayload(category="image-video").category is Category.IMAGE_VIDEO_AI
    assert AnalysisPayload(category="nonsense").category is None


def test_priority_is_clamped():
    assert AnalysisPayload(suggested_priority=500).suggested_priority == 100
    assert AnalysisPayload(suggested_priority=-5).suggested_priority == 0
    assert AnalysisPayload(suggested_priority="72").suggested_priority == 72


def test_lists_are_bounded_and_cleaned():
    payload = AnalysisPayload(target_users=[f"user {i}" for i in range(20)])
    assert len(payload.target_users) <= 6
    assert all(user.strip() == user for user in payload.target_users)


def test_control_characters_are_stripped_from_text_fields():
    payload = AnalysisPayload(summary="hello\x00world")
    assert "\x00" not in payload.summary


def test_usable_requires_real_substance():
    assert AnalysisPayload(summary="short").usable() is False
    assert AnalysisPayload(summary="x" * 40).usable() is True


# ------------------------------------------------------- deterministic analyser
async def test_deterministic_analysis_is_structured_and_cautious(config):
    analyser = DeterministicAnalyzer(config.scoring.signals)
    entry = make_entry(summary="A new multimodal model with a free tier for developers.")
    payload = analyser.analyze_entry(entry)
    assert payload.category is Category.AI_MODELS
    assert payload.summary
    assert payload.suggested_priority == 0  # a fallback must not fake confidence
    assert "Deterministic fallback" in payload.caveats
    assert payload.pricing_status_suggested is PricingStatus.FREE_TIER


# ------------------------------------------------------------- router fallback
class _FailingProvider:
    name = Provider.GEMINI

    def __init__(self, kind: str = "unavailable") -> None:
        self.calls = 0
        self._kind = kind

    async def resolve_model(self) -> str:
        return "fake-model"

    async def analyze(self, prompt: str):
        self.calls += 1
        raise ProviderError("boom", kind=self._kind)


class _RecordingProvider:
    name = Provider.GROQ

    def __init__(self, payload: AnalysisPayload | None = None) -> None:
        self.calls = 0
        self._payload = payload or AnalysisPayload(summary="x" * 40, what_is_new="y" * 25)

    async def resolve_model(self) -> str:
        return "fake-model"

    async def analyze(self, prompt: str):
        from ai_radar.intelligence.base import ProviderCall

        self.calls += 1
        return ProviderCall(payload=self._payload, provider=self.name, model_id="fake-model", latency_ms=5)


async def test_router_falls_back_to_deterministic_when_everything_fails(config):
    repo = FakeRepository()
    client = mock_client(lambda request: pytest.fail("no HTTP expected"))
    engine = IntelligenceEngine(
        settings=config.settings,
        signals=config.scoring.signals,
        client=client,
        cache=repo,
        metrics=repo,
        providers=[_FailingProvider(), _FailingProvider("rate_limit")],
    )
    try:
        result = await engine.analyze(make_entry(), "Test")
    finally:
        await client.aclose()
    assert result.provider is Provider.DETERMINISTIC
    assert result.fallback_reason
    assert engine.stats.by_provider[Provider.DETERMINISTIC.value] == 1
    assert engine.stats.errors["unavailable"] == 1
    assert engine.stats.errors["rate_limit"] == 1


async def test_router_uses_the_first_working_provider(config):
    repo = FakeRepository()
    client = mock_client(lambda request: pytest.fail("no HTTP expected"))
    failing = _FailingProvider()
    working = _RecordingProvider()
    engine = IntelligenceEngine(
        settings=config.settings,
        signals=config.scoring.signals,
        client=client,
        cache=repo,
        metrics=repo,
        providers=[failing, working],
    )
    try:
        result = await engine.analyze(make_entry(), "Test")
    finally:
        await client.aclose()
    assert result.provider is Provider.GROQ
    assert failing.calls == 1 and working.calls == 1


async def test_a_rate_limited_provider_is_not_retried_in_the_same_run(config):
    repo = FakeRepository()
    client = mock_client(lambda request: pytest.fail("no HTTP expected"))
    failing = _FailingProvider("rate_limit")
    working = _RecordingProvider()
    engine = IntelligenceEngine(
        settings=config.settings,
        signals=config.scoring.signals,
        client=client,
        cache=repo,
        metrics=repo,
        providers=[failing, working],
    )
    try:
        await engine.analyze(make_entry(url="https://example.com/a"), "Test")
        await engine.analyze(make_entry(url="https://example.com/b"), "Test")
    finally:
        await client.aclose()
    assert failing.calls == 1  # disabled after the first 429
    assert engine.stats.disabled[Provider.GEMINI.value] == "rate_limit"


async def test_second_identical_analysis_is_served_from_cache(config):
    repo = FakeRepository()
    client = mock_client(lambda request: pytest.fail("no HTTP expected"))
    working = _RecordingProvider()
    engine = IntelligenceEngine(
        settings=config.settings,
        signals=config.scoring.signals,
        client=client,
        cache=repo,
        metrics=repo,
        providers=[working],
    )
    entry = make_entry()
    try:
        await engine.analyze(entry, "Test")
        second = await engine.analyze(entry, "Test")
    finally:
        await client.aclose()
    assert working.calls == 1
    assert second.cached is True
    assert engine.stats.cache_hits == 1
