"""Schedule-aware staleness.

A fixed "now minus N minutes" test would page the owner every morning at 06:45,
because the research window deliberately closes at 22:00 IST. These tests pin the
slot-based rule and the grace period for a late GitHub cron start.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from ai_radar.operations import heartbeat as hb
from ai_radar.operations.heartbeat import HeartbeatReporter

IST = timezone(timedelta(hours=5, minutes=30))


def ist(year: int, month: int, day: int, hour: int, minute: int) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=IST).astimezone(timezone.utc)


LAST_EVENING = ist(2026, 11, 23, 22, 30)


def _reporter() -> HeartbeatReporter:
    # _is_stale is pure; the repository and HTTP client are irrelevant here.
    return HeartbeatReporter(None, None)  # type: ignore[arg-type]


def _set_now(monkeypatch, moment: datetime) -> None:
    monkeypatch.setattr(hb, "utcnow", lambda: moment)


def test_overnight_gap_is_not_stale(monkeypatch):
    _set_now(monkeypatch, ist(2026, 11, 24, 6, 45))  # maintenance job
    assert _reporter()._is_stale(LAST_EVENING) is False


def test_the_newest_slot_gets_a_grace_period(monkeypatch):
    _set_now(monkeypatch, ist(2026, 11, 24, 8, 35))  # 35 minutes after the 08:00 slot
    assert _reporter()._is_stale(LAST_EVENING) is False


def test_a_missed_morning_slot_is_stale_after_the_grace_period(monkeypatch):
    _set_now(monkeypatch, ist(2026, 11, 24, 9, 31))  # the 08:00 slot is 91 minutes overdue
    assert _reporter()._is_stale(LAST_EVENING) is True


def test_a_fresh_morning_run_is_healthy(monkeypatch):
    _set_now(monkeypatch, ist(2026, 11, 24, 9, 0))
    assert _reporter()._is_stale(ist(2026, 11, 24, 8, 32)) is False


def test_a_missed_evening_slot_is_stale_before_the_window_reopens(monkeypatch):
    _set_now(monkeypatch, ist(2026, 11, 24, 6, 45))
    # Last success 18:30 the previous day, so 21:30 and 22:30 are missing.
    assert _reporter()._is_stale(ist(2026, 11, 23, 18, 30)) is True


def test_no_successful_run_is_stale():
    assert _reporter()._is_stale(None) is True


def test_an_iso_string_is_accepted(monkeypatch):
    _set_now(monkeypatch, ist(2026, 11, 24, 7, 0))
    assert _reporter()._is_stale(LAST_EVENING.isoformat()) is False


def test_an_unparseable_string_is_treated_as_missing(monkeypatch):
    _set_now(monkeypatch, ist(2026, 11, 24, 7, 0))
    assert _reporter()._is_stale("not a date") is True


@pytest.mark.parametrize("hour,minute", [(22, 31), (23, 59)])
def test_after_the_last_slot_a_missing_run_is_stale(monkeypatch, hour, minute):
    _set_now(monkeypatch, ist(2026, 11, 24, hour, minute))
    assert _reporter()._is_stale(ist(2026, 11, 24, 20, 30)) is True
