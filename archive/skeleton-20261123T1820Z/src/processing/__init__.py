"""Processing module for AI Radar V1.0 - Normalization, canonicalization, deduplication."""

from __future__ import annotations

import re
import hashlib
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

import structlog

from ...settings import get_settings

logger = structlog.get_logger()


def normalize_title(title: str) -> str:
    """Normalize a title for comparison purposes."""
    if not title:
        return ""

    # Lowercase
    normalized = title.lower()

    # Remove special characters except alphanumeric and spaces
    normalized = re.sub(r'[^\w\s-]', '', normalized)

    # Normalize whitespace
    normalized = re.sub(r'\s+', ' ', normalized).strip()

    # Remove common prefixes/suffixes that vary
    prefixes_to_remove = ['announcing', 'release', 'update', 'v', 'version']
    words = normalized.split()
    filtered_words = [w for w in words if w not in prefixes_to_remove]
    normalized = ' '.join(filtered_words)

    return normalized


def normalize_url(url: str) -> str:
    """Normalize a URL for comparison and dedup."""
    if not url:
        return ""

    # Parse and normalize
    normalized = url.lower()

    # Remove trailing slash
    normalized = normalized.rstrip('/')

    # Remove query parameters for canonical comparison
    if '?' in normalized:
        normalized = normalized.split('?')[0]

    # Remove fragment
    if '#' in normalized:
        normalized = normalized.split('#')[0]

    return normalized


def canonicalize_discovery(entry: dict, source_id: str) -> dict:
    """Canonicalize a discovery entry into a standard format."""
    return {
        "canonical_key": _generate_canonical_key(entry, source_id),
        "title": entry.get("title", ""),
        "normalized_title": normalize_title(entry.get("title", "")),
        "url": entry.get("link", ""),
        "normalized_url": normalize_url(entry.get("link", "")),
        "published": entry.get("published", ""),
        "summary": entry.get("summary", ""),
        "source_id": source_id,
        "discovered_at": datetime.now(timezone.utc).isoformat(),
    }


def _generate_canonical_key(entry: dict, source_id: str) -> str:
    """Generate a deterministic canonical key for deduplication."""
    # Build key from normalized title + URL + source
    title = normalize_title(entry.get("title", ""))
    url = normalize_url(entry.get("link", ""))
    publisher = entry.get("author", source_id).lower()

    # Create hash from components
    key_string = f"{title}|{url}|{publisher}"
    return hashlib.sha256(key_string.encode('utf-8')).hexdigest()[:16]


def deduplicate_discoveries(
    discoveries: List[dict],
    existing_keys: Set[str] = None,
) -> Tuple[List[dict], Set[str]]:
    """Remove duplicate discoveries using deterministic canonical keys.

    Returns:
        Tuple of (deduplicated discoveries, all known keys)
    """
    if existing_keys is None:
        existing_keys = set()

    seen_keys = set(existing_keys)
    deduped = []

    for disc in discoveries:
        key = disc.get("canonical_key", "")
        if key not in seen_keys:
            seen_keys.add(key)
            deduped.append(disc)
        else:
            logger.info("duplicate_discarded", canonical_key=key)

    return deduped, seen_keys


def extract_priority_score(
    discovery: dict,
    category_config: dict,
    weights: dict = None,
) -> int:
    """Calculate a priority score (0-100) for a discovery.

    Uses weighted sum of:
    - Technical significance
    - Personal usefulness
    - Availability/accessibility
    - Novelty
    - Evidence quality
    """
    if weights is None:
        weights = get_settings().get("scoring", {}).get("weights", {
            "technical_significance": 30,
            "personal_usefulness": 25,
            "availability_accessibility": 20,
            "novelty": 15,
            "evidence_quality": 10,
        })

    score = 0

    # Technical significance (30%): based on category and announcement type
    category = discovery.get("category", "")
    if category:
        score += weights.get("technical_significance", 30)

    # Personal usefulness (25%): based on category relevance to user
    if category in ["ai_coding_tools", "ai_models"]:
        score += weights.get("personal_usefulness", 25) * 0.8
    elif category in ["ai_agents", "image_video_ai"]:
        score += weights.get("personal_usefulness", 25) * 0.5
    elif category in ["audio_speech_ai", "productivity_tools"]:
        score += weights.get("personal_usefulness", 25) * 0.3
    elif category == "ai_industry_updates":
        score += weights.get("personal_usefulness", 25) * 0.2

    # Availability/accessibility (20%): free tier, open source, etc.
    pricing = discovery.get("pricing_status", "UNVERIFIED")
    if pricing in ["FREE", "FREE_TIER", "OPEN_SOURCE"]:
        score += weights.get("availability_accessibility", 20) * 0.9
    elif pricing == "PAID":
        score += weights.get("availability_accessibility", 20) * 0.3
    else:
        score += weights.get("availability_accessibility", 20) * 0.5

    # Novelty (15%): based on recency and newsworthiness
    published = discovery.get("published", "")
    if published:
        try:
            from dateutil import parser as date_parser
            pub_date = date_parser.parse(published)
            now = datetime.now(pub_date.tzinfo) if pub_date.tzinfo else datetime.now()
            age_days = (now - pub_date).days
            if age_days < 7:
                score += weights.get("novelty", 15) * 1.0
            elif age_days < 30:
                score += weights.get("novelty", 15) * 0.7
            elif age_days < 90:
                score += weights.get("novelty", 15) * 0.4
            else:
                score += weights.get("novelty", 15) * 0.1
        except Exception:
            score += weights.get("novelty", 15) * 0.3

    # Evidence quality (10%): number of sources, verification status
    evidence_quality = discovery.get("evidence_quality", 0)
    score += weights.get("evidence_quality", 10) * min(evidence_quality / 5.0, 1.0)

    # Cap at 100
    return min(round(score), 100)


def classify_confidence(
    discovery: dict,
    evidence_tiers: dict = None,
) -> str:
    """Classify confidence level for a discovery.

    Returns: "high", "medium", "low", "very_low"
    """
    if evidence_tiers is None:
        evidence_tiers = get_settings().get("scoring", {}).get("evidence_verification", {})

    # Check evidence quality
    evidence_score = discovery.get("evidence_quality", 0)
    verification_status = discovery.get("verification_status", "UNVERIFIED")
    tier = discovery.get("source_tier", "C")

    # High confidence: verified + good evidence
    if verification_status == "VERIFIED" and evidence_score >= 4 and tier in ["A", "B"]:
        return "high"

    # Medium confidence: verified + some evidence, or unverified but good sources
    if verification_status == "VERIFIED" and evidence_score >= 2:
        return "medium"
    if verification_status == "PARTIAL" and evidence_score >= 3:
        return "medium"

    # Low confidence: unverified or minimal evidence
    if evidence_score >= 1 and tier == "B":
        return "low"

    # Very low: no evidence
    if evidence_score == 0:
        return "very_low"

    return "low"