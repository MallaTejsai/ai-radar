"""The credential scanner must catch shapes and must not print values."""

from __future__ import annotations

from pathlib import Path

from ai_radar.security.secret_scan import (
    is_allowlisted,
    load_allowlist,
    scan_sensitive_filename,
    scan_text,
)

ROOT = Path(__file__).resolve().parents[2]

# Built by concatenation so this test file does not itself contain a literal
# credential shape, which the scanner would (correctly) flag in CI.
GITHUB_TOKEN = "gh" + "p_" + "A" * 36
AWS_KEY = "AK" + "IA" + "Z" * 16
TELEGRAM_TOKEN = "123456789:" + "A" * 35
DB_URL = "postgresql://postgres.ref:" + "pw" * 6 + "@db.ai-radar.invalid:5432/postgres"


def _rules(text: str) -> list[str]:
    return [finding.rule for finding in scan_text(text, location="sample.py")]


def test_flags_common_credential_shapes():
    assert "github-token" in _rules(f'GITHUB_TOKEN = "{GITHUB_TOKEN}"')
    assert "aws-access-key-id" in _rules(f"key = {AWS_KEY}")
    assert "telegram-bot-token" in _rules(f'bot = "{TELEGRAM_TOKEN}"')
    assert "database-url-with-password" in _rules(f'DATABASE_URL="{DB_URL}"')


def test_reports_location_but_not_the_value():
    finding = scan_text(f'token = "{TELEGRAM_TOKEN}"', location="config.py")[0]
    assert finding.location == "config.py:1"
    assert TELEGRAM_TOKEN not in finding.render()


def test_documented_placeholders_are_not_credentials():
    assert _rules("TELEGRAM_BOT_TOKEN=<bot-token-from-botfather>") == []
    assert _rules("DATABASE_URL=postgresql://postgres.project-ref:YOUR-DB-PASSWORD@host:5432/x") == []
    assert _rules(f'GITHUB_TOKEN="{GITHUB_TOKEN}"  # example only') == []


def test_sensitive_filenames_are_flagged_but_the_example_is_allowed():
    assert scan_sensitive_filename(".env") is not None
    assert scan_sensitive_filename("config/.env.local") is not None
    assert scan_sensitive_filename("secrets/service-account-prod.json") is not None
    assert scan_sensitive_filename("dump.sql.gz") is not None
    assert scan_sensitive_filename(".env.example") is None
    assert scan_sensitive_filename("src/ai_radar/settings.py") is None


def test_allowlist_matches_files_not_prefixes():
    entries = load_allowlist(ROOT / ".github" / "secret-scan-allowlist.txt")
    assert entries, "the allowlist must be readable"
    assert is_allowlisted("tests/unit/test_settings_placeholders.py:71", entries)
    assert is_allowlisted("tests/unit/test_settings_placeholders.py@97ca290", entries)
    assert not is_allowlisted("src/ai_radar/settings.py:10", entries)
    assert not is_allowlisted(
        "tests/unit/test_settings_placeholders.py.extra:1", entries
    )
