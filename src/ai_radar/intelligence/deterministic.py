"""Deterministic fallback analyser.

This is not a degraded stub: it is the guaranteed floor of the system. When both
free-tier providers are unavailable the pipeline still produces a structured,
validated analysis from the collected text alone, with no invented facts and an
explicit caveat that a model was not used.

It deliberately does not produce a priority score. Priority is computed by
``processing.scoring`` from evidence, and a fallback must not fake confidence.
"""

from __future__ import annotations

from ..models.analysis import AnalysisPayload
from ..models.core import Category, Provider
from ..models.discovery import NormalizedEntry
from ..processing.pricing import classify_pricing
from ..settings import SignalsConfig
from ..utils.text import first_paragraph, truncate

TARGET_USERS: dict[Category, list[str]] = {
    Category.AI_MODELS: ["AI engineers", "ML researchers", "platform teams"],
    Category.AI_CODING_TOOLS: ["software developers", "engineering teams"],
    Category.AI_AGENTS: ["automation engineers", "agent developers"],
    Category.IMAGE_VIDEO_AI: ["designers", "content creators", "media engineers"],
    Category.AUDIO_SPEECH_AI: ["voice/audio engineers", "content creators"],
    Category.PRODUCTIVITY_TOOLS: ["knowledge workers", "operations teams"],
    Category.AI_INDUSTRY_UPDATES: ["product leaders", "investors", "analysts"],
}

WHY_IT_MATTERS: dict[Category, str] = {
    Category.AI_MODELS: "A new model can change which capability is affordable or practical to build on.",
    Category.AI_CODING_TOOLS: "Tooling changes shift day-to-day development speed and review habits.",
    Category.AI_AGENTS: "Agent runtime changes affect how much automation can be trusted end to end.",
    Category.IMAGE_VIDEO_AI: "New generation quality changes what creative work can be automated.",
    Category.AUDIO_SPEECH_AI: "Speech quality and latency changes affect production media workflows.",
    Category.PRODUCTIVITY_TOOLS: "Workflow features decide whether a task still needs manual handling.",
    Category.AI_INDUSTRY_UPDATES: "Market and policy shifts change which tools stay viable or compliant.",
}

USE_CASE: dict[Category, str] = {
    Category.AI_MODELS: "Prototype one real task on the new model and compare cost and quality against the current baseline.",
    Category.AI_CODING_TOOLS: "Trial it on a small, low-risk repository change before adopting it for team work.",
    Category.AI_AGENTS: "Wrap one recurring internal task as an agent workflow and measure how often it needs a human.",
    Category.IMAGE_VIDEO_AI: "Generate a small asset batch for an existing project and review it for editability.",
    Category.AUDIO_SPEECH_AI: "Produce a short demo clip and check pronunciation, latency and licensing.",
    Category.PRODUCTIVITY_TOOLS: "Automate one repetitive weekly task and measure the time saved.",
    Category.AI_INDUSTRY_UPDATES: "Check whether the change affects tools already in use before acting on it.",
}

_TEXT_PREFIXES = (
    "introducing",
    "announcing",
    "launching",
    "released",
    "release:",
    "now available:",
)


class DeterministicAnalyzer:
    """Rule-based analysis used when no model provider is reachable."""

    name = Provider.DETERMINISTIC
    model_id = "rules-v1"

    def __init__(self, signals: SignalsConfig) -> None:
        self._signals = signals

    async def resolve_model(self) -> str:
        return self.model_id

    def analyze_entry(self, entry: NormalizedEntry) -> AnalysisPayload:
        text = f"{entry.title} {entry.summary}".lower()
        capabilities = [
            term for term in self._signals.capability if term in text
        ][:5]
        pricing = classify_pricing(f"{entry.title} {entry.summary}", self._signals)

        headline = entry.title.strip()
        lowered = headline.lower()
        for prefix in _TEXT_PREFIXES:
            if lowered.startswith(prefix):
                headline = headline[len(prefix) :].lstrip(" :-–")
                break

        body = first_paragraph(entry.summary, limit=320)
        summary = body or truncate(headline, 220)
        if len(summary) < 40:
            summary = truncate(f"{headline}. {body}".strip(), 320)

        caveat = "Deterministic fallback: generated from the collected text without a language model."
        if not body:
            caveat += " No body text was available, so the summary is headline-only."

        return AnalysisPayload(
            summary=truncate(summary, 400),
            what_is_new=truncate(headline, 300),
            why_it_matters=WHY_IT_MATTERS[entry.category],
            practical_use_case=USE_CASE[entry.category],
            category=entry.category,
            target_users=TARGET_USERS[entry.category],
            capabilities=[cap.title() for cap in capabilities],
            pricing_status_suggested=pricing.status,
            license_note=pricing.reason if pricing.status.value != "UNVERIFIED" else "",
            pricing_uncertain=pricing.status.value == "UNVERIFIED",
            suggested_priority=0,
            caveats=caveat,
        )
