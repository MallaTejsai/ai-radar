"""DSN preconditions.

Supabase offers several endpoints for the same database and they are not
interchangeable. These tests pin the one property the pipeline depends on: the
run lock is session-scoped and psycopg prepares statements, so a transaction
pooler would break both.
"""

from __future__ import annotations

import pytest

from ai_radar.storage.db import (
    Database,
    DatabaseError,
    assert_staging_target,
    is_disposable_database,
    validate_dsn,
)

SESSION_POOLER = (
    "postgresql://postgres.abcdefghijklm:secret@"
    "aws-0-ap-south-1.pooler.supabase.com:5432/postgres?sslmode=require"
)
TRANSACTION_POOLER = SESSION_POOLER.replace(":5432/", ":6543/")
DIRECT = "postgresql://postgres:secret@db.abcdefghijklm.supabase.co:5432/postgres"


def test_session_pooler_is_accepted():
    database = Database(SESSION_POOLER)
    assert database is not None


def test_direct_connection_is_accepted():
    assert Database(DIRECT) is not None


def test_transaction_pooler_is_refused_with_an_explanation():
    with pytest.raises(DatabaseError) as excinfo:
        Database(TRANSACTION_POOLER)

    message = str(excinfo.value)
    assert "6543" in message
    assert "session" in message.lower()
    assert "advisory lock" in message.lower()


def test_operator_can_override_with_an_explicit_flag(monkeypatch):
    monkeypatch.setenv("AI_RADAR_ALLOW_TRANSACTION_POOLER", "1")
    assert Database(TRANSACTION_POOLER) is not None


def test_empty_dsn_is_still_a_configuration_error():
    with pytest.raises(DatabaseError, match="DATABASE_URL is not configured"):
        Database("")


@pytest.mark.parametrize(
    "dsn,expected",
    [
        ("postgresql://u:p@h:5432/ai_radar_test", True),
        ("postgresql://u:p@h:5432/ai_radar_ci", True),
        ("postgresql://u:p@h:5432/ai-radar-staging", True),
        ("postgresql://u:p@h:5432/scratch_db", True),
        ("postgresql://u:p@h:5432/postgres", False),
        ("postgresql://u:p@h:5432/ai_radar", False),
        ("postgresql://u:p@h:5432/production", False),
        ("not-a-dsn", False),
    ],
)
def test_only_disposable_databases_are_flagged_for_destructive_tests(dsn, expected):
    assert is_disposable_database(dsn) is expected


#: A second Supabase project. Supabase names every database ``postgres``, so the
#: only thing that distinguishes it from production is host + user (project ref).
STAGING_POOLER = (
    "postgresql://postgres.stagingref:stagingpw@"
    "aws-0-ap-south-1.pooler.supabase.com:5432/postgres"
)


def test_staging_mode_accepts_a_separate_supabase_project():
    assert (
        assert_staging_target(
            STAGING_POOLER, staging=True, production_dsn=SESSION_POOLER
        )
        is None
    )


def test_staging_mode_refuses_the_production_endpoint():
    # Same host/port/user as production. The database name is "postgres" in both,
    # so a name check cannot tell them apart; the endpoint check must.
    with pytest.raises(DatabaseError, match="same server"):
        assert_staging_target(SESSION_POOLER, staging=True, production_dsn=SESSION_POOLER)


def test_staging_mode_requires_a_dedicated_staging_url():
    # Staging never falls back to DATABASE_URL, so an empty staging URL is fatal
    # even when production is configured.
    with pytest.raises(DatabaseError, match="not set"):
        assert_staging_target("", staging=True, production_dsn=SESSION_POOLER)


def test_staging_without_production_requires_a_disposable_name():
    with pytest.raises(DatabaseError, match="cannot verify"):
        assert_staging_target(STAGING_POOLER, staging=True)
    assert (
        assert_staging_target(
            "postgresql://u:p@h:5432/ai_radar_test", staging=True
        )
        is None
    )


def test_staging_guard_is_inert_when_staging_is_off():
    assert assert_staging_target(SESSION_POOLER, staging=False) is None
    assert assert_staging_target("", staging=False) is None


def test_staging_refusal_never_echoes_the_dsn():
    with pytest.raises(DatabaseError) as excinfo:
        assert_staging_target(SESSION_POOLER, staging=True, production_dsn=SESSION_POOLER)
    message = str(excinfo.value)
    assert "secret" not in message
    assert "pooler.supabase.com" not in message
    assert "abcdefghijklm" not in message


def test_validate_dsn_returns_the_dsn_and_refuses_transaction_pooler():
    assert validate_dsn(SESSION_POOLER) == SESSION_POOLER
    with pytest.raises(DatabaseError):
        validate_dsn(TRANSACTION_POOLER)
    with pytest.raises(DatabaseError, match="not configured"):
        validate_dsn("")


@pytest.mark.parametrize(
    "dsn",
    [
        # unencoded brackets make urlsplit treat the host as an IPv6 literal
        "postgresql://postgres.ref:pa[ss]@aws-0-x.pooler.supabase.com:5432/postgres",
        "postgresql://postgres.ref:pa]ss@aws-0-x.pooler.supabase.com:5432/postgres",
        "postgresql://postgres.ref:pa%5Bss%5D@host:not-a-port/postgres",
    ],
)
def test_malformed_passwords_are_rejected_without_echoing_them(dsn):
    with pytest.raises(DatabaseError) as excinfo:
        validate_dsn(dsn)
    message = str(excinfo.value)
    assert "malformed" in message.lower()
    assert "pa[ss]" not in message
    assert "pa]ss" not in message


def test_a_dsn_without_a_password_is_rejected():
    with pytest.raises(DatabaseError, match="no password"):
        validate_dsn("postgresql://postgres.ref@aws-0-x.pooler.supabase.com:5432/postgres")
