"""free.ai adapter: request shape, error classes and budget enforcement.

The adapter is verified against the documented API (OpenAI-compatible
``POST /v1/chat/``, Bearer auth, 402 when the daily pool is exhausted). No real
key is used here; the transport is mocked.
"""

from __future__ import annotations

import json

import httpx
import pytest

from ai_radar.intelligence import FreeAIProvider, IntelligenceEngine, ProviderCall, ProviderError
from ai_radar.models.analysis import AnalysisPayload
from ai_radar.models.core import Provider
from ai_radar.settings import SignalsConfig, Settings
from support.helpers import make_entry, mock_client

KEY = "sk-free-unit-test-only"
MODEL_JSON = {
    "summary": "A new model with a free tier.",
    "what_is_new": "Longer context and lower latency.",
    "why_it_matters": "Cheaper for coding assistants.",
    "practical_use_case": "Summarise a repository.",
    "capabilities": ["code generation"],
    "suggested_priority": 72,
    "pricing_status_suggested": "FREE_TIER",
    "pricing_uncertain": False,
    "caveats": "",
}


def chat_body(**overrides):
    payload = {
        "model": "qwen7b",
        "choices": [{"message": {"content": json.dumps(MODEL_JSON)}}],
        "free_ai_usage": {"tokens_charged": 123, "tokens_used": 123, "source": "self_hosted"},
    }
    payload.update(overrides)
    return payload


def provider(handler, **kwargs) -> FreeAIProvider:
    return FreeAIProvider(mock_client(handler), KEY, **kwargs)


async def test_success_parses_openai_shape_and_records_billed_tokens():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/v1/models"):
            return httpx.Response(200, json={"data": [{"id": "qwen7b"}]})
        seen["path"] = request.url.path
        seen["auth"] = request.headers.get("authorization", "")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=chat_body())

    call = await provider(handler).analyze("Summarise this announcement.")

    assert seen["path"] == "/v1/chat/"
    assert seen["auth"].startswith("Bearer ")
    assert seen["body"]["model"] == "qwen7b"
    assert seen["body"]["messages"][0]["role"] == "system"
    assert call.provider is Provider.FREEAI
    assert call.model_id == "qwen7b"
    assert call.payload.suggested_priority == 72
    assert call.prompt_tokens == 123  # billed total, see module docstring
    assert call.http_status == 200
    assert call.completion_tokens == 0


async def test_402_is_a_quota_failure_not_a_generic_error():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/v1/models"):
            return httpx.Response(200, json={"data": []})
        return httpx.Response(402, json={"error": "out of tokens"})

    with pytest.raises(ProviderError) as excinfo:
        await provider(handler).analyze("prompt")

    assert excinfo.value.kind == "quota"


async def test_429_carries_retry_after():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/v1/models"):
            return httpx.Response(200, json={"data": []})
        return httpx.Response(429, headers={"retry-after": "7"})

    with pytest.raises(ProviderError) as excinfo:
        await provider(handler).analyze("prompt")

    assert excinfo.value.kind == "rate_limit"
    assert excinfo.value.retry_after == 7.0


async def test_a_bad_key_is_classified_as_auth():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "invalid key"})

    with pytest.raises(ProviderError) as excinfo:
        await provider(handler).analyze("prompt")

    assert excinfo.value.kind == "auth"


async def test_invalid_json_content_is_invalid_response():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/v1/models"):
            return httpx.Response(200, json={"data": []})
        return httpx.Response(200, json={"choices": [{"message": {"content": "not json"}}]})

    with pytest.raises(ProviderError) as excinfo:
        await provider(handler).analyze("prompt")

    assert excinfo.value.kind == "invalid_response"


async def test_an_exhausted_budget_prevents_the_request():
    calls = {"chat": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/v1/models"):
            return httpx.Response(200, json={"data": []})
        calls["chat"] += 1
        return httpx.Response(200, json=chat_body())

    async def used() -> int:
        return 25_000

    with pytest.raises(ProviderError) as excinfo:
        await provider(handler, daily_token_budget=25_000, usage_lookup=used).analyze("prompt")

    assert excinfo.value.kind == "quota"
    assert calls["chat"] == 0


async def test_the_per_run_cap_stops_after_the_configured_calls():
    calls = {"chat": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/v1/models"):
            return httpx.Response(200, json={"data": []})
        calls["chat"] += 1
        return httpx.Response(200, json=chat_body())

    instance = provider(handler, max_calls_per_run=1)
    await instance.analyze("first")

    with pytest.raises(ProviderError) as excinfo:
        await instance.analyze("second")

    assert excinfo.value.kind == "quota"
    assert calls["chat"] == 1


async def test_a_failing_budget_lookup_fails_closed():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/v1/models"):
            return httpx.Response(200, json={"data": []})
        return httpx.Response(200, json=chat_body())

    async def broken() -> int:
        raise RuntimeError("database down")

    with pytest.raises(ProviderError) as excinfo:
        await provider(handler, usage_lookup=broken).analyze("prompt")

    assert excinfo.value.kind == "unavailable"


async def test_the_cli_binds_the_provider_into_the_budget_lookup():
    """Regression: the CLI used to pass `repo.provider_tokens_last_24h` unbound.

    That method takes a provider name, so the router's zero-argument call raised
    TypeError, the provider was disabled as `unavailable`, and free.ai was skipped
    for every discovery in the run while the report still said "completed".
    """
    from ai_radar.cli import _freeai_usage_lookup

    class Repo:
        def __init__(self) -> None:
            self.seen: list[str] = []

        async def provider_tokens_last_24h(self, provider: str) -> int:
            self.seen.append(provider)
            return 1_234

    repo = Repo()
    lookup = _freeai_usage_lookup(repo)

    assert lookup is not None
    assert await lookup() == 1_234
    assert repo.seen == ["freeai"]
    assert _freeai_usage_lookup(None) is None


def test_freeai_is_first_in_the_router_chain():
    from ai_radar.intelligence import IntelligenceEngine

    settings = Settings(
        _env_file=None,
        freeai_api_key="k",
        gemini_api_key="g",
        groq_api_key="q",
    )
    engine = IntelligenceEngine(
        settings=settings,
        signals=SignalsConfig(),
        client=mock_client(lambda request: httpx.Response(200, json={})),
    )
    assert [item.name for item in engine._providers] == [
        Provider.FREEAI,
        Provider.GEMINI,
        Provider.GROQ,
    ]  # noqa: SLF001 - order is the contract



class _StubProvider:
    """Minimal second provider used to prove the fallback order."""

    name = Provider.GEMINI

    def __init__(self) -> None:
        self.calls = 0

    async def resolve_model(self) -> str:
        return "stub-model"

    async def analyze(self, prompt: str) -> ProviderCall:
        self.calls += 1
        return ProviderCall(
            payload=AnalysisPayload(
                summary="stub", what_is_new="stub", why_it_matters="stub", practical_use_case="stub"
            ),
            provider=Provider.GEMINI,
            model_id="stub-model",
            latency_ms=0,
        )


async def test_quota_falls_through_to_the_next_provider():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/v1/models"):
            return httpx.Response(200, json={"data": []})
        return httpx.Response(402, json={"error": "out of tokens"})

    stub = _StubProvider()
    engine = IntelligenceEngine(
        settings=Settings(_env_file=None),
        signals=SignalsConfig(),
        client=mock_client(handler),
        providers=[provider(handler), stub],
    )

    result = await engine.analyze(make_entry(), "unit")

    assert result.provider is Provider.GEMINI
    assert stub.calls == 1
    assert engine.stats.disabled.get("freeai") == "quota"


async def test_verify_models_false_makes_exactly_one_chat_request():
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(200, json=chat_body())

    instance = FreeAIProvider(mock_client(handler), KEY, verify_models=False)
    await instance.analyze("prompt")

    assert paths == ["/v1/chat/"]
