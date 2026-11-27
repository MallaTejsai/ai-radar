"""Deterministic de-duplication within a batch.

Two keys are used, in this order:

1. ``canonical_key`` - exact artefact identity (same URL, or same upstream release id).
2. ``title_fingerprint`` - the same headline republished on a different URL.

No embeddings, no fuzzy matching: the specification asks for deterministic
deduplication first, and a wrong merge is worse than a missed one because it
attaches evidence to the wrong claim.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..models.discovery import NormalizedEntry


@dataclass(slots=True)
class DedupeResult:
    unique: list[NormalizedEntry] = field(default_factory=list)
    duplicates: list[NormalizedEntry] = field(default_factory=list)
    #: unique entry key -> additional entries considered the same discovery.
    merged: dict[str, list[NormalizedEntry]] = field(default_factory=dict)

    @property
    def removed(self) -> int:
        return len(self.duplicates)


def dedupe_batch(entries: list[NormalizedEntry]) -> DedupeResult:
    """Collapse duplicates inside one batch, preserving first-seen order."""
    result = DedupeResult()
    by_key: dict[str, NormalizedEntry] = {}
    by_fingerprint: dict[str, str] = {}

    for entry in entries:
        existing_key = by_key.get(entry.canonical_key)
        if existing_key is None:
            # Same headline from another source: merge rather than create.
            fingerprint_owner = by_fingerprint.get(entry.title_fingerprint)
            if fingerprint_owner is not None and _same_category(
                by_key[fingerprint_owner], entry
            ):
                result.duplicates.append(entry)
                result.merged.setdefault(fingerprint_owner, []).append(entry)
                continue
            by_key[entry.canonical_key] = entry
            by_fingerprint.setdefault(entry.title_fingerprint, entry.canonical_key)
            result.unique.append(entry)
        else:
            result.duplicates.append(entry)
            result.merged.setdefault(existing_key.canonical_key, []).append(entry)

    return result


def _same_category(left: NormalizedEntry, right: NormalizedEntry) -> bool:
    """Only merge same-category headlines.

    A generic headline such as "Introducing our new model" can legitimately be
    reused by two vendors; merging across categories would attach one vendor's
    evidence to another's claim.
    """
    return left.category == right.category


def merge_evidence(primary: NormalizedEntry, others: list[NormalizedEntry]) -> list[NormalizedEntry]:
    """Return the primary entry followed by distinct corroborating entries."""
    seen: set[str] = set()
    merged: list[NormalizedEntry] = []
    for candidate in [primary, *others]:
        marker = f"{candidate.canonical_key}|{candidate.canonical_url}"
        if marker in seen:
            continue
        seen.add(marker)
        merged.append(candidate)
    return merged
