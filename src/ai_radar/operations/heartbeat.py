"""Heartbeat: an independent dead-man's switch, plus an internal staleness check.

Two different guarantees:

1. **External** - after a run finishes its important stages, one GET is sent to a
   free uptime service (healthchecks.io). That service raises the alarm when the
   pings stop, which is the only way to detect "GitHub Actions stopped scheduling
   me" from outside. A workflow *starting* is never reported as success.
2. **Internal** - the snapshot is stored in ``system_state`` so the daily
   maintenance job and any human can see source health, queue depth, provider
   availability and backup age without reading logs.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from ..models.run import HeartbeatSnapshot
from ..models.core import RunStatus
from ..utils.logging import get_logger
from ..utils.net import FetchError, SafeHttpClient
from ..utils.timeutil import parse_datetime, research_slots_between, to_utc, utcnow

logger = get_logger(__name__)

STATE_KEY = "heartbeat"

#: How long the most recent scheduled slot is allowed to take before its absence
#: counts as a failure. GitHub cron can be delayed by tens of minutes.
SLOT_GRACE_MINUTES = 60


class HeartbeatReporter:
    def __init__(
        self,
        repository,
        client: SafeHttpClient,
        *,
        ping_url: str = "",
        stale_after_minutes: int = 150,
    ) -> None:
        self._repo = repository
        self._client = client
        self._ping_url = ping_url.strip()
        self._stale_after = stale_after_minutes

    async def snapshot(self, *, note: str = "") -> HeartbeatSnapshot:
        overview = await self._repo.overview()
        provider_health = await self._repo.provider_health(hours=24)

        providers = {
            name: f"{counts['successes']}ok/{counts['failures']}err"
            for name, counts in provider_health.items()
        }

        last_success: datetime | None = overview.get("last_successful_run_at")
        stale = self._is_stale(last_success)

        snapshot = HeartbeatSnapshot(
            last_successful_run_at=last_success,
            last_run_id=overview.get("last_run_id"),
            last_run_status=RunStatus(overview["last_run_status"])
            if overview.get("last_run_status")
            else None,
            last_heartbeat_at=utcnow(),
            sources_total=overview.get("sources_total", 0),
            sources_unhealthy=overview.get("sources_unhealthy", 0),
            database_ok=True,
            pending_deliveries=overview.get("pending_deliveries", 0),
            unknown_deliveries=overview.get("unknown_deliveries", 0),
            last_backup_at=overview.get("last_backup_at"),
            last_backup_status=overview.get("last_backup_status"),
            providers=providers,
            stale=stale,
            detail=note,
        )
        await self._repo.set_state(STATE_KEY, json.loads(snapshot.model_dump_json()))
        return snapshot

    async def publish(self, snapshot: HeartbeatSnapshot, *, success: bool) -> bool:
        """Ping the external monitor. Returns whether the ping was accepted."""
        if not self._ping_url:
            logger.info("heartbeat_ping_skipped", reason="HEALTHCHECKS_PING_URL not configured")
            return False

        url = self._ping_url if success else self._ping_url.rstrip("/") + "/fail"
        try:
            response = await self._client.get(url, headers={"Accept": "*/*"})
        except FetchError as exc:
            logger.error("heartbeat_ping_failed", error=str(exc))
            return False
        if 200 <= response.status < 300:
            logger.info("heartbeat_ping_sent", success=success)
            return True
        logger.error("heartbeat_ping_rejected", status=response.status)
        return False

    async def check(self) -> dict[str, object]:
        """Read the stored snapshot and evaluate staleness (used by maintenance)."""
        stored = await self._repo.get_state(STATE_KEY)
        overview = await self._repo.overview()
        last_success: datetime | None = overview.get("last_successful_run_at")
        stale = self._is_stale(last_success)
        problems: list[str] = []

        if stale:
            problems.append(
                "the latest scheduled research slot has no successful run "
                f"(last success: {last_success}; threshold is now schedule-aware)"
            )
        if overview.get("unknown_deliveries", 0):
            problems.append(
                f"{overview['unknown_deliveries']} delivery row(s) have an unknown outcome and need review"
            )
        if overview.get("sources_unhealthy", 0) >= 3:
            problems.append(f"{overview['sources_unhealthy']} sources are degraded or failing")

        last_backup = overview.get("last_backup_at")
        if last_backup is None:
            problems.append("no backup has ever completed")
        elif isinstance(last_backup, datetime) and last_backup < utcnow() - timedelta(days=8):
            problems.append(f"last backup is older than 8 days ({last_backup:%Y-%m-%d})")

        return {
            "stale": stale,
            "problems": problems,
            "snapshot": stored,
            "overview": overview,
        }

    def _is_stale(self, last_success: datetime | None) -> bool:
        """Whether the scheduled research window has missed a run.

        A fixed "now - N minutes" test is wrong for a system that is deliberately
        idle overnight: the 06:45 maintenance job would page the owner every
        morning because the last successful run was 22:30. Staleness is therefore
        measured against the most recent slot that should already have completed,
        with a grace period for a late cron start.
        """
        if last_success is None:
            return True
        if isinstance(last_success, str):
            last_success = parse_datetime(last_success)
            if last_success is None:
                return True
        last_success = to_utc(last_success)
        now = utcnow()

        slots = research_slots_between(now - timedelta(hours=48), now)
        if not slots:
            # No scheduled slot in the lookback: fall back to the plain threshold.
            return last_success < now - timedelta(minutes=self._stale_after)

        latest = slots[-1]
        grace = timedelta(minutes=SLOT_GRACE_MINUTES)
        # Only slots whose grace period has elapsed can be judged. With hourly
        # slots, comparing against the newest slot would always be inside its own
        # grace window and staleness would never fire.
        eligible = [slot for slot in slots if slot + grace <= now]
        if not eligible:
            return False
        return last_success < eligible[-1]
