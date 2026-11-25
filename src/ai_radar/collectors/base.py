"""Collector contract shared by every collection method."""

from __future__ import annotations

from dataclasses import dataclass, field

from ..models.discovery import RawEntry
from ..models.source import SourceSpec, SourceState
from ..utils.net import SafeHttpClient


@dataclass(slots=True)
class CollectResult:
    """Outcome of collecting one source.

    ``error`` is set instead of raising so one broken source can never stop the
    others, and so the failure can be persisted as source health.
    """

    source_id: str
    entries: list[RawEntry] = field(default_factory=list)
    etag: str | None = None
    last_modified: str | None = None
    not_modified: bool = False
    error: str | None = None
    note: str = ""
    http_status: int | None = None
    elapsed_ms: int = 0

    @property
    def ok(self) -> bool:
        return self.error is None


#: A collector is an async callable with this signature.
Collector = "callable"


async def collect_source(
    spec: SourceSpec,
    state: SourceState | None,
    client: SafeHttpClient,
    *,
    github_token: str = "",
) -> CollectResult:
    """Dispatch to the collector for ``spec.method``.

    Imported lazily to keep module import cost low and avoid cycles.
    """
    from ..utils.timeutil import utcnow
    from .github import collect_github_releases
    from .huggingface import collect_huggingface_models
    from .rss import collect_rss

    started = utcnow()
    effective_state = state or SourceState(source_id=spec.id)

    try:
        if spec.method.value == "rss":
            result = await collect_rss(spec, effective_state, client)
        elif spec.method.value == "github_releases":
            result = await collect_github_releases(
                spec, effective_state, client, token=github_token
            )
        elif spec.method.value == "huggingface_models":
            result = await collect_huggingface_models(spec, effective_state, client)
        else:  # pragma: no cover - enum makes this unreachable
            result = CollectResult(
                source_id=spec.id, error=f"unsupported method {spec.method}"
            )
    except Exception as exc:  # noqa: BLE001 - isolation boundary
        result = CollectResult(
            source_id=spec.id,
            error=f"{type(exc).__name__}: {exc}",
        )

    result.elapsed_ms = int((utcnow() - started).total_seconds() * 1000)
    result.source_id = spec.id
    return result
