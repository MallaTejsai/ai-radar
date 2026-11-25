"""Gemini free-tier adapter.

The model id is **not** hard-coded: Google retires ids regularly (``gemini-1.5``
and ``gemini-2.5`` have both been redirected), so the adapter lists the models
available to the configured key and picks the first id from a preference list
that actually exists and supports ``generateContent``. ``GEMINI_MODEL`` pins a
specific id when you want reproducibility.
"""

from __future__ import annotations

import asyncio
import json
import time

from ..models.analysis import AnalysisPayload
from ..models.core import Provider
from ..utils.logging import get_logger
from ..utils.net import FetchError, SafeHttpClient
from .base import ProviderCall, ProviderError
from .prompts import SYSTEM_PROMPT

logger = get_logger(__name__)

API_ROOT = "https://generativelanguage.googleapis.com/v1beta"

#: Preference order. First id present in the account wins.
PREFERRED_MODELS: tuple[str, ...] = (
    "gemini-3.1-flash-lite",
    "gemini-3-flash",
    "gemini-3-flash-lite",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-2.0-flash",
)

MAX_OUTPUT_TOKENS = 800
MODEL_CACHE_SECONDS = 6 * 60 * 60


class GeminiProvider:
    """Implements :class:`IntelligenceProvider` against the Gemini REST API."""

    name = Provider.GEMINI

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

    async def resolve_model(self) -> str:
        if self._pinned:
            return self._pinned
        if self._resolved and (time.monotonic() - self._resolved_at) < MODEL_CACHE_SECONDS:
            return self._resolved
        if not self.configured:
            raise ProviderError("GEMINI_API_KEY is not set", kind="not_configured")

        url = f"{API_ROOT}/models"
        try:
            response = await self._client.get(url, headers={"x-goog-api-key": self._api_key})
        except FetchError as exc:
            raise ProviderError(f"model discovery failed: {exc}", kind="unavailable") from exc

        try:
            data = json.loads(response.text)
        except json.JSONDecodeError as exc:
            raise ProviderError("model discovery returned invalid JSON", kind="invalid_response") from exc

        available: list[str] = []
        for model in data.get("models", []):
            if not isinstance(model, dict):
                continue
            methods = model.get("supportedGenerationMethods") or []
            if "generateContent" not in methods:
                continue
            name = str(model.get("name", ""))
            available.append(name.split("/", 1)[-1])

        for candidate in PREFERRED_MODELS:
            if candidate in available:
                self._resolved = candidate
                self._resolved_at = time.monotonic()
                logger.info("gemini_model_resolved", model=candidate, available=len(available))
                return candidate

        if available:
            fallback = sorted(available)[0]
            self._resolved = fallback
            self._resolved_at = time.monotonic()
            logger.warning(
                "gemini_model_fell_back_to_first_available",
                model=fallback,
                preferred=list(PREFERRED_MODELS),
            )
            return fallback

        raise ProviderError("no generateContent model is available to this key", kind="auth")

    async def analyze(self, prompt: str) -> ProviderCall:
        model = await self.resolve_model()
        url = f"{API_ROOT}/models/{model}:generateContent"
        body = {
            "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.2,
                "maxOutputTokens": self._max_output_tokens,
                "responseMimeType": "application/json",
            },
        }

        started = time.monotonic()
        try:
            response = await self._client.post_json(
                url, payload=body, headers={"x-goog-api-key": self._api_key}
            )
        except FetchError as exc:
            if exc.kind == "timeout":
                raise ProviderError(str(exc), kind="unavailable") from exc
            raise ProviderError(str(exc), kind="unavailable") from exc

        if response.status == 429:
            raise ProviderError(
                "Gemini quota exhausted (429)",
                kind="rate_limit",
                retry_after=response.retry_after,
            )
        if response.status in (401, 403):
            raise ProviderError(f"Gemini rejected the API key ({response.status})", kind="auth")
        if response.status >= 500:
            raise ProviderError(f"Gemini upstream error {response.status}", kind="unavailable")
        if response.status != 200 or not isinstance(response.data, dict):
            raise ProviderError(f"Gemini returned HTTP {response.status}", kind="unavailable")

        candidates = response.data.get("candidates") or []
        if not candidates:
            blocked = (response.data.get("promptFeedback") or {}).get("blockReason")
            raise ProviderError(
                f"Gemini returned no candidates (blockReason={blocked})", kind="invalid_response"
            )
        parts = ((candidates[0].get("content") or {}).get("parts")) or []
        text = "".join(str(part.get("text", "")) for part in parts if isinstance(part, dict))
        if not text.strip():
            raise ProviderError("Gemini returned an empty completion", kind="invalid_response")

        latency_ms = int((time.monotonic() - started) * 1000)
        payload = parse_payload(text, provider="gemini")
        usage = response.data.get("usageMetadata") or {}
        return ProviderCall(
            payload=payload,
            provider=Provider.GEMINI,
            model_id=model,
            latency_ms=latency_ms,
            prompt_tokens=usage.get("promptTokenCount"),
            completion_tokens=usage.get("candidatesTokenCount"),
        )


def parse_payload(text: str, *, provider: str) -> AnalysisPayload:
    """Extract the first JSON object from a completion and validate it."""
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        if "\n" in stripped:
            stripped = stripped.split("\n", 1)[1]
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ProviderError(f"{provider} response contained no JSON object", kind="invalid_response")
    try:
        data = json.loads(stripped[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ProviderError(f"{provider} JSON was invalid: {exc.msg}", kind="invalid_response") from exc
    if not isinstance(data, dict):
        raise ProviderError(f"{provider} JSON was not an object", kind="invalid_response")
    return AnalysisPayload.model_validate(data)


async def probe_key(client: SafeHttpClient, api_key: str) -> tuple[bool, str]:
    """Health check used by ``ai-radar doctor``: does the key list any model?"""
    if not api_key:
        return False, "GEMINI_API_KEY not set"
    provider = GeminiProvider(client, api_key)
    try:
        model = await provider.resolve_model()
    except ProviderError as exc:
        return False, str(exc)
    except asyncio.CancelledError:  # pragma: no cover
        raise
    return True, model
