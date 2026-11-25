"""Provider routing: Gemini -> Groq -> deterministic, with cache and accounting.

Behaviour that matters operationally:

* A provider is tried at most once per discovery. Repeatedly hammering a 429 is
  how a free tier becomes useless for the rest of the day.
* After an ``auth`` or ``rate_limit`` failure a provider is disabled for the rest
  of the run (in-process circuit breaker); after two ``unavailable`` failures it
  is skipped too. The reason is attached to every analysis it was skipped for.
* Deterministic analysis is always available, so the pipeline cannot be blocked by
  an AI outage - it degrades and says so.
* Every attempt is reported to the metrics sink, including failures.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Protocol

from ..models.analysis import AnalysisResult
from ..models.core import Provider
from ..models.discovery import NormalizedEntry
from ..settings import Settings, SignalsConfig
from ..utils.logging import get_logger
from ..utils.net import SafeHttpClient
from ..utils.redact import GLOBAL_REDACTOR
from .base import IntelligenceProvider, ProviderCall, ProviderError
from .deterministic import DeterministicAnalyzer
from .freeai import FreeAIProvider
from .gemini import GeminiProvider
from .groq import GroqProvider
from .grok import GrokProvider
from .sarvam import SarvamProvider
from .prompts import PROMPT_VERSION, build_user_prompt, prompt_fingerprint

logger = get_logger(__name__)

UNAVAILABLE_FAILURES_BEFORE_OPEN = 2


class AnalysisCache(Protocol):
    """Cache contract, deliberately matching :class:`Repository` method names."""

    async def cache_get(self, fingerprint: str) -> AnalysisResult | None: ...

    async def cache_put(self, fingerprint: str, result: AnalysisResult) -> None: ...


class MetricsSink(Protocol):
    async def record_provider_call(
        self,
        *,
        provider: str,
        model_id: str,
        ok: bool,
        error_kind: str | None,
        latency_ms: int,
        prompt_tokens: int | None,
        completion_tokens: int | None,
    ) -> None: ...


@dataclass(slots=True)
class EngineStats:
    calls: int = 0
    cache_hits: int = 0
    by_provider: dict[str, int] = field(default_factory=dict)
    errors: dict[str, int] = field(default_factory=dict)
    disabled: dict[str, str] = field(default_factory=dict)

    def record_success(self, provider: str) -> None:
        self.calls += 1
        self.by_provider[provider] = self.by_provider.get(provider, 0) + 1

    def record_error(self, provider: str, kind: str) -> None:
        self.errors[kind] = self.errors.get(kind, 0) + 1
        logger.warning("provider_call_failed", provider=provider, kind=kind)


class IntelligenceEngine:
    """Routes one discovery through the mandated provider order."""

    def __init__(
        self,
        *,
        settings: Settings,
        signals: SignalsConfig,
        client: SafeHttpClient,
        cache: AnalysisCache | None = None,
        metrics: MetricsSink | None = None,
        providers: list[IntelligenceProvider] | None = None,
        freeai_usage_lookup: Callable[[], Awaitable[int]] | None = None,
        xai_usage_lookup: Callable[[], Awaitable[int]] | None = None,
        sarvam_usage_lookup: Callable[[], Awaitable[int]] | None = None,
    ) -> None:
        self._settings = settings
        self._client = client
        self._cache = cache
        self._metrics = metrics
        self._fallback = DeterministicAnalyzer(signals)
        self._xai_usage_lookup = xai_usage_lookup
        self._sarvam_usage_lookup = sarvam_usage_lookup
        self.stats = EngineStats()

        if providers is not None:
            self._providers = providers
        else:
            chain: list[IntelligenceProvider] = []
            if settings.freeai_api_key:
                chain.append(
                    FreeAIProvider(
                        client,
                        settings.freeai_api_key,
                        model=settings.freeai_model,
                        base_url=settings.freeai_base_url,
                        max_output_tokens=settings.freeai_max_output_tokens,
                        daily_token_budget=settings.freeai_daily_token_budget,
                        max_calls_per_run=settings.freeai_max_calls_per_run,
                        usage_lookup=freeai_usage_lookup,
                    )
                )
            if settings.gemini_api_key:
                chain.append(
                    GeminiProvider(client, settings.gemini_api_key, pinned_model=settings.gemini_model)
                )
            if settings.groq_api_key:
                chain.append(
                    GroqProvider(client, settings.groq_api_key, pinned_model=settings.groq_model)
                )
            if settings.xai_api_key:
                chain.append(
                    GrokProvider(
                        client,
                        settings.xai_api_key,
                        model=settings.xai_model,
                        base_url=settings.xai_base_url,
                        max_output_tokens=settings.xai_max_output_tokens,
                        daily_token_budget=settings.xai_daily_token_budget,
                        max_calls_per_run=settings.xai_max_calls_per_run,
                        usage_lookup=xai_usage_lookup,
                    )
                )
            if settings.sarvam_api_key:
                chain.append(
                    SarvamProvider(
                        client,
                        settings.sarvam_api_key,
                        model=settings.sarvam_model,
                        base_url=settings.sarvam_base_url,
                        max_output_tokens=settings.sarvam_max_output_tokens,
                        daily_token_budget=settings.sarvam_daily_token_budget,
                        max_calls_per_run=settings.sarvam_max_calls_per_run,
                        usage_lookup=sarvam_usage_lookup,
                    )
                )
            self._providers = chain

        self._disabled: dict[str, str] = {}
        self._failures: dict[str, int] = {}

    async def analyze(self, entry: NormalizedEntry, source_name: str = "") -> AnalysisResult:
        fingerprint = prompt_fingerprint(entry)

        if self._cache is not None:
            cached = await self._cache.cache_get(fingerprint)
            if cached is not None:
                self.stats.cache_hits += 1
                return cached.model_copy(update={"cached": True})

        prompt = GLOBAL_REDACTOR.scrub_text(build_user_prompt(entry, source_name))
        last_error: ProviderError | None = None

        for provider in self._providers:
            name = provider.name.value
            if name in self._disabled:
                continue
            started = time.monotonic()
            try:
                call = await provider.analyze(prompt)
            except ProviderError as exc:
                last_error = exc
                self.stats.record_error(name, exc.kind)
                if self._metrics is not None:
                    await self._metrics.record_provider_call(
                        provider=name,
                        model_id="",
                        ok=False,
                        error_kind=exc.kind,
                        latency_ms=int((time.monotonic() - started) * 1000),
                        prompt_tokens=None,
                        completion_tokens=None,
                    )
                self._handle_failure(name, exc)
                continue
            except Exception as exc:  # noqa: BLE001 - never let a provider break the run
                last_error = ProviderError(f"{type(exc).__name__}: {exc}", kind="unavailable")
                self.stats.record_error(name, "unexpected")
                self._handle_failure(name, last_error)
                continue

            self.stats.record_success(name)
            self._failures[name] = 0
            if self._metrics is not None:
                await self._metrics.record_provider_call(
                    provider=name,
                    model_id=call.model_id,
                    ok=True,
                    error_kind=None,
                    latency_ms=call.latency_ms,
                    prompt_tokens=call.prompt_tokens,
                    completion_tokens=call.completion_tokens,
                )
            result = self._to_result(call, prompt, fingerprint)
            await self._store(fingerprint, result)
            return result

        # Deterministic floor.
        payload = self._fallback.analyze_entry(entry)
        result = AnalysisResult(
            discovery_key=entry.canonical_key,
            provider=Provider.DETERMINISTIC,
            model_id=self._fallback.model_id,
            payload=payload,
            prompt_version=PROMPT_VERSION,
            input_fingerprint=fingerprint,
            latency_ms=0,
            cached=False,
            fallback_reason=(
                f"{last_error.kind}: {last_error}" if last_error else "no provider configured"
            ),
        )
        self.stats.record_success(Provider.DETERMINISTIC.value)
        if self._metrics is not None:
            await self._metrics.record_provider_call(
                provider=Provider.DETERMINISTIC.value,
                model_id=self._fallback.model_id,
                ok=True,
                error_kind=None,
                latency_ms=0,
                prompt_tokens=None,
                completion_tokens=None,
            )
        await self._store(fingerprint, result)
        logger.warning(
            "analysis_fell_back_to_deterministic",
            reason=result.fallback_reason,
            discovery=entry.canonical_key,
        )
        return result

    def _handle_failure(self, provider: str, error: ProviderError) -> None:
        if error.kind in {"auth", "not_configured", "rate_limit", "quota"}:
            self._disabled[provider] = error.kind
            self.stats.disabled[provider] = error.kind
            logger.info("provider_disabled_for_run", provider=provider, kind=error.kind)
            return
        self._failures[provider] = self._failures.get(provider, 0) + 1
        if self._failures[provider] >= UNAVAILABLE_FAILURES_BEFORE_OPEN:
            self._disabled[provider] = "unavailable"
            self.stats.disabled[provider] = "unavailable"
            logger.info("provider_disabled_for_run", provider=provider, kind="unavailable")

    def _to_result(self, call: ProviderCall, prompt: str, fingerprint: str) -> AnalysisResult:
        payload = call.payload
        if not payload.usable():
            payload = payload.model_copy(
                update={
                    "caveats": (
                        payload.caveats
                        or "Provider returned a very short analysis; treat the summary as incomplete."
                    )[:300]
                }
            )
        return AnalysisResult(
            discovery_key=fingerprint,
            provider=call.provider,
            model_id=call.model_id,
            payload=payload,
            prompt_version=PROMPT_VERSION,
            input_fingerprint=fingerprint,
            latency_ms=call.latency_ms,
        )

    async def _store(self, fingerprint: str, result: AnalysisResult) -> None:
        if self._cache is None:
            return
        try:
            await self._cache.cache_put(fingerprint, result)
        except Exception as exc:  # noqa: BLE001 - cache failure must not lose the analysis
            logger.warning("analysis_cache_write_failed", error=str(exc))


def build_engine(
    settings: Settings,
    signals: SignalsConfig,
    client: SafeHttpClient,
    *,
    cache: AnalysisCache | None = None,
    metrics: MetricsSink | None = None,
    freeai_usage_lookup: Callable[[], Awaitable[int]] | None = None,
    xai_usage_lookup: Callable[[], Awaitable[int]] | None = None,
    sarvam_usage_lookup: Callable[[], Awaitable[int]] | None = None,
) -> IntelligenceEngine:
    return IntelligenceEngine(
        settings=settings,
        signals=signals,
        client=client,
        cache=cache,
        metrics=metrics,
        freeai_usage_lookup=freeai_usage_lookup,
        xai_usage_lookup=xai_usage_lookup,
        sarvam_usage_lookup=sarvam_usage_lookup,
    )
