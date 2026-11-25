"""free.ai adapter (OpenAI-compatible chat completions).

Verified against <https://free.ai/api/> on 2026-11-24:

* ``POST https://api.free.ai/v1/chat/``
* ``Authorization: Bearer <key>``
* OpenAI-shaped request/response (``choices[0].message.content``)
* one API key for all models; self-hosted ``qwen7b`` is the cheapest
* free plan: **30,000 tokens/day**, 10 requests/minute, ``402`` when exhausted

The daily pool is the binding constraint, not the request rate. One full research
run can easily exceed 30K tokens, so the adapter enforces a *rolling 24-hour*
budget from the ``provider_usage`` table and a per-run call cap. When the budget is
gone it raises ``kind="quota"`` and the router falls through to Gemini, Groq and
finally the deterministic analyser. It never silently spends money: the free.ai
plan is token-only and there is no paid fallback in this codebase.

``free.ai`` reports a single billed-token total, not prompt/completion split, so
``ProviderCall.prompt_tokens`` carries that total and ``completion_tokens`` is 0.
The accounting query sums both columns, so the meaning stays consistent.
"""

from __future__ import annotations

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

API_ROOT = "https://api.free.ai"
CHAT_PATH = "/v1/chat/"
MODELS_PATH = "/v1/models"

MAX_OUTPUT_TOKENS = 800
DEFAULT_DAILY_TOKEN_BUDGET = 25_000
DEFAULT_MAX_CALLS_PER_RUN = 2

UsageLookup = Callable[[], Awaitable[int]]


class FreeAIProvider:
    """Implements :class:`IntelligenceProvider` against free.ai."""

    name = Provider.FREEAI

    def __init__(
        self,
        client: SafeHttpClient,
        api_key: str,
        *,
        model: str = "qwen7b",
        base_url: str = API_ROOT,
        max_output_tokens: int = MAX_OUTPUT_TOKENS,
        daily_token_budget: int = DEFAULT_DAILY_TOKEN_BUDGET,
        max_calls_per_run: int = DEFAULT_MAX_CALLS_PER_RUN,
        usage_lookup: UsageLookup | None = None,
        verify_models: bool = True,
    ) -> None:
        self._client = client
        self._api_key = api_key.strip()
        self._model = model.strip() or "qwen7b"
        self._base = base_url.rstrip("/")
        self._max_output_tokens = max_output_tokens
        self._daily_budget = daily_token_budget
        self._max_calls = max_calls_per_run
        self._usage_lookup = usage_lookup
        self._verify_models = verify_models
        self._calls = 0
        self._verified = False

    @property
    def configured(self) -> bool:
        return bool(self._api_key)

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}

    async def resolve_model(self) -> str:
        """Verify the key against ``/v1/models`` once per process.

        Listing models does not consume tokens, so this is the cheapest way for
        ``doctor`` to distinguish a bad key from an empty quota.
        """
        if not self.configured:
            raise ProviderError("FREEAI_API_KEY is not set", kind="not_configured")
        if self._verified or not self._verify_models:
            # verify_models=False is used by the one-shot G0 verifier: it must make
            # exactly one HTTP request (the chat call), not a discovery call first.
            return self._model

        try:
            response = await self._client.get(f"{self._base}{MODELS_PATH}", headers=self._headers)
        except FetchError as exc:
            kind = "auth" if exc.status in (401, 403) else "unavailable"
            raise ProviderError(f"free.ai model discovery failed: {exc}", kind=kind) from exc

        if response.status in (401, 403):
            raise ProviderError(f"free.ai rejected the API key ({response.status})", kind="auth")
        if response.status != 200:
            raise ProviderError(f"free.ai returned HTTP {response.status} for /v1/models", kind="unavailable")

        self._verified = True
        return self._model

    async def analyze(self, prompt: str) -> ProviderCall:
        model = await self.resolve_model()
        await self._check_budget()
        self._calls += 1

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
            raise ProviderError(f"free.ai request failed: {exc}", kind="unavailable") from exc

        if response.status == 402:
            # Documented: the daily token pool is exhausted.
            raise ProviderError(
                "free.ai daily token pool exhausted (402)", kind="quota"
            )
        if response.status == 429:
            raise ProviderError(
                "free.ai rate limited (429)", kind="rate_limit", retry_after=response.retry_after
            )
        if response.status in (401, 403):
            raise ProviderError(f"free.ai rejected the API key ({response.status})", kind="auth")
        if response.status == 400:
            raise ProviderError("free.ai rejected the request (400)", kind="invalid_response")
        if response.status >= 500:
            raise ProviderError(f"free.ai upstream error {response.status}", kind="unavailable")
        if response.status != 200 or not isinstance(response.data, dict):
            raise ProviderError(f"free.ai returned HTTP {response.status}", kind="unavailable")

        data = response.data
        choices = data.get("choices") or []
        if not choices:
            raise ProviderError("free.ai returned no choices", kind="invalid_response")
        message = choices[0].get("message") or {}
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ProviderError("free.ai returned an empty completion", kind="invalid_response")

        payload: AnalysisPayload = parse_payload(content, provider="free.ai")
        latency_ms = int((time.monotonic() - started) * 1000)

        usage = data.get("free_ai_usage") or {}
        charged = usage.get("tokens_charged") or usage.get("tokens_used")
        try:
            billed = int(charged) if charged is not None else None
        except (TypeError, ValueError):
            billed = None

        return ProviderCall(
            payload=payload,
            provider=Provider.FREEAI,
            model_id=str(data.get("model") or model),
            latency_ms=latency_ms,
            prompt_tokens=billed,
            completion_tokens=0,
            http_status=response.status,
        )

    async def _check_budget(self) -> None:
        if self._calls >= self._max_calls:
            raise ProviderError(
                f"free.ai per-run cap reached ({self._max_calls} call(s)); deferring to the next provider",
                kind="quota",
            )
        if self._usage_lookup is None:
            return
        try:
            used = int(await self._usage_lookup())
        except Exception as exc:  # noqa: BLE001 - failing open could overspend the pool
            raise ProviderError(
                f"free.ai budget lookup failed: {type(exc).__name__}", kind="unavailable"
            ) from exc
        if used >= self._daily_budget:
            raise ProviderError(
                f"free.ai rolling 24h token budget reached ({used}/{self._daily_budget})",
                kind="quota",
            )

