"""Live service contract tests - skipped unless real credentials are present.

These are the only tests that touch the internet. Run them once per deployment to
prove the credentials work; CI runs them only when the secrets exist.
"""

from __future__ import annotations

import os

import pytest

from ai_radar.intelligence import ProviderError
from ai_radar.intelligence.gemini import GeminiProvider
from ai_radar.intelligence.groq import GroqProvider
from ai_radar.notifications import TelegramClient, TelegramError
from ai_radar.operations.drive import DriveClient, DriveError
from ai_radar.utils.net import SafeHttpClient

pytestmark = pytest.mark.live

GEMINI = os.environ.get("GEMINI_API_KEY", "")
GROQ = os.environ.get("GROQ_API_KEY", "")


@pytest.fixture
async def client():
    http = SafeHttpClient(timeout=30.0, retries=2)
    try:
        yield http
    finally:
        await http.aclose()


@pytest.mark.skipif(not GEMINI, reason="GEMINI_API_KEY not set")
async def test_gemini_key_resolves_a_current_model(client):
    provider = GeminiProvider(client, GEMINI)
    try:
        model = await provider.resolve_model()
    except ProviderError as exc:
        pytest.fail(f"Gemini key is not usable: {exc.kind}: {exc}")
    assert model
    assert "gemini" in model.lower()


@pytest.mark.skipif(not GROQ, reason="GROQ_API_KEY not set")
async def test_groq_key_resolves_a_current_model(client):
    provider = GroqProvider(client, GROQ)
    try:
        model = await provider.resolve_model()
    except ProviderError as exc:
        pytest.fail(f"Groq key is not usable: {exc.kind}: {exc}")
    assert "/" in model or model


@pytest.mark.skipif(
    not (os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID")),
    reason="Telegram credentials not set",
)
async def test_telegram_credentials_are_valid(client):
    telegram = TelegramClient(
        client,
        os.environ["TELEGRAM_BOT_TOKEN"],
        os.environ["TELEGRAM_CHAT_ID"],
    )
    try:
        me = await telegram.get_me()
    except TelegramError as exc:
        pytest.fail(f"Telegram credentials rejected: {exc.kind}: {exc}")
    assert me.get("is_bot") is True


@pytest.mark.skipif(
    not os.environ.get("GOOGLE_OAUTH_REFRESH_TOKEN"),
    reason="Google OAuth credentials not set",
)
async def test_google_drive_token_and_folder(client):
    drive = DriveClient(
        client,
        client_id=os.environ.get("GOOGLE_OAUTH_CLIENT_ID", ""),
        client_secret=os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", ""),
        refresh_token=os.environ["GOOGLE_OAUTH_REFRESH_TOKEN"],
        folder_id=os.environ.get("GOOGLE_DRIVE_FOLDER_ID", ""),
    )
    try:
        token = await drive.access_token()
        files = await drive.list_backups()
    except DriveError as exc:
        pytest.fail(f"Google Drive is not usable: {exc}")
    assert token
    assert isinstance(files, list)
