"""Collectors for the curated source registry.

Collection is concurrent but strictly bounded, and every source is isolated: a
timeout, a 404 or a malformed body affects only that source's ``CollectResult``.
"""

from __future__ import annotations

import asyncio

from ..models.source import SourceSpec, SourceState
from ..utils.logging import get_logger
from ..utils.net import SafeHttpClient
from .base import CollectResult, collect_source

logger = get_logger(__name__)

__all__ = ["CollectResult", "collect_all", "collect_source"]


async def collect_all(
    specs: list[SourceSpec],
    states: dict[str, SourceState],
    client: SafeHttpClient,
    *,
    concurrency: int = 4,
    github_token: str = "",
) -> dict[str, CollectResult]:
    """Collect every source, returning a result per source id.

    Results are never dropped: a source that raises still gets a ``CollectResult``
    carrying the error, so health reporting cannot silently lose it.
    """
    semaphore = asyncio.Semaphore(max(1, concurrency))
    results: dict[str, CollectResult] = {}

    async def worker(spec: SourceSpec) -> CollectResult:
        async with semaphore:
            return await collect_source(
                spec, states.get(spec.id), client, github_token=github_token
            )

    outcomes = await asyncio.gather(
        *(worker(spec) for spec in specs), return_exceptions=True
    )

    for spec, outcome in zip(specs, outcomes):
        if isinstance(outcome, BaseException):
            logger.error("collector_crashed", source_id=spec.id, error=str(outcome))
            results[spec.id] = CollectResult(
                source_id=spec.id, error=f"collector crash: {type(outcome).__name__}: {outcome}"
            )
        else:
            results[spec.id] = outcome

    ok = sum(1 for result in results.values() if result.ok)
    logger.info(
        "collection_batch_complete",
        sources=len(results),
        ok=ok,
        failed=len(results) - ok,
        entries=sum(len(result.entries) for result in results.values()),
    )
    return results
