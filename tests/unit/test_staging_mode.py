"""Staging safety mode (``AI_RADAR_STAGING=1``).

Two independent guarantees:

1. **Production is never read.** Staging mode uses
   ``AI_RADAR_STAGING_DATABASE_URL`` and ignores ``DATABASE_URL`` entirely, so a
   forgotten override cannot fall back to production. The endpoint comparison in
   ``ai_radar.storage.db`` (covered in ``test_db_dsn.py``) closes the copy/paste
   gap.
2. **No side effects outside staging.** Even when real credentials are present in
   ``.env``, staging mode blanks them, so the Telegram dispatcher, the Gemini and
   Groq providers, the heartbeat ping and the Drive client can never be built.

A Supabase project names its database ``postgres`` regardless of the project, so
none of this relies on the database name.
"""

from __future__ import annotations

import pytest

from ai_radar.cli import _staging_settings
from ai_radar.settings import Settings, load_config, reset_cache
from ai_radar.storage.db import (
    DatabaseError,
    dsn_fingerprint,
    endpoint_signature,
    masked_endpoint,
    validate_dsn,
)

PROD_DSN = (
    "postgresql://postgres.prodref:s3cret@aws-0-ap-south-1.pooler.supabase.com:5432/postgres"
)
STAGING_DSN = (
    "postgresql://postgres.stagingref:stagingpw@aws-0-ap-south-1.pooler.supabase.com:5432/postgres"
)


def _staging(**overrides) -> Settings:
    payload = {
        "_env_file": None,
        "ai_radar_staging": True,
        "database_url": PROD_DSN,
        "ai_radar_staging_database_url": STAGING_DSN,
        "freeai_api_key": "sk-free-abc123def456",
        "telegram_bot_token": "7781234567:AAH-abcdefghijklmnopqrstuvwxyz012345",
        "telegram_chat_id": "7781234567",
        "gemini_api_key": "AIzaSyAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "groq_api_key": "gsk_abcdefghijklmnopqrstuvwxyz0123456789ABCDEFGH",
        "healthchecks_ping_url": "https://hc-ping.com/3f2b9c1e-7d4a-4c2e-9b31-1234567890ab",
        "google_oauth_client_id": "client-id.apps.googleusercontent.com",
        "google_oauth_client_secret": "client-secret-value",
        "google_oauth_refresh_token": "refresh-token-value",
        "google_drive_folder_id": "folder-id-value",
    }
    payload.update(overrides)
    return Settings(**payload)


@pytest.fixture
def clean_config(monkeypatch):
    monkeypatch.setenv("AI_RADAR_IGNORE_DOTENV", "1")
    reset_cache()
    yield load_config()
    reset_cache()


def test_staging_is_off_by_default_and_keeps_credentials():
    settings = Settings(_env_file=None, telegram_chat_id="7781234567")
    assert settings.ai_radar_staging is False
    assert settings.telegram_chat_id == "7781234567"


def test_staging_disables_every_notification_and_backup_integration():
    settings = _staging()
    assert settings.telegram_bot_token == ""
    assert settings.telegram_chat_id == ""
    assert settings.healthchecks_ping_url == ""
    assert settings.google_oauth_client_id == ""
    assert settings.google_oauth_client_secret == ""
    assert settings.google_oauth_refresh_token == ""
    assert settings.google_drive_folder_id == ""


def test_staging_disables_the_other_ai_providers():
    # Requirement: a staging run must not contact Gemini or Groq.
    settings = _staging()
    assert settings.gemini_api_key == ""
    assert settings.groq_api_key == ""


def test_staging_keeps_freeai_and_the_call_cap():
    settings = _staging()
    assert settings.freeai_api_key == "sk-free-abc123def456"
    assert settings.freeai_max_calls_per_run == 2
    assert settings.freeai_daily_token_budget == 25_000


def test_masked_endpoint_identifies_the_project_without_the_password():
    masked = masked_endpoint(STAGING_DSN)
    assert masked == (
        "postgres.stagingref@aws-0-ap-south-1.pooler.supabase.com:5432/postgres"
    )
    assert "stagingpw" not in masked
    assert "s3cret" not in masked_endpoint(PROD_DSN)
    assert masked_endpoint("") == "(not set)"


def test_fingerprint_is_stable_distinct_and_short():
    assert dsn_fingerprint(STAGING_DSN) == dsn_fingerprint(STAGING_DSN)
    assert dsn_fingerprint(STAGING_DSN) != dsn_fingerprint(PROD_DSN)
    assert len(dsn_fingerprint(STAGING_DSN)) == 16
    assert dsn_fingerprint("") == "(none)"
    # Non-reversible: no fragment of the DSN survives in the fingerprint.
    assert "stagingref" not in dsn_fingerprint(STAGING_DSN)
    assert "stagingpw" not in dsn_fingerprint(STAGING_DSN)


#: Exactly what Supabase's Connect panel puts on the clipboard: the password is
#: still the literal placeholder, and its brackets make the URI unparseable.
PLACEHOLDER_DSN = (
    "postgresql://postgres.stagingref:[YOUR-PASSWORD]@"
    "aws-1-ap-southeast-2.pooler.supabase.com:5432/postgres"
)


def test_unreplaced_password_placeholder_is_reported_not_crashed(clean_config):
    settings = _staging(ai_radar_staging_database_url=PLACEHOLDER_DSN)
    config = clean_config.model_copy(update={"settings": settings})

    # Must be a DatabaseError (exit 2 with a readable line), never a ValueError
    # traceback from urlsplit.
    with pytest.raises(DatabaseError) as excinfo:
        _staging_settings(config)

    message = str(excinfo.value)
    assert "AI_RADAR_STAGING_DATABASE_URL" in message
    assert "YOUR-PASSWORD" in message


def test_validate_dsn_names_the_variable_the_operator_set():
    with pytest.raises(DatabaseError, match="AI_RADAR_STAGING_DATABASE_URL is malformed"):
        validate_dsn(PLACEHOLDER_DSN, label="AI_RADAR_STAGING_DATABASE_URL")


def test_endpoint_signature_raises_database_error_not_value_error():
    with pytest.raises(DatabaseError):
        endpoint_signature(PLACEHOLDER_DSN)


def test_reporting_survives_an_unparseable_dsn():
    assert "unparseable" in masked_endpoint(PLACEHOLDER_DSN)
    assert dsn_fingerprint(PLACEHOLDER_DSN) != "(none)"


def test_staging_env_variable_names_match_the_documentation(monkeypatch):
    # Pins the operator-facing names. A field named `staging_database_url` would
    # silently read STAGING_DATABASE_URL instead of the documented variable, and
    # staging mode would refuse to run with a confusing "not set" message.
    monkeypatch.setenv("AI_RADAR_IGNORE_DOTENV", "1")
    monkeypatch.setenv("AI_RADAR_STAGING", "1")
    monkeypatch.setenv("AI_RADAR_STAGING_DATABASE_URL", STAGING_DSN)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    settings = Settings(_env_file=None)

    assert settings.ai_radar_staging is True
    assert settings.ai_radar_staging_database_url == STAGING_DSN
    # Staging did not invent a production URL from the environment.
    assert settings.database_url == ""


def test_staging_settings_use_the_dedicated_url_not_production(clean_config):
    config = clean_config.model_copy(update={"settings": _staging()})
    effective = _staging_settings(config)
    assert effective.ai_radar_staging is True
    assert effective.database_url == STAGING_DSN
    # The production URL is untouched on the original object; it is simply unused.
    assert config.settings.database_url == PROD_DSN


def test_staging_settings_refuse_the_production_endpoint(clean_config):
    settings = _staging(ai_radar_staging_database_url=PROD_DSN)
    config = clean_config.model_copy(update={"settings": settings})
    with pytest.raises(DatabaseError, match="same server"):
        _staging_settings(config)


def test_non_staging_settings_are_returned_unchanged(clean_config):
    config = clean_config.model_copy(
        update={"settings": Settings(_env_file=None, database_url=PROD_DSN)}
    )
    effective = _staging_settings(config)
    assert effective.ai_radar_staging is False
    assert effective.database_url == PROD_DSN
