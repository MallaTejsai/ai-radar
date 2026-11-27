"""F1 regression tests: the paid per-run cap must be atomic.

The original bug was a check-then-act race: ``_check_budget()`` awaited a usage
lookup, and only afterwards incremented the counter. Two concurrent analysis
workers could both pass a one-call cap and both open an HTTP request.

These tests count **outbound HTTP requests**, not counter increments, because a
correct counter with two requests is exactly the bug.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from ai_radar.intelligence.base import ProviderError
from ai_radar.intelligence.sarvam import SarvamProvider
from ai_radar.utils.net import SafeHttpClient

API_KEY = "test-subscription-key-not-real"


def _client(handler) -> SafeHttpClient:
    return SafeHttpClient(
        transport=httpx.MockTransport(handler),
        validate_dns=False,
        retries=1,
        timeout=5.0,
    )


def _valid_body() -> dict:
    payload = {
        "summary": "A new open model with a genuinely long summary for validation.",
        "what_is_new": "It adds a longer context window and a cheaper inference tier.",
        "why_it_matters": "It lowers the cost of running a capable local model.",
        "suggested_priority": 60,
    }
    return {
        "model": "sarvam-105b",
        "choices": [
            {"message": {"content": json.dumps(payload)}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 120, "completion_tokens": 80},
    }


class _Counter:
    def __init__(self) -> None:
        self.requests = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests += 1
        return httpx.Response(200, json=_valid_body())


async def test_one_call_cap_allows_exactly_one_http_request_under_concurrency():
    counter = _Counter()
    client = _client(counter.handler)

    async def slow_usage() -> int:
        # Widens the window the old code lost the race in.
        await asyncio.sleep(0.05)
        return 0

    provider = SarvamProvider(
        client,
        API_KEY,
        max_calls_per_run=1,
        daily_token_budget=10_000,
        usage_lookup=slow_usage,
    )
    try:
        results = await asyncio.gather(
            provider.analyze("first prompt"),
            provider.analyze("second prompt"),
            return_exceptions=True,
        )
    finally:
        await client.aclose()

    assert counter.requests == 1, "the cap must be enforced before the socket opens"
    successes = [item for item in results if not isinstance(item, Exception)]
    rejections = [item for item in results if isinstance(item, ProviderError)]
    assert len(successes) == 1
    assert len(rejections) == 1
    assert rejections[0].kind == "quota"


async def test_local_quota_rejection_never_opens_a_socket():
    counter = _Counter()
    client = _client(counter.handler)
    provider = SarvamProvider(
        client,
        API_KEY,
        max_calls_per_run=1,
        daily_token_budget=5_000,
        usage_lookup=lambda: _constant(9_999),
    )
    try:
        with pytest.raises(ProviderError) as excinfo:
            await provider.analyze("prompt")
    finally:
        await client.aclose()

    assert excinfo.value.kind == "quota"
    assert counter.requests == 0


async def test_a_failed_request_still_consumes_the_paid_slot():
    """A 500 may already have been billed, so the cap must stay conservative."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "upstream"})

    client = _client(handler)
    provider = SarvamProvider(client, API_KEY, max_calls_per_run=1)
    try:
        with pytest.raises(ProviderError):
            await provider.analyze("prompt")
        with pytest.raises(ProviderError) as excinfo:
            await provider.analyze("prompt")
    finally:
        await client.aclose()

    assert excinfo.value.kind == "quota"


async def test_invalid_response_is_diagnosable_without_leaking_the_key():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "sarvam-105b",
                "choices": [{"message": {"content": "not json at all"}, "finish_reason": "length"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 800},
            },
        )

    client = _client(handler)
    provider = SarvamProvider(client, API_KEY, max_calls_per_run=1)
    try:
        with pytest.raises(ProviderError) as excinfo:
            await provider.analyze("prompt")
    finally:
        await client.aclose()

    message = str(excinfo.value)
    assert excinfo.value.kind == "invalid_response"
    assert "finish_reason=length" in message
    assert "http_status=200" in message
    assert API_KEY not in message


async def test_the_configured_token_ceiling_is_not_overridden():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, json=_valid_body())

    client = _client(handler)
    provider = SarvamProvider(client, API_KEY, max_output_tokens=900)
    try:
        await provider.analyze("prompt")
    finally:
        await client.aclose()

    assert seen["max_tokens"] == 900


async def _constant(value: int) -> int:
    return value
