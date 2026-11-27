"""Workflow run accounting and heartbeat state."""

from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field

from .core import RunStatus


class RunReport(BaseModel):
    """Everything one pipeline execution learned about itself.

    This is the object written to ``workflow_runs`` and embedded in the
    heartbeat snapshot, so it must never contain secrets.
    """

    model_config = ConfigDict(extra="ignore")

    run_id: str
    started_at: datetime
    finished_at: datetime | None = None
    status: RunStatus = RunStatus.RUNNING
    trigger: str = "schedule"
    recovery_window_hours: int = 24
    recovered_missed_runs: int = 0
    sources_attempted: int = 0
    sources_failed: int = 0
    entries_collected: int = 0
    after_dedup: int = 0
    verified: int = 0
    discarded_low_confidence: int = 0
    discarded_unverified: int = 0
    #: Explicit reason -> count for every candidate the run rejected. A counter
    #: alone cannot explain *why* a paid analysis never reached the database.
    discard_reasons: dict[str, int] = Field(default_factory=dict)
    qualified: int = 0
    persisted_new: int = 0
    persisted_updated: int = 0
    alerts_sent: int = 0
    ai_provider_counts: dict[str, int] = Field(default_factory=dict)
    ai_errors: int = 0
    checkpoints_advanced: int = 0
    errors: list[str] = Field(default_factory=list)
    duration_seconds: float = 0.0

    def add_error(self, message: str) -> None:
        if len(self.errors) < 50:
            self.errors.append(message[:400])

    def record_discard(self, reason: str) -> None:
        """Count one discarded candidate under an explicit, stable reason."""
        self.discard_reasons[reason] = self.discard_reasons.get(reason, 0) + 1


class HeartbeatSnapshot(BaseModel):
    """Written after all critical stages, and pinged to the external monitor."""

    model_config = ConfigDict(extra="ignore")

    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    last_successful_run_at: datetime | None = None
    last_run_id: str | None = None
    last_run_status: RunStatus | None = None
    last_heartbeat_at: datetime | None = None
    sources_total: int = 0
    sources_unhealthy: int = 0
    database_ok: bool = False
    pending_deliveries: int = 0
    unknown_deliveries: int = 0
    last_backup_at: datetime | None = None
    last_backup_status: str | None = None
    providers: dict[str, str] = Field(default_factory=dict)
    stale: bool = False
    detail: str = ""
