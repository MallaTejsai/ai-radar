"""Replaceable intelligence providers (free.ai -> Gemini -> Groq -> deterministic)."""

from .base import IntelligenceProvider, ProviderCall, ProviderError
from .deterministic import DeterministicAnalyzer
from .freeai import FreeAIProvider
from .gemini import GeminiProvider
from .groq import GroqProvider
from .grok import GrokProvider
from .sarvam import SarvamProvider
from .prompts import PROMPT_VERSION, build_user_prompt, prompt_fingerprint, sanitize_untrusted
from .router import AnalysisCache, EngineStats, IntelligenceEngine, MetricsSink, build_engine

__all__ = [
    "AnalysisCache",
    "DeterministicAnalyzer",
    "EngineStats",
    "FreeAIProvider",
    "GeminiProvider",
    "GroqProvider",
    "GrokProvider",
    "SarvamProvider",
    "IntelligenceEngine",
    "IntelligenceProvider",
    "MetricsSink",
    "PROMPT_VERSION",
    "ProviderCall",
    "ProviderError",
    "build_engine",
    "build_user_prompt",
    "prompt_fingerprint",
    "sanitize_untrusted",
]
