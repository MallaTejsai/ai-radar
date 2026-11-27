"""The FILTER stage."""

from __future__ import annotations

from datetime import timedelta

import pytest

from ai_radar.processing.filter import decide, filter_entries
from ai_radar.settings import load_config
from ai_radar.utils.timeutil import utcnow
from support.helpers import make_entry


@pytest.fixture(scope="module")
def signals():
    return load_config().scoring.signals


def test_ordinary_release_passes(signals):
    entry = make_entry(title="Introducing Model X, a new coding model", summary="Long context support.")
    assert decide(entry, signals=signals).keep


def test_single_word_headline_is_rejected(signals):
    entry = make_entry(title="News", summary="Something happened.")
    decision = decide(entry, signals=signals)
    assert not decision.keep
    assert decision.reason in {"headline_has_no_substance", "placeholder_headline"}


def test_placeholder_headline_is_rejected(signals):
    entry = make_entry(title="Read more", summary="Click through for details about the launch.")
    assert not decide(entry, signals=signals).keep


def test_entry_outside_the_backlog_window_is_rejected(signals):
    entry = make_entry(published_at=utcnow() - timedelta(days=200))
    decision = decide(entry, signals=signals, max_age_days=60)
    assert not decision.keep
    assert decision.reason == "older_than_backlog_window"


def test_future_dated_entry_is_rejected(signals):
    entry = make_entry(published_at=utcnow() + timedelta(days=10))
    assert decide(entry, signals=signals).reason == "published_in_the_future"


def test_entry_without_summary_or_signal_is_rejected(signals):
    entry = make_entry(title="Quarterly internal offsite photographs", summary="")
    decision = decide(entry, signals=signals)
    assert not decision.keep
    assert decision.reason == "no_summary_and_no_release_signal"


def test_entry_without_summary_but_with_a_signal_survives(signals):
    entry = make_entry(title="Introducing our new model for code", summary="")
    assert decide(entry, signals=signals).keep


def test_filter_outcome_reports_reason_counts(signals):
    entries = [
        make_entry(url="https://a.com/1", title="Introducing a new model"),
        make_entry(url="https://a.com/2", title="Home"),
        make_entry(url="https://a.com/3", title="x y"),
    ]
    outcome = filter_entries(entries, signals=signals)
    assert len(outcome.kept) == 1
    assert outcome.rejected_count == 2
    assert sum(outcome.reason_counts().values()) == 2
