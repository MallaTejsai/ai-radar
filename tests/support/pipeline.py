"""Pipeline wiring shared by integration and failure tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import httpx

from ai_radar.intelligence import IntelligenceEngine
from ai_radar.models.source import SourceRegistry, SourceSpec
from ai_radar.notifications import DeliveryDispatcher
from ai_radar.operations import ResearchPipeline
from ai_radar.operations.heartbeat import HeartbeatReporter
from ai_radar.verification import EvidenceVerifier
from support.fakes import FakeTelegram
from support.helpers import mock_client

FEED_TEMPLATE = """<?xml version="1.0"?>
<rss version="2.0"><channel><title>Test</title>
<item>
  <title>{title}</title>
  <link>{url}</link>
  <description><![CDATA[{summary}]]></description>
  <pubDate>{date}</pubDate>
</item>
</channel></rss>"""

RSS_SPEC = SourceSpec(
    id="test_feed",
    name="Test Feed",
    category="ai_models",
    tier="A",
    method="rss",
    url="https://example.com/feed.xml",
    max_items=10,
)


class AlwaysLocked:
    """Stand-in for the PostgreSQL advisory lock, which needs a real server."""

    def __init__(self, *args, **kwargs) -> None:
        self.acquired = True

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class NeverLocked(AlwaysLocked):
    def __init__(self, *args, **kwargs) -> None:
        self.acquired = False


def feed(
    title: str = "Introducing Model X, a new model",
    url: str = "https://example.com/model-x",
    summary: str = "A new model with a free tier and long context support.",
    date: str | None = None,
) -> str:
    # Relative to now, not a literal: a fixed pubDate silently becomes "stale"
    # two days later and the alert assertions start failing for a clock reason.
    if date is None:
        date = format_datetime(datetime.now(timezone.utc) - timedelta(hours=2), usegmt=True)
    return FEED_TEMPLATE.format(title=title, url=url, summary=summary, date=date)


def make_handler(body: str):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={})
        if request.url.path.endswith("/feed.xml"):
            return httpx.Response(
                200, text=body, headers={"content-type": "application/rss+xml"}
            )
        return httpx.Response(200, text="<html>ok</html>")

    return handler


def one_source_config(config, spec: SourceSpec = RSS_SPEC):
    cfg = config.model_copy(deep=True)
    cfg.sources = SourceRegistry(sources=[spec])
    return cfg


def build_pipeline(config, repo, handler, *, telegram=None, providers=None):
    """Wire a pipeline against in-memory persistence and a mock transport."""
    http = mock_client(handler)
    engine = IntelligenceEngine(
        settings=config.settings,
        signals=config.scoring.signals,
        client=http,
        cache=repo,
        metrics=repo,
        providers=providers if providers is not None else [],
    )
    dispatcher = DeliveryDispatcher(
        repo,
        telegram or FakeTelegram(),
        worker_id="test-worker",
        category_names={"ai_models": "AI Models"},
        max_per_run=5,
    )
    pipeline = ResearchPipeline(
        config=config,
        repository=repo,
        http=http,
        engine=engine,
        verifier=EvidenceVerifier(http, config.scoring.signals),
        dispatcher=dispatcher,
        heartbeat=HeartbeatReporter(repo, http),
    )
    return http, pipeline
