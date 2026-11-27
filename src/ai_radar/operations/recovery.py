"""Missed-run detection and bounded recovery planning.

GitHub Actions cron is best-effort: runs are delayed, coalesced or skipped. The
pipeline therefore cannot assume "the previous slot ran". On every start it
computes which scheduled slots inside the recovery window have no successful run,
widens collection for this pass, and records the count.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ..utils.timeutil import missing_slots, research_slots_between, to_utc, utcnow

#: Multiplier applied to per-source item caps while recovering a gap.
RECOVERY_WIDEN_FACTOR = 2
MAX_ITEMS_CEILING = 100


@dataclass(slots=True)
class RecoveryPlan:
    now: datetime
    window_start: datetime
    window_end: datetime
    missed_slots: list[datetime] = field(default_factory=list)
    last_successful_run: datetime | None = None
    widen_factor: int = 1

    @property
    def recovering(self) -> bool:
        return bool(self.missed_slots)

    @property
    def missed_count(self) -> int:
        return len(self.missed_slots)

    def effective_max_items(self, configured: int) -> int:
        """Item cap for one source during this pass."""
        return min(MAX_ITEMS_CEILING, configured * self.widen_factor)

    def summary(self) -> str:
        if not self.recovering:
            return "no missed slots"
        stamps = ", ".join(slot.strftime("%H:%M") for slot in self.missed_slots[:6])
        return f"{self.missed_count} missed slot(s): {stamps}"


def plan_recovery(
    *,
    now: datetime | None = None,
    last_successful_run: datetime | None,
    successful_runs: list[datetime] | None = None,
    window_hours: int = 24,
) -> RecoveryPlan:
    """Compute the recovery plan for this run."""
    now = to_utc(now or utcnow())
    window_start = now - timedelta(hours=window_hours)

    missed = missing_slots(
        last_successful_run,
        now,
        window_hours=window_hours,
        successful_runs=successful_runs or [],
    )

    # Bound the work: recovering more than the window is out of scope by design.
    missed = [slot for slot in missed if slot >= window_start]
    widen = RECOVERY_WIDEN_FACTOR if missed else 1

    return RecoveryPlan(
        now=now,
        window_start=window_start,
        window_end=now,
        missed_slots=missed,
        last_successful_run=last_successful_run,
        widen_factor=widen,
    )


def expected_slots(window_hours: int = 24, *, now: datetime | None = None) -> list[datetime]:
    """Scheduled slots in the trailing window (used by the maintenance check)."""
    now = to_utc(now or utcnow())
    return research_slots_between(now - timedelta(hours=window_hours), now)
