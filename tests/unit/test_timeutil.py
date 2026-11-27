"""Time handling: IST windows, slot maths and missed-run detection."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ai_radar.utils.timeutil import (
    IST,
    day_bounds_ist,
    missing_slots,
    parse_datetime,
    research_slots_between,
    to_ist,
    to_utc,
)


def test_parse_datetime_accepts_rfc822_and_iso():
    rfc = parse_datetime("Mon, 23 Nov 2026 06:00:00 GMT")
    iso = parse_datetime("2026-11-23T06:00:00Z")
    assert rfc == iso
    assert rfc.tzinfo is not None


def test_parse_datetime_treats_naive_as_utc():
    parsed = parse_datetime("2026-11-23 06:00:00")
    assert parsed == datetime(2026, 11, 23, 6, tzinfo=timezone.utc)


def test_parse_datetime_rejects_nonsense():
    assert parse_datetime("") is None
    assert parse_datetime("not a date") is None
    assert parse_datetime("1899-01-01") is None


def test_ist_offset_is_five_thirty():
    moment = datetime(2026, 11, 23, 6, 0, tzinfo=timezone.utc)
    local = to_ist(moment)
    assert local.hour == 11
    assert local.minute == 30
    assert local.tzinfo is not None


def test_day_bounds_ist_covers_exactly_one_ist_day():
    start, end = day_bounds_ist(datetime(2026, 11, 23).date())
    assert to_ist(start).hour == 0
    assert end - start == timedelta(days=1)
    assert start.hour == 18 and start.minute == 30  # 00:00 IST == 18:30 UTC previous day


def test_research_slots_span_the_ist_window():
    start = datetime(2026, 11, 23, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 11, 23, 23, 59, tzinfo=timezone.utc)
    slots = research_slots_between(start, end)
    # 08:30 .. 22:30 IST inclusive = 15 slots
    assert len(slots) == 15
    assert all(slot.minute == 30 for slot in slots)


def test_missing_slots_detects_a_gap():
    now = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)
    missed = missing_slots(now - timedelta(hours=6), now, window_hours=24)
    assert missed
    assert all(slot <= now for slot in missed)


def test_missing_slots_is_empty_when_every_slot_ran():
    now = datetime(2026, 11, 23, 9, 0, tzinfo=timezone.utc)
    slots = research_slots_between(now - timedelta(hours=4), now)
    missed = missing_slots(
        now - timedelta(hours=4), now, window_hours=24, successful_runs=list(slots)
    )
    assert missed == []


def test_to_utc_normalises_naive_values():
    naive = datetime(2026, 11, 23, 6, 0)
    assert to_utc(naive).tzinfo is timezone.utc
    assert to_utc(naive).hour == 6


def test_ist_constant_matches_india():
    assert IST.utcoffset(None) == timedelta(hours=5, minutes=30)
