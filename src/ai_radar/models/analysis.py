"""Validated shape of an AI analysis payload.

Two rules drive this module:

1. The model may only *suggest*. Category, pricing and priority all come from
   deterministic code plus evidence; the payload is stored as advisory output.
2. A syntactically valid JSON answer is not evidence of factual accuracy, so the
   schema deliberately keeps uncertainty explicit (``pricing_uncertain``,
   ``caveats``) instead of forcing a confident-sounding string.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .core import Category, PricingStatus, Provider

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_WS = re.compile(r"\s+")


def _clean(value: str) -> str:
    return _WS.sub(" ", _CONTROL.sub("", value)).strip()


class AnalysisPayload(BaseModel):
    """Structured output requested from an intelligence provider."""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    summary: str = Field(default="", max_length=400)
    what_is_new: str = Field(default="", max_length=300)
    why_it_matters: str = Field(default="", max_length=300)
    practical_use_case: str = Field(default="", max_length=300)
    category: Category | None = None
    target_users: list[str] = Field(default_factory=list, max_length=6)
    capabilities: list[str] = Field(default_factory=list, max_length=8)
    pricing_status_suggested: PricingStatus = PricingStatus.UNVERIFIED
    license_note: str = Field(default="", max_length=200)
    pricing_uncertain: bool = True
    suggested_priority: int = Field(default=0, ge=0, le=100)
    caveats: str = Field(default="", max_length=300)

    @field_validator(
        "summary",
        "what_is_new",
        "why_it_matters",
        "practical_use_case",
        "license_note",
        "caveats",
        mode="before",
    )
    @classmethod
    def _clean_text(cls, value: Any) -> Any:
        if value is None:
            return ""
        if not isinstance(value, str):
            value = str(value)
        return _clean(value)

    @field_validator("target_users", "capabilities", mode="before")
    @classmethod
    def _clean_list(cls, value: Any) -> Any:
        if value is None:
            return []
        if isinstance(value, str):
            value = [part for part in re.split(r"[;\n]", value)]
        if not isinstance(value, (list, tuple)):
            return []
        cleaned: list[str] = []
        for item in value:
            text = _clean(str(item))[:60]
            if text:
                cleaned.append(text)
        return cleaned[:6]

    @field_validator("pricing_uncertain", mode="before")
    @classmethod
    def _coerce_bool(cls, value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() not in {"false", "no", "0", ""}
        return True

    @field_validator("suggested_priority", mode="before")
    @classmethod
    def _coerce_priority(cls, value: Any) -> int:
        try:
            return max(0, min(100, int(float(value))))
        except (TypeError, ValueError):
            return 0

    @field_validator("category", mode="before")
    @classmethod
    def _coerce_category(cls, value: Any) -> Any:
        if value is None or value == "":
            return None
        text = _clean(str(value)).lower().replace(" ", "_").replace("-", "_")
        aliases = {
            "models": "ai_models",
            "model": "ai_models",
            "coding": "ai_coding_tools",
            "coding_tools": "ai_coding_tools",
            "agents": "ai_agents",
            "image_video": "image_video_ai",
            "audio_speech": "audio_speech_ai",
            "productivity": "productivity_tools",
            "industry": "ai_industry_updates",
        }
        text = aliases.get(text, text)
        return text if any(text == member.value for member in Category) else None

    def usable(self) -> bool:
        """Whether the payload carries enough substance to display."""
        return len(self.summary) >= 20 or len(self.what_is_new) >= 20


class AnalysisResult(BaseModel):
    """A stored analysis row, including provenance needed to audit it later."""

    model_config = ConfigDict(extra="ignore")

    discovery_key: str
    provider: Provider
    model_id: str
    payload: AnalysisPayload
    prompt_version: str
    input_fingerprint: str
    latency_ms: int = 0
    cached: bool = False
    fallback_reason: str | None = None
