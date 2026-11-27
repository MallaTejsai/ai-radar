"""xAI Grok adapter (OpenAI-compatible chat completions).

Grok is a **paid** provider. This adapter is only constructed when
``XAI_API_KEY`` is set, and it enforces its own rolling-24h token budget and
per-run call cap so it cannot be used accidentally or without limits.

API: ``POST https://api.x.ai/v1/chat/completions``
Auth: ``Authorization: Bearer <key>``
Model discovery: ``GET https://api.x.ai/v1/models``
Usage is reported in the OpenAI shape (``usage.prompt_tokens`` /
``usage.completion_tokens``).
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Awaitable, Callable

from ..models.analysis import AnalysisPayload
from ..models.core import Provider
from ..utils.logging import get_logger
from ..utils.net import FetchError, SafeHttpClient
from .base import ProviderCall, ProviderError
from .gemini import parse_payload
from .prompts import SYSTEM_PROMPT

logger = get_logger(__name__)

API_ROOT = "https://api.x.ai"
CHAT_PATH = "/v1/chat/completions"
MODELS_PATH = "/v1/models"

MAX_OUTPUT_TOKENS = 800
DEFAULT_DAILY_TOKEN_BUDGET = 5_000
DEFAULT_MAX_CALLS_PER_RUN = 1

UsageLookup = Callable[[], Awaitable[int]]

PREFERRED_MODELS: tuple[str, ...] = (
    "grok-4.3",
    "grok-4.20-0309-non-reasoning",
    "grok-4-1-fast-non-reasoning",
    "grok-4-fast-non-reasoning",
    "grok-4.20-0309-reasoning",
)


class GrokProvider:
    name = Provider.GROK

    def __init__(
        self,
        client: SafeHttpClient,
        api_key: str,
        *,
        model: str = "",
        base_url: str = API_ROOT,
        max_output_tokens: int = MAX_OUTPUT_TOKENS,
        daily_token_budget: int = DEFAULT_DAILY_TOKEN_BUDGET,
        max_calls_per_run: int = DEFAULT_MAX_CALLS_PER_RUN,
        usage_lookup: UsageLookup | None = None,
        verify_models: bool = True,
    ) -> None:
        self._client = client
        self._api_key = api_key.strip()
        self._pinned = model.strip()
        self._base = base_url.rstrip("/")
        self._max_output_tokens = max_output_tokens
        self._daily_budget = daily_token_budget
        self._max_calls = max_calls_per_run
        self._usage_lookup = usage_lookup
        self._verify_models = verify_models
        self._calls = 0
        self._resolved: str | None = None
        #: Same check-then-act guard as Sarvam: Grok is paid, so its per-run cap
        #: must be atomic across the concurrent analysis workers.
        self._lock = asyncio.Lock()

    @property
    def configured(self) -> bool:
        return bool(self._api_key)

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}

    async def resolve_model(self) -> str:
        if self._pinned:
            return self._pinned
        if self._resolved:
            return self._resolved
        if not self.configured:
            raise ProviderError("XAI_API_KEY is not set", kind="not_configured")
        if not self._verify_models:
            # Fallback when discovery is skipped; should not normally happen.
            return "grok-4.3"

        try:
            response = await self._client.get(f"{self._base}{MODELS_PATH}", headers=self._headers)
        except FetchError as exc:
            kind = "auth" if exc.status in (401, 403) else "unavailable"
            raise ProviderError(f"Grok model discovery failed: {exc}", kind=kind) from exc

        if response.status in (401, 403):
            raise ProviderError(f"Grok rejected the API key ({response.status})", kind="auth")
        if response.status != 200:
            raise ProviderError(f"Grok returned HTTP {response.status} for /v1/models", kind="unavailable")

        try:
            data = json.loads(response.text)
        except json.JSONDecodeError as exc:
            raise ProviderError("Grok model discovery returned invalid JSON", kind="invalid_response") from exc

        available: list[str] = []
        # OpenAI-shaped: {"data": [{"id": "..."}]} or {"models": [...]}
        items = data.get("data") if isinstance(data, dict) else None
        if not isinstance(items, list):
            items = data.get("models") if isinstance(data, dict) else None
        if isinstance(items, list):
            for m in items:
                if isinstance(m, dict):
                    mid = m.get("id") or m.get("name")
                    if isinstance(mid, str):
                        available.append(mid)
                elif isinstance(m, str):
                    available.append(m)

        # Filter to text/chat models (exclude image/video/voice).
        text_models = [m for m in available if "imagine" not in m and "voice" not in m]

        for candidate in PREFERRED_MODELS:
            if candidate in text_models:
                self._resolved = candidate
                logger.info("grok_model_resolved", model=candidate, available=len(text_models))
                return candidate

        if text_models:
            chosen = sorted(text_models)[0]
            self._resolved = chosen
            logger.warning("grok_model_fell_back_to_first_available", model=chosen)
            return chosen

        raise ProviderError("no chat model available to this key", kind="auth")

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
            "max_tokens": self._max_output_tokens,
            "stream": False,
        }

        started = time.monotonic()
        try:
            response = await self._client.post_json(
                f"{self._base}{CHAT_PATH}", payload=body, headers=self._headers
            )
        except FetchError as exc:
            raise ProviderError(f"Grok request failed: {exc}", kind="unavailable") from exc

        if response.status == 429:
            retry_after = response.retry_after
            # Try body-level retry_after if present
            data = response.data if isinstance(response.data, dict) else {}
            err = data.get("error") if isinstance(data.get("error"), dict) else {}
            if isinstance(err, dict) and isinstance(err.get("retry_after"), (int, float)):
                retry_after = float(err["retry_after"])
            raise ProviderError("Grok rate limited (429)", kind="rate_limit", retry_after=retry_after, status=429)
        if response.status in (401, 403):
            raise ProviderError(f"Grok rejected the API key ({response.status})", kind="auth")
        if response.status == 400:
            raise ProviderError("Grok rejected the request (400)", kind="invalid_response")
        if response.status >= 500:
            raise ProviderError(f"Grok upstream error {response.status}", kind="unavailable")
        if response.status != 200 or not isinstance(response.data, dict):
            raise ProviderError(f"Grok returned HTTP {response.status}", kind="unavailable")

        data = response.data
        choices = data.get("choices") or []
        if not choices:
            raise ProviderError("Grok returned no choices", kind="invalid_response")
        message = choices[0].get("message") or {}
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ProviderError("Grok returned an empty completion", kind="invalid_response")

        payload: AnalysisPayload = parse_payload(content, provider="grok")
        latency_ms = int((time.monotonic() - started) * 1000)

        usage = data.get("usage") or {}
        prompt_tokens = usage.get("prompt_tokens")
        completion_tokens = usage.get("completion_tokens")

        return ProviderCall(
            payload=payload,
            provider=Provider.GROK,
            model_id=str(data.get("model") or model),
            latency_ms=latency_ms,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            http_status=response.status,
        )

    async def _reserve_call(self) -> None:
        """Atomically check the budget and consume one paid call slot."""
        async with self._lock:
            await self._check_budget()
            self._calls += 1

    async def _check_budget(self) -> None:
        if self._calls >= self._max_calls:
            raise ProviderError(
                f"Grok per-run cap reached ({self._max_calls} call(s)); deferring to the next provider",
                kind="quota",
            )
        if self._usage_lookup is None:
            return
        try:
            used = int(await self._usage_lookup())
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(f"Grok budget lookup failed: {type(exc).__name__}", kind="unavailable") from exc
        if used >= self._daily_budget:
            raise ProviderError(
                f"Grok rolling 24h token budget reached ({used}/{self._daily_budget})",
                kind="quota",
            )