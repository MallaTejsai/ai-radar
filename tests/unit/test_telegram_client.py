"""Telegram client error classification.

`doctor` must print a readable FAIL for a bad token, not a traceback, and the
token must never survive in the error text.
"""

from __future__ import annotations

import httpx
import pytest

from ai_radar.notifications import TelegramClient, TelegramError
from support.helpers import mock_client

TOKEN = "123456789:AAFakeTokenForRedactionTestOnly_1234567890"


def _client(handler) -> TelegramClient:
    return TelegramClient(mock_client(handler), TOKEN, "424242")


async def test_get_me_returns_the_bot_identity():
    telegram = _client(
        lambda request: httpx.Response(
            200, json={"ok": True, "result": {"username": "ai_radar_bot"}}
        )
    )
    assert (await telegram.get_me())["username"] == "ai_radar_bot"


@pytest.mark.parametrize("status", [401, 404])
async def test_bad_credentials_are_unauthorized_not_a_traceback(status):
    telegram = _client(lambda request: httpx.Response(status, json={"ok": False}))

    with pytest.raises(TelegramError) as excinfo:
        await telegram.get_me()

    assert excinfo.value.kind == "unauthorized"
    assert "AAFakeToken" not in str(excinfo.value)


async def test_upstream_failure_is_unavailable():
    telegram = _client(lambda request: httpx.Response(503, text="unavailable"))

    with pytest.raises(TelegramError) as excinfo:
        await telegram.get_me()

    assert excinfo.value.kind == "unavailable"


async def test_get_chat_is_read_only_and_targets_getchat():
    # `notify --check` must be able to prove a chat id works without delivering a
    # message, because the notification gate may still be closed.
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {"id": 424242, "type": "private", "first_name": "Owner"},
            },
        )

    telegram = _client(handler)
    chat = await telegram.get_chat()

    assert chat["id"] == 424242
    assert len(requests) == 1
    assert requests[0].method == "GET"
    assert requests[0].url.path.endswith("/getChat")
    assert requests[0].url.params["chat_id"] == "424242"
    assert "sendMessage" not in str(requests[0].url)


async def test_get_chat_without_a_chat_id_is_not_configured():
    telegram = TelegramClient(
        mock_client(lambda request: httpx.Response(200, json={"ok": True})), TOKEN, ""
    )

    with pytest.raises(TelegramError) as excinfo:
        await telegram.get_chat()

    assert excinfo.value.kind == "not_configured"


async def test_unknown_chat_is_a_permanent_configuration_error():
    telegram = _client(
        lambda request: httpx.Response(
            400, json={"ok": False, "description": "Bad Request: chat not found"}
        )
    )

    with pytest.raises(TelegramError) as excinfo:
        await telegram.get_chat()

    assert excinfo.value.kind == "bad_request"
    # SafeHttpClient raises on >=400, so the status is what survives.
    assert "400" in str(excinfo.value)
    # The request URL embeds the bot token; it must never reach the message.
    assert "AAFakeToken" not in str(excinfo.value)


async def test_http_200_with_ok_false_is_reported():
    telegram = _client(
        lambda request: httpx.Response(
            200, json={"ok": False, "description": "Unauthorized"}
        )
    )

    with pytest.raises(TelegramError) as excinfo:
        await telegram.get_me()

    assert "Unauthorized" in str(excinfo.value)
