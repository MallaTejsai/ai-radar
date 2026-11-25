"""The FILTER stage: cheap, deterministic relevance gating.

This runs before verification and before any paid-for-intelligence call, so it is
the place to reject volume that cannot become a useful discovery:

* headlines with no substance (a single word, or a known placeholder)
* entries older than the backlog window
* entries with neither a summary nor a release signal to analyse

Nothing is dropped silently: every rejection carries a reason string that is
counted in the run report.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from ..models.discovery import NormalizedEntry
from ..settings import SignalsConfig
from ..utils.text import normalize_title
from ..utils.timeutil import age_days, utcnow

#: Placeholder or navigation headlines that appear in feeds.
_BLOCKED_TITLES = frozenset(
    {
        "untitled",
        "home",
        "blog",
        "news",
        "read more",
        "learn more",
        "index",
        "archive",
        "changelog",
    }
)


@dataclass(frozen=True, slots=True)
class FilterDecision:
    keep: bool
    reason: str


@dataclass(slots=True)
class FilterOutcome:
    kept: list[NormalizedEntry] = field(default_factory=list)
    rejected: list[tuple[NormalizedEntry, str]] = field(default_factory=list)

    @property
    def rejected_count(self) -> int:
        return len(self.rejected)

    def reason_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for _, reason in self.rejected:
            counts[reason] = counts.get(reason, 0) + 1
        return counts


def decide(
    entry: NormalizedEntry,
    *,
    signals: SignalsConfig,
    max_age_days: int = 60,
    now: datetime | None = None,
    allow_undated: bool = True,
) -> FilterDecision:
    """Decide whether one normalised entry is worth verifying."""
    now = now or utcnow()
    normalised = normalize_title(entry.title)

    if not normalised or len(normalised) < 6:
        return FilterDecision(False, "headline_has_no_substance")
    if entry.title.strip().lower() in _BLOCKED_TITLES:
        return FilterDecision(False, "placeholder_headline")

    age = age_days(entry.published_at, reference=now)
    if age is None:
        if not allow_undated:
            return FilterDecision(False, "undated_entry")
    elif age > max_age_days:
        return FilterDecision(False, "older_than_backlog_window")
    elif age < -2:
        return FilterDecision(False, "published_in_the_future")

    haystack = f"{entry.title} {entry.summary}".lower()
    has_signal = any(
        term in haystack
        for term in (
            *signals.major_release,
            *signals.capability,
            *signals.free_signal,
            *signals.open_source_signal,
            *signals.paid_signal,
        )
    )
    if not entry.summary.strip() and not has_signal:
        return FilterDecision(False, "no_summary_and_no_release_signal")

    return FilterDecision(True, "relevant")


def filter_entries(
    entries: list[NormalizedEntry],
    *,
    signals: SignalsConfig,
    max_age_days: int = 60,
    now: datetime | None = None,
) -> FilterOutcome:
    outcome = FilterOutcome()
    for entry in entries:
        decision = decide(entry, signals=signals, max_age_days=max_age_days, now=now)
        if decision.keep:
            outcome.kept.append(entry)
        else:
            outcome.rejected.append((entry, decision.reason))
    return outcome
