"""GitHub REST releases collection.

Uses the public REST API with an optional read-only token (60 -> 5000 req/hour).
Release identity is ``owner/repo@tag`` rather than the web URL, so a re-tagged or
edited release keeps the same canonical key.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

from ..models.discovery import RawEntry
from ..models.source import SourceSpec, SourceState
from ..utils.logging import get_logger
from ..utils.net import FetchError, SafeHttpClient, fetch_json_document
from .base import CollectResult

logger = get_logger(__name__)

_REPO_RE = re.compile(r"/repos/([^/]+)/([^/]+)/releases")


def repo_identity(url: str) -> str:
    match = _REPO_RE.search(urlsplit(url).path)
    if not match:
        return urlsplit(url).path.strip("/").replace("/", ".")
    return f"{match.group(1)}/{match.group(2)}"


def parse_releases(payload: Any, spec: SourceSpec) -> list[RawEntry]:
    """Convert a releases payload into raw entries (drafts excluded)."""
    if not isinstance(payload, list):
        # A single release object, or an API error envelope.
        if isinstance(payload, dict) and "message" in payload:
            raise ValueError(f"GitHub API error: {payload.get('message')}")
        payload = [payload] if isinstance(payload, dict) else []

    repo = repo_identity(spec.url)
    entries: list[RawEntry] = []
    for release in payload:
        if not isinstance(release, dict) or release.get("draft"):
            continue
        tag = str(release.get("tag_name") or release.get("name") or "").strip()
        if not tag:
            continue
        body = str(release.get("body") or "")
        name = str(release.get("name") or "").strip()
        title = f"{repo.split('/')[-1]} {tag}"
        if name and name.lower() != tag.lower():
            title = f"{title} — {name}"
        author = ""
        author_payload = release.get("author")
        if isinstance(author_payload, dict):
            author = str(author_payload.get("login") or "")
        entries.append(
            RawEntry(
                source_id=spec.id,
                title=title,
                url=str(release.get("html_url") or f"https://github.com/{repo}/releases/tag/{tag}"),
                summary=body,
                published_raw=str(release.get("published_at") or release.get("created_at") or ""),
                author=author or None,
                external_id=f"{repo}@{tag}",
                kind_hint="github_releases",
            )
        )
        if len(entries) >= spec.max_items:
            break
    return entries


async def collect_github_releases(
    spec: SourceSpec,
    state: SourceState,
    client: SafeHttpClient,
    *,
    token: str = "",
) -> CollectResult:
    """Collect releases for one repository."""
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        payload, response = await fetch_json_document(
            client,
            spec.url,
            headers=headers,
            etag=state.etag,
            last_modified=state.last_modified,
        )
    except FetchError as exc:
        if exc.status == 401 and token:
            # A present-but-invalid token must not silently disable collection for
            # every repository: public releases are readable anonymously. Say so
            # loudly, then fall back to an unauthenticated request (60 req/h).
            logger.warning(
                "github_token_rejected",
                source_id=spec.id,
                note="retrying anonymously; check GITHUB_TOKEN",
            )
            try:
                payload, response = await fetch_json_document(
                    client,
                    spec.url,
                    headers={k: v for k, v in headers.items() if k != "Authorization"},
                    etag=state.etag,
                    last_modified=state.last_modified,
                )
            except FetchError as retry_exc:
                return CollectResult(
                    source_id=spec.id,
                    error=f"{retry_exc.kind}: {retry_exc}",
                    http_status=retry_exc.status,
                )
            try:
                entries = parse_releases(payload, spec)
            except ValueError as parse_exc:
                return CollectResult(
                    source_id=spec.id, error=str(parse_exc), http_status=response.status
                )
            return CollectResult(
                source_id=spec.id,
                entries=entries,
                note="GITHUB_TOKEN was rejected; collected anonymously",
                etag=response.etag,
                last_modified=response.last_modified,
                http_status=response.status,
            )
        if exc.kind == "not_modified":
            # The feed is unchanged: keep the validators so the next run is cheap
            # too, and let the checkpoint advance without re-processing anything.
            return CollectResult(
                source_id=spec.id,
                not_modified=True,
                etag=state.etag,
                last_modified=state.last_modified,
                http_status=304,
            )
        hint = ""
        if exc.status in (403, 429):
            hint = " (GitHub rate limit - set GITHUB_TOKEN to raise 60/hr to 5000/hr)"
        return CollectResult(
            source_id=spec.id,
            error=f"{exc.kind}: {exc}{hint}",
            http_status=exc.status,
        )

    try:
        entries = parse_releases(payload, spec)
    except ValueError as exc:
        return CollectResult(source_id=spec.id, error=str(exc), http_status=200)

    if not entries:
        return CollectResult(
            source_id=spec.id,
            note="no published releases in the API response",
            http_status=200,
        )

    return CollectResult(
        source_id=spec.id,
        entries=entries,
        etag=response.etag,
        last_modified=response.last_modified,
        http_status=200,
    )
