"""Derive the confidence threshold from the labelled evaluation set.

Usage:
    python scripts/evaluate_confidence.py            # report only
    python scripts/evaluate_confidence.py --write    # also write config/confidence_thresholds.json

Method (deliberately simple and auditable):

1. Score every labelled case with the production confidence function.
2. Place the threshold at the midpoint between the lowest retained case and the
   highest discarded case - the maximum-margin separator.
3. Report precision, recall and the retained/discarded counts at that threshold.

This is a seed set, not a validation study. The output file records its size and
that caveat so nobody can quote the number as more than it is.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from ai_radar.models.core import SourceTier, Verdict  # noqa: E402
from ai_radar.processing.confidence import explain_confidence  # noqa: E402

LABELS_PATH = REPO_ROOT / "tests" / "evaluation" / "labels.jsonl"
OUTPUT_PATH = REPO_ROOT / "config" / "confidence_thresholds.json"
MIN_CASES_FOR_CALIBRATION = 20
MIN_PRECISION = 0.85


def load_cases(path: Path = LABELS_PATH) -> list[dict]:
    cases: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or not line.startswith("{"):
            continue
        cases.append(json.loads(line))
    return cases


def score_cases(cases: list[dict]) -> list[tuple[float, int, dict]]:
    scored = []
    for case in cases:
        breakdown = explain_confidence(
            verdict=Verdict(case["verdict"]),
            tiers={SourceTier(tier) for tier in case["tiers"]},
            distinct_domains=int(case.get("domains", 1)),
            reachable=bool(case.get("reachable", True)),
            date_verified=bool(case.get("date_verified", True)),
        )
        scored.append((breakdown.total, int(case["label"]), case))
    return scored


def evaluate(scored: list[tuple[float, int, dict]]) -> dict:
    positives = [score for score, label, _ in scored if label == 1]
    negatives = [score for score, label, _ in scored if label == 0]
    if not positives or not negatives:
        raise SystemExit("evaluation set needs at least one positive and one negative case")

    clean_separation = min(positives) > max(negatives)
    threshold = round((min(positives) + max(negatives)) / 2, 3)

    retained = [score for score, _, _ in scored if score >= threshold]
    true_positives = sum(1 for score, label, _ in scored if score >= threshold and label == 1)
    false_positives = len(retained) - true_positives
    false_negatives = sum(1 for score, label, _ in scored if score < threshold and label == 1)

    precision = true_positives / (true_positives + false_positives) if retained else 0.0
    recall = true_positives / (true_positives + false_negatives) if positives else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0

    return {
        "threshold": threshold,
        "n_samples": len(scored),
        "positives": len(positives),
        "negatives": len(negatives),
        "precision": round(precision, 3),
        "recall": round(recall, 3),
        "f1": round(f1, 3),
        "false_positives": false_positives,
        "false_negatives": false_negatives,
        "clean_separation": clean_separation,
        "positive_range": [round(min(positives), 3), round(max(positives), 3)],
        "negative_range": [round(min(negatives), 3), round(max(negatives), 3)],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate the confidence threshold")
    parser.add_argument("--write", action="store_true", help="write config/confidence_thresholds.json")
    parser.add_argument("--labels", default=str(LABELS_PATH))
    args = parser.parse_args(argv)

    cases = load_cases(Path(args.labels))
    result = evaluate(score_cases(cases))

    print(json.dumps(result, indent=2))
    print()
    if result["clean_separation"]:
        print("The seed set separates cleanly; the threshold is the midpoint of the gap.")
    else:
        print(
            "WARNING: positive and negative scores overlap, so no threshold is clean. "
            "Add labelled cases before trusting this number."
        )
    print(
        f"NOTE: n={result['n_samples']} hand-labelled seed cases. "
        "This is not a validation study."
    )

    eligible = (
        result["n_samples"] >= MIN_CASES_FOR_CALIBRATION
        and result["clean_separation"]
        and result["precision"] >= MIN_PRECISION
    )

    if args.write:
        payload = {
            "calibrated": eligible,
            "threshold": result["threshold"],
            "n_samples": result["n_samples"],
            "precision": result["precision"],
            "recall": result["recall"],
            "f1": result["f1"],
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "dataset": "tests/evaluation/labels.jsonl",
            "note": (
                "Threshold derived from a hand-labelled seed set via maximum-margin "
                "separation; not a statistically validated calibration."
            ),
        }
        OUTPUT_PATH.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {OUTPUT_PATH.relative_to(REPO_ROOT)} (calibrated={eligible})")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
