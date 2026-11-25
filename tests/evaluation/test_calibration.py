"""The shipped confidence threshold must match a fresh evaluation of the labels."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from evaluate_confidence import evaluate, load_cases, score_cases  # noqa: E402

LABELS = REPO_ROOT / "tests" / "evaluation" / "labels.jsonl"
SHIPPED = REPO_ROOT / "config" / "confidence_thresholds.json"


def test_seed_set_size_and_balance():
    cases = load_cases(LABELS)
    labels = [case["label"] for case in cases]
    assert len(cases) >= 20
    assert labels.count(1) >= 5
    assert labels.count(0) >= 5


def test_threshold_is_derived_and_separates_the_seed_set():
    result = evaluate(score_cases(load_cases(LABELS)))
    assert result["clean_separation"] is True
    assert result["precision"] == 1.0
    assert result["recall"] == 1.0
    assert 0.5 < result["threshold"] < 0.9
    # The midpoint must lie strictly between the two populations.
    assert result["negative_range"][1] < result["threshold"] < result["positive_range"][0]


def test_shipped_calibration_file_matches_a_fresh_run():
    if not SHIPPED.exists():
        pytest.skip("config/confidence_thresholds.json has not been generated")
    shipped = json.loads(SHIPPED.read_text(encoding="utf-8"))
    fresh = evaluate(score_cases(load_cases(LABELS)))
    assert shipped["threshold"] == pytest.approx(fresh["threshold"], abs=0.001)
    assert shipped["n_samples"] == fresh["n_samples"]
    assert "not a statistically validated" in shipped["note"]


def test_calibration_file_is_loaded_by_the_application():
    from ai_radar.settings import load_config, reset_cache

    reset_cache()
    try:
        config = load_config()
        assert config.calibration.calibrated is True
        assert config.confidence_threshold == pytest.approx(config.calibration.threshold)
    finally:
        reset_cache()
