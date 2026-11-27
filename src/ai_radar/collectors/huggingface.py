"""Hugging Face Hub models API collection.

The Hub API returns JSON, not a feed. A model id (``owner/name``) is the stable
identity, so renames of the model page do not create a second discovery.
"""

from __future__ import annotations

from typing import Any

from ..models.discovery import RawEntry
from ..models.source import SourceSpec, SourceState
from ..utils.net import FetchError, SafeHttpClient, fetch_json_document
from .base import CollectResult

_NEW_REPO_WINDOW_NOTE = "listed by the Hub API as a newly created repository"


def parse_models(payload: Any, spec: SourceSpec) -> tuple[list[RawEntry], int]:
    """Parse Hub rows, applying the source's substance gates.

    Returns ``(entries, filtered_out)`` so the caller can report how much volume
    the gates removed - silence is not the same thing as "nothing happened".
    """
    if isinstance(payload, dict) and payload.get("error"):
        raise ValueError(f"Hugging Face API error: {payload.get('error')}")
    if not isinstance(payload, list):
        raise ValueError("unexpected Hugging Face payload (expected a JSON array)")

    entries: list[RawEntry] = []
    filtered_out = 0
    for model in payload:
        if not isinstance(model, dict):
            continue
        model_id = str(model.get("id") or model.get("modelId") or "").strip()
        if not model_id or "/" not in model_id:
            filtered_out += 1
            continue
        likes = model.get("likes")
        downloads = model.get("downloads")
        if spec.min_likes and (not isinstance(likes, int) or likes < spec.min_likes):
            filtered_out += 1
            continue
        if spec.min_downloads and (
            not isinstance(downloads, int) or downloads < spec.min_downloads
        ):
            filtered_out += 1
            continue
        pipeline = str(model.get("pipeline_tag") or "").strip()
        tags = model.get("tags") or []
        tag_sample = ", ".join(str(t) for t in tags[:8]) if isinstance(tags, list) else ""
        stats = []
        if isinstance(downloads, int):
            stats.append(f"{downloads:,} downloads")
        if isinstance(likes, int):
            stats.append(f"{likes:,} likes")
        summary_parts = [
            f"New Hub repository ({pipeline or 'model'}) - {_NEW_REPO_WINDOW_NOTE}.",
            f"Tags: {tag_sample}." if tag_sample else "",
            f"Stats: {'; '.join(stats)}." if stats else "",
        ]
        entries.append(
            RawEntry(
                source_id=spec.id,
                title=f"{model_id}" + (f" ({pipeline})" if pipeline else ""),
                url=f"https://huggingface.co/{model_id}",
                summary=" ".join(part for part in summary_parts if part),
                published_raw=str(model.get("createdAt") or "") or None,
                author=model_id.split("/", 1)[0],
                external_id=model_id,
                kind_hint="huggingface_models",
            )
        )
        if len(entries) >= spec.max_items:
            break
    return entries, filtered_out


async def collect_huggingface_models(
    spec: SourceSpec, state: SourceState, client: SafeHttpClient
) -> CollectResult:
    try:
        payload, response = await fetch_json_document(
            client,
            spec.url,
            headers={"Accept": "application/json"},
            etag=state.etag,
            last_modified=state.last_modified,
        )
    except FetchError as exc:
        if exc.kind == "not_modified":
            return CollectResult(
                source_id=spec.id,
                not_modified=True,
                etag=state.etag,
                last_modified=state.last_modified,
                http_status=304,
            )
        return CollectResult(
            source_id=spec.id,
            error=f"{exc.kind}: {exc}",
            http_status=exc.status,
        )

    try:
        entries, filtered_out = parse_models(payload, spec)
    except ValueError as exc:
        return CollectResult(source_id=spec.id, error=str(exc), http_status=200)

    note = (
        f"{filtered_out} repositories below the substance gate "
        f"(min_likes={spec.min_likes}, min_downloads={spec.min_downloads})"
        if filtered_out
        else ""
    )
    if not entries:
        return CollectResult(
            source_id=spec.id,
            note=note or "Hub API returned no rows",
            http_status=200,
        )

    return CollectResult(
        source_id=spec.id,
        entries=entries,
        note=note,
        etag=response.etag,
        last_modified=response.last_modified,
        http_status=200,
    )
