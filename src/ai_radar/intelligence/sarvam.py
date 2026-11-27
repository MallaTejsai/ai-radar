"""Sarvam AI adapter (OpenAI-compatible chat completions).

Sarvam is a **paid** provider (credits). Endpoint:
``POST https://api.sarvam.ai/v2/chat/completions``
Auth: ``api-subscription-key: <key>`` (also accepts Bearer).
Usage is reported in the OpenAI shape.
"""

from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable

from ..models.analysis import AnalysisPayload
from ..models.core import Provider
from ..utils.logging import get_logger
from ..utils.net import FetchError, SafeHttpClient
from ..utils.redact import scrub
from .base import ProviderCall, ProviderError
from .gemini import parse_payload
from .prompts import SYSTEM_PROMPT

logger = get_logger(__name__)

API_ROOT = "https://api.sarvam.ai"
CHAT_PATH = "/v2/chat/completions"

MAX_OUTPUT_TOKENS = 800
DEFAULT_DAILY_TOKEN_BUDGET = 5_000
DEFAULT_MAX_CALLS_PER_RUN = 1

#: How much of an unusable completion is kept for diagnosis. Never the prompt,
#: never the key, never the whole body.
DIAGNOSTIC_EXCERPT_CHARS = 200

UsageLookup = Callable[[], Awaitable[int]]


def diagnostic_excerpt(text: str, limit: int = DIAGNOSTIC_EXCERPT_CHARS) -> str:
    """A short, secret-scrubbed, single-line sample of an unusable response."""
    collapsed = " ".join(str(text or "").split())
    return scrub(collapsed)[:limit]


class SarvamProvider:
    name = Provider.SARVAM

    def __init__(
        self,
        client: SafeHttpClient,
        api_key: str,
        *,
        model: str = "sarvam-105b",
        base_url: str = API_ROOT,
        max_output_tokens: int = MAX_OUTPUT_TOKENS,
        daily_token_budget: int = DEFAULT_DAILY_TOKEN_BUDGET,
        max_calls_per_run: int = DEFAULT_MAX_CALLS_PER_RUN,
        usage_lookup: UsageLookup | None = None,
    ) -> None:
        self._client = client
        self._api_key = api_key.strip()
        self._model = model.strip() or "sarvam-105b"
        self._base = base_url.rstrip("/")
        self._max_output_tokens = max_output_tokens
        self._daily_budget = daily_token_budget
        self._max_calls = max_calls_per_run
        self._usage_lookup = usage_lookup
        self._calls = 0
        #: Serialises "check the budget, then reserve the slot". Without this the
        #: check and the increment straddle an ``await`` and two concurrent
        #: workers can both pass a one-call cap.
        self._lock = asyncio.Lock()

    @property
    def configured(self) -> bool:
        return bool(self._api_key)

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "api-subscription-key": self._api_key,
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    async def resolve_model(self) -> str:
        return self._model

    async def analyze(self, prompt: str) -> ProviderCall:
        model = await self.resolve_model()
        await self._reserve_call()

        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
            # Deliberate, configured ceiling. Forcing a floor (the previous
            # ``max(..., 2000)``) made every call cost 2,000 completion tokens
            # and still returned JSON truncated at the cap.
            "max_tokens": self._max_output_tokens,
            "reasoning_effort": "low",
            "stream": False,
        }

        started = time.monotonic()
        try:
            response = await self._client.post_json(
                f"{self._base}{CHAT_PATH}", payload=body, headers=self._headers
            )
        except FetchError as exc:
            raise ProviderError(f"Sarvam request failed: {exc}", kind="unavailable") from exc

        if response.status == 429:
            raise ProviderError("Sarvam rate limited (429)", kind="rate_limit", retry_after=response.retry_after, status=429)
        if response.status in (401, 403):
            raise ProviderError(f"Sarvam rejected the API key ({response.status})", kind="auth")
        if response.status == 402:
            raise ProviderError("Sarvam credits exhausted (402)", kind="quota")
        if response.status == 400:
            raise ProviderError("Sarvam rejected the request (400)", kind="invalid_response")
        if response.status >= 500:
            raise ProviderError(f"Sarvam upstream error {response.status}", kind="unavailable")
        if response.status != 200 or not isinstance(response.data, dict):
            raise ProviderError(f"Sarvam returned HTTP {response.status}", kind="unavailable")

        data = response.data
        choices = data.get("choices") or []
        if not choices:
            raise ProviderError("Sarvam returned no choices", kind="invalid_response")
        message = choices[0].get("message") or {}
        content = message.get("content")
        rc = message.get("reasoning_content")
        candidates = []
        if isinstance(content, str) and content.strip():
            candidates.append(content)
        if isinstance(rc, str) and rc.strip():
            candidates.append(rc)

        payload = None
        last_err = None
        for cand in candidates:
            try:
                payload = parse_payload(cand, provider="sarvam")
                break
            except ProviderError as exc:
                last_err = exc
        if payload is None:
            # A paid request that produced nothing usable must leave evidence
            # behind: status, finish_reason and a short scrubbed excerpt. The
            # prompt, the key and the full body are never recorded.
            finish_reason = str(choices[0].get("finish_reason") or "unknown")
            excerpt = diagnostic_excerpt(response.text)
            logger.warning(
                "sarvam_unusable_completion",
                http_status=response.status,
                finish_reason=finish_reason,
                candidates=len(candidates),
                excerpt=excerpt,
            )
            detail = f"http_status={response.status} finish_reason={finish_reason}"
            if not candidates:
                raise ProviderError(
                    f"Sarvam returned an empty completion ({detail}); excerpt={excerpt!r}",
                    kind="invalid_response",
                )
            raise ProviderError(
                f"Sarvam JSON was invalid ({detail}, {len(candidates)} candidate(s), "
                f"{last_err}); excerpt={excerpt!r}",
                kind="invalid_response",
            )
        latency_ms = int((time.monotonic() - started) * 1000)

        usage = data.get("usage") or {}
        prompt_tokens = usage.get("prompt_tokens")
        completion_tokens = usage.get("completion_tokens")

        return ProviderCall(
            payload=payload,
            provider=Provider.SARVAM,
            model_id=str(data.get("model") or model),
            latency_ms=latency_ms,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            http_status=response.status,
        )

    async def _reserve_call(self) -> None:
        """Atomically check the budget and consume one call slot.

        The lock is held across the (possibly awaiting) usage lookup, so two
        coroutines can never both observe ``self._calls`` below the cap and then
        both increment it. The reservation is deliberately *not* rolled back on
        an HTTP failure: a request may already have been billed.
        """
        async with self._lock:
            await self._check_budget()
            self._calls += 1

    async def _check_budget(self) -> None:
        if self._calls >= self._max_calls:
            raise ProviderError(
                f"Sarvam per-run cap reached ({self._max_calls} call(s)); deferring to the next provider",
                kind="quota",
            )
        if self._usage_lookup is None:
            return
        try:
            used = int(await self._usage_lookup())
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(f"Sarvam budget lookup failed: {type(exc).__name__}", kind="unavailable") from exc
        if used >= self._daily_budget:
            raise ProviderError(
                f"Sarvam rolling 24h token budget reached ({used}/{self._daily_budget})",
                kind="quota",
            )