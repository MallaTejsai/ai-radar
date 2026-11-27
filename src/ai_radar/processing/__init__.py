"""Deterministic processing: normalisation, identity, dedupe, pricing, scoring."""

from .canonical import canonical_key, canonical_url, same_site
from .confidence import ConfidenceBreakdown, compute_confidence, explain_confidence
from .dedupe import DedupeResult, dedupe_batch, merge_evidence
from .filter import FilterDecision, FilterOutcome, decide, filter_entries
from .normalize import normalize_batch, normalize_raw
from .pricing import PricingAssessment, classify_pricing, describe
from .scoring import priority_label, score_priority

__all__ = [
    "ConfidenceBreakdown",
    "DedupeResult",
    "FilterDecision",
    "FilterOutcome",
    "PricingAssessment",
    "canonical_key",
    "canonical_url",
    "classify_pricing",
    "compute_confidence",
    "decide",
    "dedupe_batch",
    "describe",
    "explain_confidence",
    "filter_entries",
    "merge_evidence",
    "normalize_batch",
    "normalize_raw",
    "priority_label",
    "same_site",
    "score_priority",
]
