"""CLI contract: exit codes and the module entry point.

The workflows branch on exit codes, so `0` success, `1` runtime failure and `2`
configuration problem are part of the interface, not a detail.
"""

from __future__ import annotations

import pytest

from ai_radar import cli
from ai_radar.settings import reset_cache

SECRET_ENV = (
    "DATABASE_URL",
    "FREEAI_API_KEY",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "GEMINI_API_KEY",
    "GROQ_API_KEY",
    "GOOGLE_OAUTH_CLIENT_ID",
    "GOOGLE_OAUTH_CLIENT_SECRET",
    "GOOGLE_OAUTH_REFRESH_TOKEN",
    "GOOGLE_DRIVE_FOLDER_ID",
    "RESTORE_DATABASE_URL",
    "HEALTHCHECKS_PING_URL",
)


@pytest.fixture
def clean_env(monkeypatch):
    for name in SECRET_ENV:
        monkeypatch.delenv(name, raising=False)
    reset_cache()
    yield
    reset_cache()


def test_module_entry_point_exists():
    # `python -m ai_radar` is what every workflow runs.
    import ai_radar.__main__ as entry

    assert callable(entry.main)


def test_help_exits_zero(capsys):
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--help"])
    assert excinfo.value.code == 0
    assert "research" in capsys.readouterr().out


def test_research_without_database_is_a_configuration_error(clean_env, capsys):
    code = cli.main(["research", "--dry-run"])
    assert code == 2
    assert "DATABASE_URL" in capsys.readouterr().err


def test_restore_without_drive_is_a_configuration_error(clean_env, capsys):
    code = cli.main(["restore", "--verify-only"])
    assert code == 2
    assert "Google Drive is not configured" in capsys.readouterr().err


def test_unknown_command_is_rejected():
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["does-not-exist"])
    assert excinfo.value.code != 0


def test_doctor_reports_a_malformed_dsn_without_raising(clean_env, monkeypatch, capsys):
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql://postgres.ref:pa[ss]@aws-0-x.pooler.supabase.com:5432/postgres",
    )
    code = cli.main(["doctor"])
    output = capsys.readouterr().out
    assert code == 2
    assert "malformed" in output.lower()
    # the unparsable password must not be echoed back
    assert "pa[ss]" not in output


def test_doctor_database_only_skips_every_other_integration(clean_env, capsys):
    code = cli.main(["doctor", "--database-only"])
    output = capsys.readouterr().out

    assert code == 2  # no DATABASE_URL in the test environment
    assert "database" in output
    assert "free.ai" not in output
    assert "Gemini" not in output
    assert "telegram" not in output
    assert "google drive" not in output
    assert "heartbeat" not in output
