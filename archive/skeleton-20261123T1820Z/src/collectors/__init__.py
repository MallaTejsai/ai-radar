"""Collectors module for AI Radar V1.0 - RSS and HTTP source collection."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import structlog

from ...settings import get_source_config, get_settings

logger = structlog.get_logger()


async def collect_source(source_config: dict) -> Tuple[List[dict], dict]:
    """Collect entries from a single source.

    Returns:
        Tuple of (entries list, metadata dict with checkpoint/errors)
    """
    source_id = source_config.get("id", "unknown")
    collection_method = source_config.get("collection_method", "rss_poll")
    enabled = source_config.get("enabled", True)

    if not enabled:
        logger.info("source_disabled", source_id=source_id)
        return [], {"checkpoint": None, "error": None, "fetched": 0}

    metadata = {
        "checkpoint": source_config.get("checkpoint"),
        "last_error": None,
        "fetched": 0,
        "health_status": source_config.get("health_status", "healthy"),
    }

    try:
        if collection_method == "rss_poll":
            entries = await _collect_rss(source_config)
        elif collection_method == "http_get":
            entries = await _collect_http_get(source_config)
        elif collection_method == "github_api":
            entries = await _collect_github_api(source_config)
        else:
            logger.warning("unknown_collection_method", source_id=source_id, method=collection_method)
            return [], metadata

        metadata["fetched"] = len(entries)
        metadata["checkpoint"] = datetime.now(timezone.utc).isoformat()
        metadata["last_error"] = None

        logger.info(
            "source_collected",
            source_id=source_id,
            entries_fetched=len(entries),
            checkpoint=metadata["checkpoint"],
        )

        return entries, metadata

    except Exception as e:
        metadata["last_error"] = str(e)
        metadata["checkpoint"] = source_config.get("checkpoint")

        logger.error(
            "source_collection_failed",
            source_id=source_id,
            error=metadata["last_error"],
        )

        return [], metadata


async def _collect_rss(source_config: dict) -> List[dict]:
    """Collect entries from an RSS feed."""
    import feedparser

    url = source_config.get("url", "")
    if not url:
        return []

    try:
        loop = asyncio.get_event_loop()
        entries = await loop.run_in_executor(None, lambda: feedparser.parse(url))

        results = []
        for entry in entries.get("entries", []):
            result = {
                "title": entry.get("title", ""),
                "link": entry.get("link", ""),
                "published": entry.get("published", ""),
                "summary": entry.get("summary", ""),
                "author": entry.get("author", ""),
                "source_id": source_config.get("id", "unknown"),
                "raw_feed": url,
            }
            results.append(result)

        return results

    except Exception as e:
        logger.error("rss_collection_failed", url=url, error=str(e))
        return []


async def _collect_http_get(source_config: dict) -> List[dict]:
    """Collect entries via HTTP GET and JSON parsing."""
    import json

    url = source_config.get("url", "")
    headers = source_config.get("headers", {})

    if not url:
        return []

    try:
        import httpx

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(url, headers=headers or None)
            response.raise_for_status()

            data = response.json()

            # Handle various JSON structures
            results = []

            # Common patterns for blog/api responses
            if isinstance(data, list):
                items = data
            elif isinstance(data, dict):
                # Try common keys
                for key in ["items", "entries", "articles", "posts", "data"]:
                    if key in data:
                        items = data[key]
                        break
                else:
                    items = [data]
            else:
                items = [data]

            for item in items:
                result = {
                    "title": item.get("title", item.get("name", "")),
                    "link": item.get("url", item.get("link", "")),
                    "published": item.get("published", item.get("date", "")),
                    "summary": item.get("summary", item.get("description", "")),
                    "author": item.get("author", ""),
                    "source_id": source_config.get("id", "unknown"),
                }
                results.append(result)

            return results

    except Exception as e:
        logger.error("http_get_failed", url=url, error=str(e))
        return []


async def _collect_github_api(source_config: dict) -> List[dict]:
    """Collect entries from GitHub API (releases, tags, etc.)."""
    import json

    url = source_config.get("url", "")
    if not url:
        return []

    try:
        import httpx

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(url, headers={"Accept": "application/vnd.github.v3+json"})
            response.raise_for_status()

            data = response.json()

            results = []

            # GitHub API various response formats
            if isinstance(data, list):
                items = data
            elif isinstance(data, dict):
                for key in ["items", "releases", "tags", "commits"]:
                    if key in data:
                        items = data[key]
                        break
                else:
                    items = [data]
            else:
                items = [data]

            for item in items:
                result = {
                    "title": item.get("name", item.get("title", "")),
                    "link": item.get("html_url", item.get("html_url", "")),
                    "published": item.get("published_at", item.get("commit", {}).get("committer", {}).get("date", "")),
                    "summary": item.get("body", item.get("description", ""))[:500] if item.get("body") else "",
                    "author": item.get("author", {}).get("login", ""),
                    "source_id": source_config.get("id", "unknown"),
                }
                results.append(result)

            return results

    except Exception as e:
        logger.error("github_api_failed", url=url, error=str(e))
        return []


async def collect_all_sources() -> Dict[str, Tuple[List[dict], dict]]:
    """Collect entries from all enabled sources.

    Returns dict mapping source_id -> (entries, metadata)
    """
    settings = get_settings()
    sources_config = settings.get("sources", {}).get("sources", {})

    results = {}

    # Collect in parallel with concurrency limit
    semaphore = asyncio.Semaphore(5)  # Max 5 concurrent collections

    async def collect_with_semaphore(source_id: str, config: dict):
        async with semaphore:
            entries, metadata = await collect_source(config)
            return source_id, entries, metadata

    tasks = []
    for source_id, config in sources_config.items():
        if config.get("enabled", True):
            tasks.append(collect_with_semaphore(source_id, config))

    task_results = await asyncio.gather(*tasks, return_exceptions=True)

    for result in task_results:
        if isinstance(result, Exception):
            logger.error("collection_task_failed", error=str(result))
        else:
            source_id, entries, metadata = result
            results[source_id] = (entries, metadata)

    return results