"""Provider-neutral contracts for the intelligence layer."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ..models.analysis import AnalysisPayload
from ..models.core import Provider


class ProviderError(RuntimeError):
    """A provider call failed in a classified way.

    ``kind`` drives fallback and health accounting:
      auth             - key missing/invalid (configuration problem, do not retry)
      rate_limit       - 429; honour ``retry_after`` or fall back
      quota            - the provider's free allowance is exhausted (e.g. free.ai 402)
      unavailable      - 5xx / timeout / transport
      invalid_response - the response could not be validated
      not_configured   - no credential present
    """

    def __init__(self, message: str, *, kind: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.retry_after = retry_after

    @property
    def retryable(self) -> bool:
        return self.kind in {"rate_limit", "unavailable"}


@dataclass(slots=True)
class ProviderCall:
    """Result of one successful provider call."""

    payload: AnalysisPayload
    provider: Provider
    model_id: str
    latency_ms: int
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    http_status: int | None = None


@runtime_checkable
class IntelligenceProvider(Protocol):
    """What the router needs from a provider adapter.

    ``prompt`` already contains the analyst instruction and the fenced untrusted
    content; adapters add their own system prompt, transport and parsing.
    """

    name: Provider

    async def resolve_model(self) -> str:
        """Return the model id that will be used, resolved from the live API."""

    async def analyze(self, prompt: str) -> ProviderCall:
        """Analyse one prompt, returning a validated payload or raising ProviderError."""
