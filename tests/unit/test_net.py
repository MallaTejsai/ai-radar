"""Hardened HTTP client: SSRF guard, retries, limits and conditional GET."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from ai_radar.utils.net import (
    FetchError,
    SafeHttpClient,
    UrlRejected,
    assert_safe_url,
    parse_retry_after,
)
from support.helpers import mock_client

PUBLIC = "93.184.216.34"


async def _public_resolver(host: str, port: int) -> list[str]:
    return [PUBLIC]


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost/admin",
        "http://127.0.0.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.5/internal",
        "http://192.168.1.1/",
        "file:///etc/passwd",
        "ftp://example.com/feed",
        "https://user:pass@example.com/feed",
        "https://example.com:8080/feed",
        "http://[::1]/",
    ],
)
async def test_dangerous_urls_are_rejected(url):
    with pytest.raises(UrlRejected):
        await assert_safe_url(url, resolver=_public_resolver)


async def test_public_host_with_public_dns_is_allowed():
    assert await assert_safe_url("https://example.com/feed", resolver=_public_resolver)


async def test_host_resolving_to_a_private_address_is_rejected():
    async def private_resolver(host: str, port: int) -> list[str]:
        return ["10.1.2.3"]

    with pytest.raises(UrlRejected):
        await assert_safe_url("https://internal.example.com/", resolver=private_resolver)


async def test_literal_private_ip_is_rejected_even_without_dns_validation():
    # validate_dns=False only skips hostname resolution; IP literals are always checked.
    with pytest.raises(UrlRejected):
        await assert_safe_url("http://127.0.0.1:9/x", validate_dns=False)


def test_retry_after_parses_seconds_and_http_dates():
    assert parse_retry_after("5") == 5.0
    assert parse_retry_after("not-a-number") is None
    assert parse_retry_after(None) is None


async def test_redirect_to_a_private_address_is_blocked():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://127.0.0.1/secret"})

    client = mock_client(handler)
    try:
        with pytest.raises(UrlRejected):
            await client.get("https://example.com/start")
    finally:
        await client.aclose()


async def test_response_size_limit_is_enforced():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 5000)

    client = mock_client(handler, max_bytes=100)
    try:
        with pytest.raises(FetchError) as excinfo:
            await client.get("https://example.com/big")
        assert excinfo.value.kind == "too_large"
    finally:
        await client.aclose()


async def test_429_is_retried_and_honours_retry_after():
    calls = {"n": 0}
    waits: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"retry-after": "2"})
        return httpx.Response(200, text="ok")

    async def fake_sleep(seconds: float) -> None:
        waits.append(seconds)

    def make():
        return SafeHttpClient(
            transport=httpx.MockTransport(handler),
            validate_dns=False,
            retries=3,
            sleep=fake_sleep,
        )

    client = make()
    try:
        result = await client.get("https://example.com/feed")
    finally:
        await client.aclose()
    assert result.text == "ok"
    assert calls["n"] == 2
    assert waits and 1.0 < waits[0] < 3.0


async def test_5xx_is_retried_then_succeeds():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503) if calls["n"] == 1 else httpx.Response(200, text="ok")

    client = SafeHttpClient(
        transport=httpx.MockTransport(handler),
        validate_dns=False,
        retries=2,
        sleep=lambda _: asyncio.sleep(0),
    )
    try:
        assert (await client.get("https://example.com/x")).text == "ok"
    finally:
        await client.aclose()
    assert calls["n"] == 2


async def test_error_messages_never_carry_a_telegram_bot_token():
    token = "123456789:AAFakeTokenForRedactionTestOnly_1234567890"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    client = mock_client(handler)
    try:
        with pytest.raises(FetchError) as excinfo:
            await client.get(f"https://api.telegram.org/bot{token}/sendMessage")
    finally:
        await client.aclose()

    message = str(excinfo.value)
    assert token not in message
    assert "AAFakeToken" not in message
    assert "***REDACTED***" in message


async def test_error_messages_mask_query_credentials():
    key = "AIzaSyAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403)

    client = mock_client(handler)
    try:
        with pytest.raises(FetchError) as excinfo:
            await client.get(f"https://example.com/feed?key={key}")
    finally:
        await client.aclose()

    message = str(excinfo.value)
    assert key not in message
    assert "***REDACTED***" in message


async def test_404_is_not_retried():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(404)

    client = SafeHttpClient(
        transport=httpx.MockTransport(handler),
        validate_dns=False,
        retries=3,
        sleep=lambda _: asyncio.sleep(0),
    )
    try:
        with pytest.raises(FetchError) as excinfo:
            await client.get("https://example.com/missing")
    finally:
        await client.aclose()
    assert excinfo.value.status == 404
    assert calls["n"] == 1


async def test_304_is_reported_as_not_modified():
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update({k.lower(): v for k, v in request.headers.items()})
        return httpx.Response(304)

    client = mock_client(handler)
    try:
        result = await client.get(
            "https://example.com/feed", etag='W/"abc"', last_modified="Mon, 23 Nov 2026 06:00:00 GMT"
        )
    finally:
        await client.aclose()
    assert result.not_modified is True
    assert seen["if-none-match"] == 'W/"abc"'


async def test_timeout_is_retryable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=request)

    client = SafeHttpClient(
        transport=httpx.MockTransport(handler),
        validate_dns=False,
        retries=2,
        sleep=lambda _: asyncio.sleep(0),
    )
    try:
        with pytest.raises(FetchError) as excinfo:
            await client.get("https://example.com/x")
    finally:
        await client.aclose()
    assert excinfo.value.retryable is True
    assert excinfo.value.kind == "timeout"


async def test_probe_falls_back_to_get_when_head_is_unsupported():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "HEAD":
            return httpx.Response(405)
        return httpx.Response(200, text="<html></html>")

    client = mock_client(handler)
    try:
        result = await client.probe("https://example.com/page")
    finally:
        await client.aclose()
    assert result.ok is True
    assert result.status == 200


async def test_post_json_classifies_status():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": "quota"})

    client = mock_client(handler)
    try:
        response = await client.post_json("https://example.com/api", payload={"a": 1})
    finally:
        await client.aclose()
    assert response.status == 429
    assert response.data["error"] == "quota"
