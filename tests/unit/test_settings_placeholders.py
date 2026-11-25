"""Placeholders in ``.env.example`` must never be treated as credentials.

``.env.example`` tells the operator that every value in it is a placeholder and
that the integration stays NOT CONFIGURED until a real value is supplied. Copying
the file to ``.env`` previously made the app send
``Authorization: Bearer <github-read-only-pat>`` to GitHub, which answers 401 for
all five release sources - an apparent outage caused by configuration copy/paste.

These tests pin the promise: a documented placeholder is equivalent to an empty
value, and a real value is never blanked.
"""

from __future__ import annotations

from ai_radar.settings import Settings, looks_like_placeholder


def test_documented_placeholders_are_recognised():
    for value in (
        "<github-read-only-pat>",
        "<bot-token-from-botfather>",
        "<your-numeric-chat-id>",
        "<freeai-api-key>",
        "<gemini-api-key>",
        "<groq-api-key>",
        "<oauth-refresh-token>",
        "postgresql://postgres.project-ref:YOUR-DB-PASSWORD@aws-0-region.pooler.supabase.com:5432/postgres?sslmode=require",
        "https://hc-ping.com/00000000-0000-0000-0000-000000000000",
    ):
        assert looks_like_placeholder(value), value


def test_real_looking_values_are_not_placeholders():
    for value in (
        "",
        "sk-free-abc123def456",
        "7781234567:AAH-abcdefghijklmnopqrstuvwxyz012345",
        "postgresql://postgres.abcdefghijklm:s3cret@aws-0-ap-south-1.pooler.supabase.com:5432/postgres",
        "https://hc-ping.com/3f2b9c1e-7d4a-4c2e-9b31-1234567890ab",
    ):
        assert not looks_like_placeholder(value), value


def test_settings_blank_placeholder_credentials():
    settings = Settings(
        _env_file=None,
        github_token="<github-read-only-pat>",
        telegram_bot_token="<bot-token-from-botfather>",
        gemini_api_key="<gemini-api-key>",
        healthchecks_ping_url="https://hc-ping.com/00000000-0000-0000-0000-000000000000",
        database_url=(
            "postgresql://postgres.project-ref:YOUR-DB-PASSWORD@"
            "aws-0-region.pooler.supabase.com:5432/postgres?sslmode=require"
        ),
    )
    assert settings.github_token == ""
    assert settings.telegram_bot_token == ""
    assert settings.gemini_api_key == ""
    assert settings.healthchecks_ping_url == ""
    assert settings.database_url == ""
    # A blanked placeholder is reported as missing, not as an auth failure.
    assert "DATABASE_URL" in settings.missing_for("research")
    assert "GEMINI_API_KEY" in settings.missing_for("ai")


def test_settings_keep_real_credentials():
    # Deliberately shaped like credentials but marked as synthetic: this test must
    # not look like a leaked key to the repository's own secret scanner.
    settings = Settings(
        _env_file=None,
        github_token="ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxx",
        telegram_chat_id="7781234567",
        freeai_api_key="sk-free-xxxxxxxxxxxxxxxxxxxx",
    )
    assert settings.github_token == "ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxx"
    assert settings.telegram_chat_id == "7781234567"
    assert settings.freeai_api_key == "sk-free-xxxxxxxxxxxxxxxxxxxx"
    assert settings.missing_for("ai") == ["GEMINI_API_KEY"]
