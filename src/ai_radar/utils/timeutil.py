"""Time handling. Everything is stored in UTC; IST is only for display and scheduling."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Iterable

from dateutil import parser as date_parser

IST = timezone(timedelta(hours=5, minutes=30), name="IST")
UTC = timezone.utc

#: Research window in IST, matching the GitHub Actions cron ``30 2-16 * * *``.
#: 02:30-16:30 UTC == 08:00-22:00 IST (IST is UTC+05:30).
RESEARCH_START_IST = time(8, 0)
RESEARCH_END_IST = time(22, 0)
SLOT_MINUTE = 30


def utcnow() -> datetime:
    """Timezone-aware UTC now. Never returns a naive datetime."""
    return datetime.now(UTC)


def to_ist(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(IST)


def to_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def iso(moment: datetime | None) -> str | None:
    return None if moment is None else to_utc(moment).isoformat().replace("+00:00", "Z")


def parse_datetime(value: str | None, *, assume_tz: timezone = UTC) -> datetime | None:
    """Parse a feed/API timestamp into an aware UTC datetime.

    Feeds are inconsistent: RFC 822 with a named zone, RFC 3339, ``Z`` suffix,
    or a bare date. Unparseable values return ``None`` rather than a guess.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return to_utc(value)
    text = str(value).strip()
    if not text:
        return None
    # Normalise a trailing Z which dateutil handles but be explicit for clarity.
    if text.endswith("Z") and "+" not in text:
        text = text[:-1] + "+00:00"
    try:
        parsed = date_parser.parse(text, fuzzy=False)
    except (ValueError, OverflowError, TypeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=assume_tz)
    # Guard against nonsense far outside a plausible window.
    if parsed.year < 1990 or parsed.year > utcnow().year + 1:
        return None
    return to_utc(parsed)


def age_days(moment: datetime | None, *, reference: datetime | None = None) -> float | None:
    if moment is None:
        return None
    ref = reference or utcnow()
    return (ref - to_utc(moment)).total_seconds() / 86400.0


def bucket_hour(moment: datetime) -> datetime:
    """Truncate to the hour (UTC), used for run/interval accounting."""
    moment = to_utc(moment)
    return moment.replace(minute=0, second=0, microsecond=0)


def research_slots_between(
    start: datetime, end: datetime, *, step_hours: int = 1
) -> list[datetime]:
    """All scheduled research slot times (UTC) in ``[start, end]``.

    Slots are hourly at minute :data:`SLOT_MINUTE` between 08:00 and 22:00 IST.
    """
    start_utc = to_utc(start)
    end_utc = to_utc(end)
    if end_utc <= start_utc:
        return []

    first_day = to_ist(start_utc).date()
    last_day = to_ist(end_utc).date()
    slots: list[datetime] = []
    day = first_day
    while day <= last_day:
        slot_ist = datetime.combine(day, RESEARCH_START_IST, tzinfo=IST)
        end_ist = datetime.combine(day, RESEARCH_END_IST, tzinfo=IST)
        while slot_ist <= end_ist:
            slot_utc = slot_ist.astimezone(UTC)
            if start_utc <= slot_utc <= end_utc:
                slots.append(slot_utc)
            slot_ist = slot_ist + timedelta(hours=step_hours)
        day = day + timedelta(days=1)
    return slots


def missing_slots(
    last_success: datetime | None,
    now: datetime,
    *,
    window_hours: int = 24,
    successful_runs: Iterable[datetime] = (),
) -> list[datetime]:
    """Scheduled slots inside the recovery window with no successful run.

    ``successful_runs`` are the actual completed run timestamps; each covers the
    slot it belongs to and the slot immediately before it (a run started at
    10:30 may legitimately be the completion of the 09:30 backlog).
    """
    now = to_utc(now)
    window_start = now - timedelta(hours=window_hours)
    lower = max(window_start, to_utc(last_success)) if last_success else window_start
    slots = research_slots_between(lower, now)
    if not slots:
        return []

    covered: set[datetime] = set()
    for run_at in successful_runs:
        run_utc = to_utc(run_at)
        covered.add(bucket_hour(run_utc).replace(minute=SLOT_MINUTE))
        covered.add(bucket_hour(run_utc - timedelta(hours=1)).replace(minute=SLOT_MINUTE))

    return [slot for slot in slots if slot not in covered]


def today_ist() -> date:
    return to_ist(utcnow()).date()


def day_bounds_ist(day: date) -> tuple[datetime, datetime]:
    """UTC bounds of an IST calendar day."""
    start = datetime.combine(day, time(0, 0), tzinfo=IST).astimezone(UTC)
    end = start + timedelta(days=1)
    return start, end
