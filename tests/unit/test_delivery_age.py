"""F8 regression tests: queue age is enforced at claim time.

Staging had five eligible deliveries queued with ``attempts = 0``. The age
window was checked when the intent was created, never when it was claimed, so a
slowly drained queue could deliver news days later.
"""

from __future__ import annotations

from datetime import timedelta

from ai_radar.models.core import DeliveryStatus
from ai_radar.notifications import DeliveryDispatcher
from ai_radar.utils.timeutil import utcnow
from support.fakes import FakeRepository, FakeTelegram

HASH = "0123456789abcdef"


def _seed(repo: FakeRepository, key: str, *, age_hours: float) -> str:
    discovery_id = f"id-{key}"
    repo.discoveries[key] = {
        "id": discovery_id,
        "canonical_key": key,
        "published_at": (utcnow() - timedelta(hours=age_hours)).isoformat(),
    }
    return discovery_id


async def test_a_stale_intent_is_expired_instead_of_sent():
    repo = FakeRepository()
    discovery_id = _seed(repo, "old", age_hours=120)
    await repo.queue_delivery(discovery_id, "old message", HASH)

    telegram = FakeTelegram()
    dispatcher = DeliveryDispatcher(
        repo, telegram, worker_id="test", max_per_run=5, alert_max_age_hours=48
    )
    outcome = await dispatcher.dispatch(limit=5)

    assert outcome.sent == 0
    assert telegram.sent == []
    row = next(iter(repo.deliveries.values()))
    assert row["status"] == DeliveryStatus.UNKNOWN.value
    assert "expired" in row["last_error"]


async def test_a_fresh_intent_is_still_delivered_next_to_a_stale_one():
    repo = FakeRepository()
    stale_id = _seed(repo, "old", age_hours=120)
    fresh_id = _seed(repo, "new", age_hours=1)
    await repo.queue_delivery(stale_id, "old message", HASH)
    await repo.queue_delivery(fresh_id, "fresh message", HASH)

    telegram = FakeTelegram()
    dispatcher = DeliveryDispatcher(
        repo, telegram, worker_id="test", max_per_run=5, alert_max_age_hours=48
    )
    outcome = await dispatcher.dispatch(limit=5)

    assert outcome.sent == 1
    assert telegram.sent == ["fresh message"]


async def test_without_an_age_limit_nothing_is_expired():
    """The default (None) preserves the previous behaviour for non-pipeline callers."""
    repo = FakeRepository()
    discovery_id = _seed(repo, "old", age_hours=120)
    await repo.queue_delivery(discovery_id, "old message", HASH)

    telegram = FakeTelegram()
    dispatcher = DeliveryDispatcher(repo, telegram, worker_id="test", max_per_run=5)
    outcome = await dispatcher.dispatch(limit=5)

    assert outcome.sent == 1
