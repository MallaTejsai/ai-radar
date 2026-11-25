"""Groq free-tier adapter (OpenAI-compatible chat completions).

Like the Gemini adapter, the model id is resolved from ``/openai/v1/models`` at
runtime. Groq's catalogue changes frequently - ``mixtral-8x7b-32768``, which the
original skeleton hard-coded, has been retired - so guessing an id is a defect.
"""

from __future__ import annotations

import json
import time

from ..models.core import Provider
from ..utils.logging import get_logger
from ..utils.net import FetchError, SafeHttpClient
from .base import ProviderCall, ProviderError
from .gemini import parse_payload
from .prompts import SYSTEM_PROMPT

logger = get_logger(__name__)

API_ROOT = "https://api.groq.com/openai/v1"

#: Preference order. First id present in the account wins.
PREFERRED_MODELS: tuple[str, ...] = (
    "openai/gpt-oss-120b",
    "openai/gpt-oss-20b",
    "qwen/qwen3.8-27b",
    "llama-3.3-70b-versatile",
    "llama-3.1-8b-instant",
)

MAX_OUTPUT_TOKENS = 800
MODEL_CACHE_SECONDS = 6 * 60 * 60


class GroqProvider:
    """Implements :class:`IntelligenceProvider` against Groq's OpenAI-compatible API."""

    name = Provider.GROQ

    def __init__(
        self,
        client: SafeHttpClient,
        api_key: str,
        *,
        pinned_model: str = "",
        max_output_tokens: int = MAX_OUTPUT_TOKENS,
    ) -> None:
        self._client = client
        self._api_key = api_key
        self._pinned = pinned_model.strip()
        self._max_output_tokens = max_output_tokens
        self._resolved: str | None = None
        self._resolved_at: float = 0.0

    @property
    def configured(self) -> bool:
        return bool(self._api_key)

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}"}

    async def resolve_model(self) -> str:
        if self._pinned:
            return self._pinned
        if self._resolved and (time.monotonic() - self._resolved_at) < MODEL_CACHE_SECONDS:
            return self._resolved
        if not self.configured:
            raise ProviderError("GROQ_API_KEY is not set", kind="not_configured")

        try:
            response = await self._client.get(f"{API_ROOT}/models", headers=self._headers)
        except FetchError as exc:
            raise ProviderError(f"model discovery failed: {exc}", kind="unavailable") from exc

        if response.status in (401, 403):
            raise ProviderError(f"Groq rejected the API key ({response.status})", kind="auth")
        try:
            data = json.loads(response.text)
        except json.JSONDecodeError as exc:
            raise ProviderError("model discovery returned invalid JSON", kind="invalid_response") from exc

        available = [
            str(model.get("id"))
            for model in data.get("data", [])
            if isinstance(model, dict) and model.get("id")
        ]
        for candidate in PREFERRED_MODELS:
            if candidate in available:
                self._resolved = candidate
                self._resolved_at = time.monotonic()
                logger.info("groq_model_resolved", model=candidate, available=len(available))
                return candidate

        # Exclude speech/guard models from the blind fallback.
        chat_like = [
            model
            for model in available
            if not any(token in model for token in ("whisper", "guard", "tts", "orpheus"))
        ]
        if chat_like:
            fallback = sorted(chat_like)[0]
            self._resolved = fallback
            self._resolved_at = time.monotonic()
            logger.warning(
                "groq_model_fell_back_to_first_available",
                model=fallback,
                preferred=list(PREFERRED_MODELS),
            )
            return fallback

        raise ProviderError("no chat model is available to this key", kind="auth")

    async def analyze(self, prompt: str) -> ProviderCall:
        model = await self.resolve_model()
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
            "max_tokens": self._max_output_tokens,
            "response_format": {"type": "json_object"},
        }

        started = time.monotonic()
        try:
            response = await self._client.post_json(
                f"{API_ROOT}/chat/completions", payload=body, headers=self._headers
            )
        except FetchError as exc:
            raise ProviderError(str(exc), kind="unavailable") from exc

        if response.status == 429:
            raise ProviderError(
                "Groq quota exhausted (429)", kind="rate_limit", retry_after=response.retry_after
            )
        if response.status in (401, 403):
            raise ProviderError(f"Groq rejected the API key ({response.status})", kind="auth")
        if response.status >= 500:
            raise ProviderError(f"Groq upstream error {response.status}", kind="unavailable")
        if response.status != 200 or not isinstance(response.data, dict):
            raise ProviderError(f"Groq returned HTTP {response.status}", kind="unavailable")

        choices = response.data.get("choices") or []
        if not choices:
            raise ProviderError("Groq returned no choices", kind="invalid_response")
        content = ((choices[0].get("message") or {}).get("content")) or ""
        if not str(content).strip():
            raise ProviderError("Groq returned an empty completion", kind="invalid_response")

        latency_ms = int((time.monotonic() - started) * 1000)
        payload = parse_payload(str(content), provider="groq")
        usage = response.data.get("usage") or {}
        return ProviderCall(
            payload=payload,
            provider=Provider.GROQ,
            model_id=model,
            latency_ms=latency_ms,
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
        )
