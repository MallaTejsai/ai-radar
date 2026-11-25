"""Collector parsing for RSS, GitHub releases and the Hugging Face Hub."""

from __future__ import annotations

import httpx
import pytest

from ai_radar.collectors.base import collect_source
from ai_radar.collectors.github import collect_github_releases, parse_releases, repo_identity
from ai_radar.collectors.huggingface import collect_huggingface_models, parse_models
from ai_radar.collectors.rss import collect_rss, parse_feed
from ai_radar.models.core import CollectionMethod
from ai_radar.models.source import SourceSpec, SourceState
from support.helpers import mock_client

RSS_SAMPLE = """<?xml version="1.0"?>
<rss version="2.0"><channel><title>Test</title>
<item>
  <title>Introducing Model X</title>
  <link>https://example.com/model-x?utm_source=rss</link>
  <description><![CDATA[<p>A new model.</p>]]></description>
  <pubDate>Mon, 23 Nov 2026 06:00:00 GMT</pubDate>
  <author>team@example.com</author>
  <guid>https://example.com/model-x</guid>
</item>
<item>
  <title>Second post</title>
  <link>https://example.com/second</link>
  <description>Body</description>
  <pubDate>Tue, 24 Nov 2026 06:00:00 GMT</pubDate>
</item>
</channel></rss>"""


def rss_spec(**overrides) -> SourceSpec:
    payload = {
        "id": "test_rss",
        "name": "Test RSS",
        "category": "ai_models",
        "tier": "A",
        "method": CollectionMethod.RSS,
        "url": "https://example.com/feed.xml",
        "max_items": 2,
    }
    payload.update(overrides)
    return SourceSpec(**payload)


def github_spec(**overrides) -> SourceSpec:
    payload = {
        "id": "test_github",
        "name": "Test GitHub",
        "category": "ai_agents",
        "tier": "A",
        "method": CollectionMethod.GITHUB_RELEASES,
        "url": "https://api.github.com/repos/acme/tool/releases?per_page=20",
        "max_items": 5,
    }
    payload.update(overrides)
    return SourceSpec(**payload)


def hf_spec(**overrides) -> SourceSpec:
    payload = {
        "id": "test_hf",
        "name": "Test HF",
        "category": "image_video_ai",
        "tier": "A",
        "method": CollectionMethod.HUGGINGFACE_MODELS,
        "url": "https://huggingface.co/api/models?limit=5",
        "max_items": 5,
    }
    payload.update(overrides)
    return SourceSpec(**payload)


def test_rss_parse_extracts_fields():
    entries = parse_feed(RSS_SAMPLE, rss_spec())
    assert len(entries) == 2
    first = entries[0]
    assert first.title == "Introducing Model X"
    assert first.url.endswith("model-x?utm_source=rss")
    assert "new model" in first.summary
    assert first.published_raw.startswith("Mon, 23 Nov 2026")
    assert first.kind_hint == "rss"


def test_rss_parse_respects_max_items():
    entries = parse_feed(RSS_SAMPLE, rss_spec(max_items=1))
    assert len(entries) == 1


def test_rss_parse_of_garbage_returns_nothing():
    assert parse_feed("<html><body>not a feed</body></html>", rss_spec()) == []


async def test_collect_rss_returns_not_modified_on_304():
    client = mock_client(lambda request: httpx.Response(304))
    try:
        result = await collect_rss(rss_spec(), SourceState(source_id="test_rss", etag='"e"'), client)
    finally:
        await client.aclose()
    assert result.not_modified is True
    assert result.ok is True


async def test_collect_rss_reports_empty_feed_as_an_error():
    client = mock_client(lambda request: httpx.Response(200, text="<rss><channel></channel></rss>"))
    try:
        result = await collect_rss(rss_spec(), SourceState(source_id="test_rss"), client)
    finally:
        await client.aclose()
    assert not result.ok
    assert "no entries" in (result.error or "")


async def test_collect_rss_isolates_http_failures():
    client = mock_client(lambda request: httpx.Response(404))
    try:
        result = await collect_rss(rss_spec(), SourceState(source_id="test_rss"), client)
    finally:
        await client.aclose()
    assert not result.ok
    assert result.http_status == 404


def test_repo_identity_parses_owner_and_name():
    assert repo_identity("https://api.github.com/repos/acme/tool/releases?per_page=20") == "acme/tool"


def test_github_parse_skips_drafts_and_builds_identity():
    payload = [
        {
            "tag_name": "v1.2.3",
            "name": "Version 1.2.3",
            "html_url": "https://github.com/acme/tool/releases/tag/v1.2.3",
            "body": "What's Changed: features",
            "published_at": "2026-11-20T10:00:00Z",
            "author": {"login": "maintainer"},
        },
        {"tag_name": "v1.2.4", "draft": True, "html_url": "https://github.com/acme/tool/releases/tag/v1.2.4"},
    ]
    entries = parse_releases(payload, github_spec())
    assert len(entries) == 1
    assert entries[0].external_id == "acme/tool@v1.2.3"
    assert entries[0].kind_hint == "github_releases"
    assert "Version 1.2.3" in entries[0].title


def test_github_parse_raises_on_api_error_envelope():
    with pytest.raises(ValueError):
        parse_releases({"message": "API rate limit exceeded"}, github_spec())


async def test_github_collector_falls_back_when_the_token_is_rejected():
    # A present-but-invalid token previously disabled all five release sources
    # with HTTP 401, even though public releases are readable anonymously.
    def handler(request: httpx.Request):
        if request.headers.get("authorization"):
            return httpx.Response(401, json={"message": "Bad credentials"})
        return httpx.Response(
            200,
            json=[
                {
                    "tag_name": "v3.0.0",
                    "html_url": "https://github.com/acme/tool/releases/tag/v3.0.0",
                    "published_at": "2026-11-21T10:00:00Z",
                }
            ],
        )

    client = mock_client(handler)
    try:
        result = await collect_github_releases(
            github_spec(), SourceState(source_id="test_github"), client, token="ghp_bogus"
        )
    finally:
        await client.aclose()
    assert result.ok
    assert [entry.external_id for entry in result.entries] == ["acme/tool@v3.0.0"]
    assert "rejected" in result.note


async def test_github_collector_hints_at_rate_limits():
    client = mock_client(lambda request: httpx.Response(403, json={"message": "rate limited"}))
    try:
        result = await collect_github_releases(
            github_spec(), SourceState(source_id="test_github"), client
        )
    finally:
        await client.aclose()
    assert not result.ok
    assert "GITHUB_TOKEN" in (result.error or "")


def test_huggingface_requires_an_owner_prefix():
    payload = [{"id": "no-owner-repo", "likes": 50, "downloads": 10000}]
    entries, filtered = parse_models(payload, hf_spec())
    assert entries == []
    assert filtered == 1


def test_huggingface_substance_gate_filters_low_usage():
    payload = [
        {"id": "acme/good", "likes": 10, "downloads": 5000, "pipeline_tag": "text-to-image"},
        {"id": "acme/throwaway", "likes": 0, "downloads": 3, "pipeline_tag": "text-to-image"},
    ]
    entries, filtered = parse_models(payload, hf_spec(min_likes=3, min_downloads=200))
    assert [entry.external_id for entry in entries] == ["acme/good"]
    assert filtered == 1


def test_huggingface_entry_shape():
    payload = [{"id": "acme/model", "likes": 9, "downloads": 900, "pipeline_tag": "text-to-image",
                "createdAt": "2026-11-23T17:47:27.000Z", "tags": ["diffusers", "safetensors"]}]
    entries, _ = parse_models(payload, hf_spec())
    entry = entries[0]
    assert entry.url == "https://huggingface.co/acme/model"
    assert "downloads" in entry.summary
    assert entry.author == "acme"
    assert entry.external_id == "acme/model"


async def test_huggingface_collector_reports_filtered_count():
    client = mock_client(
        lambda request: httpx.Response(200, json=[{"id": "acme/x", "likes": 0, "downloads": 0}])
    )
    try:
        result = await collect_huggingface_models(
            hf_spec(min_likes=3), SourceState(source_id="test_hf"), client
        )
    finally:
        await client.aclose()
    assert result.ok
    assert result.entries == []
    assert "substance gate" in result.note


async def test_github_collector_preserves_validators_and_reports_304():
    # First response carries an ETag which must be persisted, otherwise the next
    # run cannot make a conditional request.
    client = mock_client(
        lambda request: httpx.Response(
            200,
            json=[
                {
                    "tag_name": "v2.0.0",
                    "html_url": "https://github.com/acme/tool/releases/tag/v2.0.0",
                    "published_at": "2026-11-20T10:00:00Z",
                }
            ],
            headers={"ETag": '"gh-etag"', "Last-Modified": "Wed, 19 Nov 2026 10:00:00 GMT"},
        )
    )
    try:
        result = await collect_github_releases(
            github_spec(), SourceState(source_id="test_github"), client
        )
    finally:
        await client.aclose()
    assert result.ok
    assert result.etag == '"gh-etag"'
    assert result.last_modified == "Wed, 19 Nov 2026 10:00:00 GMT"

    conditional = mock_client(lambda request: httpx.Response(304))
    try:
        cached = await collect_github_releases(
            github_spec(), SourceState(source_id="test_github", etag='"gh-etag"'), conditional
        )
    finally:
        await conditional.aclose()
    # A 304 must be "unchanged", never an invalid-JSON source failure.
    assert cached.ok
    assert cached.not_modified is True
    assert cached.http_status == 304
    assert cached.etag == '"gh-etag"'


async def test_huggingface_collector_treats_304_as_unchanged():
    client = mock_client(lambda request: httpx.Response(304))
    try:
        result = await collect_huggingface_models(
            hf_spec(), SourceState(source_id="test_hf", etag='"hf"'), client
        )
    finally:
        await client.aclose()
    assert result.ok
    assert result.not_modified is True
    assert result.etag == '"hf"'


async def test_collect_source_isolates_unexpected_exceptions():
    spec = rss_spec()

    def boom(request):
        raise RuntimeError("transport exploded")

    client = mock_client(boom)
    try:
        result = await collect_source(spec, None, client)
    finally:
        await client.aclose()
    assert not result.ok
    assert "RuntimeError" in (result.error or "")
