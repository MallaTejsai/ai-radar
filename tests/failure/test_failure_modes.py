"""Failure-mode tests: every item on the spec's list that can be simulated offline."""

from __future__ import annotations

import json

import httpx
import pytest

from ai_radar.intelligence.base import ProviderError
from ai_radar.intelligence.gemini import GeminiProvider
from ai_radar.intelligence.groq import GroqProvider
from ai_radar.models.core import Provider, RunStatus
from ai_radar.models.source import SourceRegistry, SourceSpec
from ai_radar.notifications import DeliveryDispatcher, TelegramError
from ai_radar.operations import BackupError, BackupService
from ai_radar.operations import pipeline as pipeline_module
from support.fakes import FakeDrive, FakeRepository, FakeTelegram
from support.helpers import mock_client
from support.pipeline import AlwaysLocked, build_pipeline, feed, make_handler, one_source_config


def gemini_response(text: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={"candidates": [{"content": {"parts": [{"text": text}]}}], "usageMetadata": {}},
    )


# ------------------------------------------------------------------ upstream
async def test_network_timeout_is_isolated(config, fake_repo, monkeypatch):
    monkeypatch.setattr(pipeline_module, "RunLock", AlwaysLocked)
    cfg = one_source_config(config)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("simulated timeout", request=request)

    http, pipeline = build_pipeline(cfg, fake_repo, handler)
    try:
        report = await pipeline.run(trigger="test")
    finally:
        await http.aclose()

    assert report.sources_failed == 1
    assert report.status is RunStatus.DEGRADED
    assert fake_repo.discoveries == {}


async def test_http_429_is_isolated_and_reported(config, fake_repo, monkeypatch):
    monkeypatch.setattr(pipeline_module, "RunLock", AlwaysLocked)
    cfg = one_source_config(config)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"retry-after": "60"})

    http, pipeline = build_pipeline(cfg, fake_repo, handler)
    try:
        report = await pipeline.run(trigger="test")
    finally:
        await http.aclose()

    assert report.sources_failed == 1
    assert any("429" in error or "rate_limit" in error for error in report.errors)


async def test_malformed_feed_is_isolated(config, fake_repo, monkeypatch):
    monkeypatch.setattr(pipeline_module, "RunLock", AlwaysLocked)
    cfg = one_source_config(config)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>definitely not a feed</html>")

    http, pipeline = build_pipeline(cfg, fake_repo, handler)
    try:
        report = await pipeline.run(trigger="test")
    finally:
        await http.aclose()

    assert report.sources_failed == 1
    assert fake_repo.discoveries == {}


# -------------------------------------------------------------- intelligence
async def test_invalid_json_from_gemini_is_classified():
    client = mock_client(lambda request: gemini_response("this is not json"))
    provider = GeminiProvider(client, "key", pinned_model="gemini-test")
    try:
        with pytest.raises(ProviderError) as excinfo:
            await provider.analyze("prompt")
    finally:
        await client.aclose()
    assert excinfo.value.kind == "invalid_response"


async def test_gemini_quota_error_carries_retry_after():
    client = mock_client(
        lambda request: httpx.Response(429, headers={"retry-after": "30"}, json={})
    )
    provider = GeminiProvider(client, "key", pinned_model="gemini-test")
    try:
        with pytest.raises(ProviderError) as excinfo:
            await provider.analyze("prompt")
    finally:
        await client.aclose()
    assert excinfo.value.kind == "rate_limit"
    assert excinfo.value.retry_after == 30.0


async def test_gemini_outage_is_classified():
    client = mock_client(lambda request: httpx.Response(503))
    provider = GeminiProvider(client, "key", pinned_model="gemini-test")
    try:
        with pytest.raises(ProviderError) as excinfo:
            await provider.analyze("prompt")
    finally:
        await client.aclose()
    assert excinfo.value.kind == "unavailable"


async def test_groq_rejection_is_classified_as_auth():
    client = mock_client(lambda request: httpx.Response(401, json={"error": "bad key"}))
    provider = GroqProvider(client, "key", pinned_model="model-test")
    try:
        with pytest.raises(ProviderError) as excinfo:
            await provider.analyze("prompt")
    finally:
        await client.aclose()
    assert excinfo.value.kind == "auth"


async def test_empty_completion_is_rejected():
    client = mock_client(
        lambda request: httpx.Response(200, json={"candidates": [{"content": {"parts": []}}]})
    )
    provider = GeminiProvider(client, "key", pinned_model="gemini-test")
    try:
        with pytest.raises(ProviderError) as excinfo:
            await provider.analyze("prompt")
    finally:
        await client.aclose()
    assert excinfo.value.kind == "invalid_response"


async def test_valid_provider_payload_is_parsed_end_to_end():
    payload = {
        "summary": "A model that supports long context for repository-scale work.",
        "what_is_new": "Long context plus agentic tool use.",
        "why_it_matters": "Large codebases can be handled in one pass.",
        "practical_use_case": "Summarise a repository before a review.",
        "category": "ai_coding_tools",
        "target_users": ["developers"],
        "capabilities": ["long context"],
        "pricing_status_suggested": "FREE_TIER",
        "license_note": "",
        "pricing_uncertain": False,
        "suggested_priority": 70,
        "caveats": "",
    }
    client = mock_client(lambda request: gemini_response(json.dumps(payload)))
    provider = GeminiProvider(client, "key", pinned_model="gemini-test")
    try:
        call = await provider.analyze("prompt")
    finally:
        await client.aclose()
    assert call.provider is Provider.GEMINI
    assert call.payload.suggested_priority == 70
    assert call.model_id == "gemini-test"


# ------------------------------------------------------------------ database
async def test_database_outage_mid_run_marks_the_run_failed(config, monkeypatch):
    monkeypatch.setattr(pipeline_module, "RunLock", AlwaysLocked)
    repo = FakeRepository()
    repo.fail_on_existing = True
    cfg = one_source_config(config)
    http, pipeline = build_pipeline(cfg, repo, make_handler(feed()))
    try:
        report = await pipeline.run(trigger="test")
    finally:
        await http.aclose()

    assert report.status is RunStatus.FAILED
    assert any("database outage" in error for error in report.errors)
    assert repo.checkpoint_advances == []
    assert repo.runs[report.run_id].status is RunStatus.FAILED


# ---------------------------------------------------------------- deliveries
class _SentThenBrokenRepo(FakeRepository):
    async def mark_delivery_sent(self, delivery_id, telegram_message_id):
        raise RuntimeError("simulated crash after Telegram accepted the message")


async def test_crash_after_telegram_accepts_leaves_delivery_unconfirmed(config, monkeypatch):
    monkeypatch.setattr(pipeline_module, "RunLock", AlwaysLocked)
    repo = _SentThenBrokenRepo()
    cfg = one_source_config(config)
    http, pipeline = build_pipeline(cfg, repo, make_handler(feed()), telegram=FakeTelegram())
    try:
        report = await pipeline.run(trigger="test")
    finally:
        await http.aclose()

    assert report.status is RunStatus.FAILED
    delivery = next(iter(repo.deliveries.values()))
    assert delivery["status"] == "SENDING"
    assert delivery["telegram_message_id"] is None


async def test_dispatcher_stops_after_an_ambiguous_send():
    repo = FakeRepository()
    dispatcher = DeliveryDispatcher(repo, FakeTelegram(), worker_id="w", max_per_run=5)
    for index in range(3):
        await repo.queue_delivery(f"discovery-{index}", f"message {index}", "hash" * 4)
    dispatcher._telegram = FakeTelegram(error=TelegramError("timeout", kind="unknown"))  # noqa: SLF001

    outcome = await dispatcher.dispatch()
    assert outcome.unknown == 1
    assert outcome.sent == 0
    assert outcome.deferred == 2


async def test_permanent_telegram_rejection_is_not_retried():
    repo = FakeRepository()
    dispatcher = DeliveryDispatcher(repo, FakeTelegram(), worker_id="w", max_per_run=5)
    await repo.queue_delivery("discovery-1", "message", "hash" * 4)
    dispatcher._telegram = FakeTelegram(  # noqa: SLF001
        error=TelegramError("bad markup", kind="bad_request")
    )
    outcome = await dispatcher.dispatch()
    assert outcome.unknown == 1
    assert next(iter(repo.deliveries.values()))["status"] == "UNKNOWN"


@pytest.mark.parametrize(
    ("kind", "expected_status"),
    [("unknown", "UNKNOWN"), ("bad_request", "UNKNOWN"), ("unavailable", "RETRY_WAIT")],
)
async def test_telegram_token_is_scrubbed_before_a_delivery_error_is_persisted(
    kind, expected_status
):
    # TelegramError text embeds the request URL, and that URL carries the bot
    # token. The log processor redacts it, but this string is also written to
    # `deliveries.last_error` and then exported in backups.
    repo = FakeRepository()
    dispatcher = DeliveryDispatcher(repo, FakeTelegram(), worker_id="w", max_per_run=5)
    await repo.queue_delivery("discovery-1", "message", "hash" * 4)
    token = "123456789:AAFakeTokenForRedactionTestOnly_1234567890"
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    dispatcher._telegram = FakeTelegram(  # noqa: SLF001
        error=TelegramError(f"send failed: HTTP 400 from {url}", kind=kind)
    )

    await dispatcher.dispatch()

    delivery = next(iter(repo.deliveries.values()))
    assert delivery["status"] == expected_status
    stored = json.dumps(delivery, default=str)
    assert token not in stored
    assert "AAFakeToken" not in stored
    assert "***REDACTED***" in stored


# ------------------------------------------------------------------- backups
async def test_upload_failure_is_raised_and_not_marked_validated():
    repo = FakeRepository()
    service = BackupService(repo, FakeDrive(fail_upload=True))
    with pytest.raises(BackupError, match="simulated upload failure"):
        await service.run()
    assert repo.saved, "the failed attempt must still be recorded"
    assert all(item.status != "VALIDATED" for item in repo.saved)
    assert repo.saved[-1].status == "FAILED"


async def test_corrupted_download_is_detected():
    repo = FakeRepository()
    service = BackupService(repo, FakeDrive(corrupt_download=True))
    with pytest.raises(BackupError):
        await service.run()
    assert all(item.status != "VALIDATED" for item in repo.saved)


async def test_remote_size_mismatch_is_detected():
    repo = FakeRepository()
    service = BackupService(repo, FakeDrive(size_mismatch=True))
    with pytest.raises(BackupError, match="remote size"):
        await service.run()


async def test_successful_backup_is_uploaded_and_verified():
    repo = FakeRepository()
    drive = FakeDrive()
    service = BackupService(repo, drive)
    manifest = await service.run()
    assert manifest.status == "VALIDATED"
    assert manifest.google_drive_file_id
    assert drive.uploaded
    assert any(item.status == "VALIDATED" for item in repo.saved)


async def test_backup_without_drive_credentials_fails_before_exporting():
    repo = FakeRepository()
    service = BackupService(repo, None)
    with pytest.raises(BackupError, match="not configured"):
        await service.run()
    assert repo.saved == []
