"""PostgreSQL integration tests.

These require a real database and are skipped otherwise. Point TEST_DATABASE_URL at
a dedicated database (never production): the tests truncate tables.

Safety guard: the suite refuses to run unless the database name contains
test/ci/scratch/sandbox, or AI_RADAR_ALLOW_DESTRUCTIVE_TESTS=1 is set explicitly.
Pointing TEST_DATABASE_URL at the production project therefore skips instead of
wiping it.

    $env:TEST_DATABASE_URL="postgresql://..."
    python -m pytest tests/integration/test_postgres.py -m postgres
"""

from __future__ import annotations

import os
import uuid
from datetime import timedelta

import pytest

from ai_radar.models.analysis import AnalysisPayload, AnalysisResult
from ai_radar.models.core import Category, DiscoveryStatus, PricingStatus, Provider, SourceTier, Verdict
from ai_radar.models.discovery import DiscoveryRecord, Evidence, ScoreBreakdown, ScoreComponent, VerificationDetail
from ai_radar.models.source import SourceRegistry, SourceSpec
from ai_radar.storage import Database, Repository, RunLock, apply_migrations
from ai_radar.storage.db import is_disposable_database
from ai_radar.utils.timeutil import utcnow

DSN = os.environ.get("TEST_DATABASE_URL", "")
DESTRUCTIVE_TESTS_ALLOWED = bool(DSN) and (
    is_disposable_database(DSN)
    or os.environ.get("AI_RADAR_ALLOW_DESTRUCTIVE_TESTS") == "1"
)
pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not DSN, reason="TEST_DATABASE_URL is not set"),
    pytest.mark.skipif(
        bool(DSN) and not DESTRUCTIVE_TESTS_ALLOWED,
        reason=(
            "refusing to truncate a database whose name is not test/ci/scratch/sandbox; "
            "set AI_RADAR_ALLOW_DESTRUCTIVE_TESTS=1 only for a disposable database"
        ),
    ),
]

TABLES = (
    "deliveries, discovery_sources, analysis_results, pricing_history, discoveries, "
    "workflow_runs, daily_digests, backup_manifests, system_state, analysis_cache, "
    "provider_usage, sources"
)


@pytest.fixture
async def database():
    db = Database(DSN, min_size=1, max_size=3)
    await db.connect()
    await apply_migrations(db)
    async with db.transaction() as cursor:
        await cursor.execute(f"truncate table {TABLES} restart identity cascade")
    try:
        yield db
    finally:
        await db.close()


@pytest.fixture
async def repo(database) -> Repository:
    return Repository(database)


def spec(source_id: str = "test_feed") -> SourceSpec:
    return SourceSpec(
        id=source_id,
        name="Test Feed",
        category=Category.AI_MODELS,
        tier=SourceTier.A,
        method="rss",
        url="https://example.com/feed.xml",
    )


def record(key: str = "url:" + "a" * 32, **overrides) -> DiscoveryRecord:
    payload = {
        "canonical_key": key,
        "title": "Introducing Model X",
        "category": Category.AI_MODELS,
        "summary": "A new model.",
        "url": "https://example.com/model-x",
        "source_id": "test_feed",
        "tier": SourceTier.A,
        "published_at": utcnow(),
        "priority_score": 72,
        "confidence": 0.88,
        "status": DiscoveryStatus.VERIFIED,
        "pricing_status": PricingStatus.FREE_TIER,
        "verdict": Verdict.CONFIRMED,
        "evidence": [
            Evidence(
                url="https://example.com/model-x",
                source_id="test_feed",
                tier=SourceTier.A,
                domain="example.com",
                http_status=200,
                checked_at=utcnow(),
                reachable=True,
            )
        ],
        "verification": VerificationDetail(date_check="CONFIRMED", availability_check="CONFIRMED"),
        "score_breakdown": ScoreBreakdown(
            components=[ScoreComponent(name="novelty", raw=1.0, weight=15, points=15)],
            total=72,
            label="4/5",
        ),
    }
    payload.update(overrides)
    return DiscoveryRecord(**payload)


async def test_migrations_are_idempotent(database):
    first = await apply_migrations(database)
    second = await apply_migrations(database)
    assert first == []  # already applied by the fixture
    assert second == []


async def test_source_sync_and_checkpoint(repo):
    await repo.sync_sources([spec()])
    states = await repo.load_source_states()
    assert states["test_feed"].health_status == "unknown"

    await repo.record_source_success(
        "test_feed", etag='"abc"', last_modified=None, entries=3, checkpoint=utcnow()
    )
    states = await repo.load_source_states()
    assert states["test_feed"].health_status == "healthy"
    assert states["test_feed"].etag == '"abc"'
    assert states["test_feed"].records_collected == 3

    await repo.record_source_failure("test_feed", "boom")
    states = await repo.load_source_states()
    assert states["test_feed"].health_status == "degraded"
    assert states["test_feed"].last_error == "boom"


async def test_discovery_upsert_is_idempotent_and_reports_creation(repo):
    await repo.sync_sources([spec()])
    analysis = AnalysisResult(
        discovery_key="url:" + "a" * 32,
        provider=Provider.GEMINI,
        model_id="gemini-test",
        payload=AnalysisPayload(summary="A new model with long context."),
        prompt_version="2026-11-a",
        input_fingerprint="fp1",
    )

    discovery_id, created = await repo.upsert_discovery(record(), analysis)
    assert created is True
    discovery_id_2, created_2 = await repo.upsert_discovery(record(priority_score=80), analysis)
    assert created_2 is False
    assert discovery_id == discovery_id_2

    stored = await repo.get_discovery("url:" + "a" * 32)
    assert stored["priority_score"] == 80
    assert stored["verdict"] == "CONFIRMED"

    async with repo.database.transaction() as cursor:
        await cursor.execute("select count(*) as n from discovery_sources")
        assert (await cursor.fetchone())["n"] == 1
        await cursor.execute("select provider from analysis_results")
        assert (await cursor.fetchone())["provider"] == "gemini"
        await cursor.execute("select count(*) as n from pricing_history")
        assert (await cursor.fetchone())["n"] == 1


async def test_pricing_history_only_records_changes(repo):
    await repo.sync_sources([spec()])
    await repo.upsert_discovery(record())
    await repo.upsert_discovery(record())
    await repo.upsert_discovery(record(pricing_status=PricingStatus.PAID))
    async with repo.database.transaction() as cursor:
        await cursor.execute("select pricing_status from pricing_history order by id")
        rows = await cursor.fetchall()
    assert [row["pricing_status"] for row in rows] == ["FREE_TIER", "PAID"]


async def test_canonical_key_uniqueness_is_enforced(repo):
    await repo.sync_sources([spec()])
    discovery_id, _ = await repo.upsert_discovery(record())
    async with repo.database.transaction() as cursor:
        await cursor.execute("select count(*) as n from discoveries where id = %s", (discovery_id,))
        assert (await cursor.fetchone())["n"] == 1


async def test_delivery_intent_is_unique_per_discovery(repo):
    await repo.sync_sources([spec()])
    discovery_id, _ = await repo.upsert_discovery(record())
    first = await repo.queue_delivery(discovery_id, "message", "hash" * 8)
    second = await repo.queue_delivery(discovery_id, "message", "hash" * 8)
    assert first is not None
    assert second is None


async def test_claim_is_atomic_and_bounded(repo):
    await repo.sync_sources([spec()])
    for index in range(4):
        discovery_id, _ = await repo.upsert_discovery(record(key=f"url:{index:032d}"))
        await repo.queue_delivery(discovery_id, f"message {index}", "hash" * 8)

    claimed = await repo.claim_deliveries("worker-a", 2)
    assert len(claimed) == 2
    assert all(intent.status.value == "SENDING" for intent in claimed)
    assert all(intent.attempts == 1 for intent in claimed)

    # A second worker cannot take the rows already claimed.
    second = await repo.claim_deliveries("worker-b", 5)
    assert len(second) == 2
    assert {intent.id for intent in claimed}.isdisjoint({intent.id for intent in second})


async def test_sent_delivery_is_not_reclaimed(repo):
    await repo.sync_sources([spec()])
    discovery_id, _ = await repo.upsert_discovery(record())
    await repo.queue_delivery(discovery_id, "message", "hash" * 8)
    claimed = await repo.claim_deliveries("worker", 1)
    await repo.mark_delivery_sent(str(claimed[0].id), 4242)
    assert await repo.claim_deliveries("worker", 5) == []

    counts = await repo.delivery_counts()
    assert counts["SENT"] == 1
    stored = await repo.get_discovery(record().canonical_key)
    assert stored["status"] == "verified"  # mark_notified is a separate call


async def test_unknown_delivery_is_never_reclaimed(repo):
    await repo.sync_sources([spec()])
    discovery_id, _ = await repo.upsert_discovery(record())
    await repo.queue_delivery(discovery_id, "message", "hash" * 8)
    claimed = await repo.claim_deliveries("worker", 1)
    await repo.mark_delivery_unknown(str(claimed[0].id), "timeout after send")
    assert await repo.claim_deliveries("worker", 5) == []
    counts = await repo.delivery_counts()
    assert counts["UNKNOWN"] == 1


async def test_stale_sending_rows_become_unknown(repo):
    await repo.sync_sources([spec()])
    discovery_id, _ = await repo.upsert_discovery(record())
    await repo.queue_delivery(discovery_id, "message", "hash" * 8)
    claimed = await repo.claim_deliveries("worker", 1)
    async with repo.database.transaction() as cursor:
        await cursor.execute(
            "update deliveries set claimed_at = now() - interval '2 hours' where id = %s",
            (str(claimed[0].id),),
        )
    released = await repo.release_stale_claims(15)
    assert released == 1
    assert (await repo.delivery_counts())["UNKNOWN"] == 1


async def test_existing_identities_matches_keys_and_fingerprints(repo):
    await repo.sync_sources([spec()])
    stored = record()
    await repo.upsert_discovery(stored)
    keys, fingerprints = await repo.existing_identities(
        [stored.canonical_key], [stored.title_fingerprint]
    )
    assert stored.canonical_key in keys
    assert fingerprints[stored.title_fingerprint] == stored.canonical_key


async def test_workflow_run_lifecycle(repo):
    from ai_radar.models.run import RunReport

    report = RunReport(run_id="run-1", started_at=utcnow(), trigger="test")
    await repo.start_run(report)
    report.finished_at = utcnow()
    report.persisted_new = 3
    await repo.finish_run(report)

    assert await repo.last_successful_run() is not None
    run_id, status = await repo.last_run_status()
    assert run_id == "run-1"
    assert status == "running"  # RunReport.status stays "running" unless set

    runs = await repo.recent_run_starts(hours=1)
    assert len(runs) == 1


async def test_overview_aggregates(repo):
    await repo.sync_sources([spec()])
    discovery_id, _ = await repo.upsert_discovery(record())
    await repo.queue_delivery(discovery_id, "message", "hash" * 8)
    overview = await repo.overview()
    assert overview["sources_total"] == 1
    assert overview["pending_deliveries"] == 1
    assert overview["discoveries_total"] == 1
    assert overview["discoveries_confirmed"] == 1


async def test_advisory_lock_blocks_a_second_runner(database):
    first = RunLock(database, "ai_radar_test_lock", timeout_seconds=1)
    async with first:
        assert first.acquired is True
        second = RunLock(database, "ai_radar_test_lock", timeout_seconds=1)
        async with second:
            assert second.acquired is False
    third = RunLock(database, "ai_radar_test_lock", timeout_seconds=1)
    async with third:
        assert third.acquired is True


async def test_digest_upsert(repo):
    from ai_radar.utils.timeutil import today_ist

    day = today_ist()
    await repo.save_digest(digest_date=day, text="first", discovery_keys=["a"], count=1)
    await repo.save_digest(digest_date=day, text="second", discovery_keys=["a", "b"], count=2)
    stored = await repo.get_digest(day)
    assert stored["digest_text"] == "second"
    assert stored["discovery_count"] == 2


async def test_analysis_cache_roundtrip(repo):
    result = AnalysisResult(
        discovery_key="fp",
        provider=Provider.GROQ,
        model_id="gpt-oss-120b",
        payload=AnalysisPayload(summary="Cached analysis with enough length."),
        prompt_version="2026-11-a",
        input_fingerprint="fp",
    )
    await repo.cache_put("fp", result)
    cached = await repo.cache_get("fp")
    assert cached is not None
    assert cached.provider is Provider.GROQ
    assert cached.cached is True
