"""Verification module for AI Radar V1.0 - Evidence verification pipeline."""

from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import structlog
import httpx

from ...settings import get_settings

logger = structlog.get_logger()


async def verify_discovery(
    discovery: dict,
    source_config: dict,
) -> Tuple[dict, str]:
    """Verify a discovery using evidence checks.

    Returns:
        Tuple of (verified_discovery, verdict)
        verdict: "CONFIRMED", "PARTIAL", "INSUFFICIENT", "DISPROVED"
    """
    source_id = source_config.get("id", "unknown")
    tier = source_config.get("tier", "C")

    verified = dict(discovery)
    verdict = "INSUFFICIENT"

    try:
        # 1. Verify publication date
        date_verdict, date_info = await _verify_date(discovery)

        # 2. Verify product availability / pricing
        pricing_verdict, pricing_info = await _verify_pricing(discovery, source_config)

        # 3. Verify release/model specifics
        spec_verdict, spec_info = await _verify_specifications(discovery, source_config)

        # 4. Check for contradictory evidence
        contradiction_info = await _check_contradictions(discovery, source_config)

        # Assemble verdict
        verdicts = [date_verdict, pricing_verdict, spec_verdict]

        # Count verdict distribution
        confirmed_count = verdicts.count("CONFIRMED")
        partial_count = verdicts.count("PARTIAL")
        insufficient_count = verdicts.count("INSUFFICIENT")

        # Decision logic
        if date_verdict == "CONFIRMED" and pricing_verdict in ["CONFIRMED", "PARTIAL"]:
            if insufficient_count == 0 or (insufficient_count == 1 and partial_count >= 2):
                verdict = "CONFIRMED"
            else:
                verdict = "PARTIAL"
        elif date_verdict == "CONFIRMED" and insufficient_count >= 2:
            verdict = "PARTIAL"
        elif date_verdict == "INSUFFICIENT":
            verdict = "INSUFFICIENT"
        else:
            verdict = "INSUFFICIENT"

        # Build comprehensive verification record
        verified["verification_status"] = verdict
        verified["verified_at"] = datetime.now(timezone.utc).isoformat()
        verified["verification_detail"] = {
            "date": date_info,
            "pricing": pricing_info,
            "specifications": spec_info,
            "contradictions": contradiction_info,
        }

        # Update confidence based on verdict
        if verdict == "CONFIRMED":
            verified["confidence"] = min(verified.get("confidence", 0) + 0.3, 1.0)
        elif verdict == "PARTIAL":
            verified["confidence"] = max(verified.get("confidence", 0) - 0.1, 0.1)
        else:
            verified["confidence"] = max(verified.get("confidence", 0) - 0.2, 0.05)

        logger.info(
            "discovery_verified",
            discovery_id=discovered.get("canonical_key", "unknown"),
            verdict=verdict,
            source_id=source_id,
        )

    except Exception as e:
        logger.error("verification_failed", discovery_id=discovery.get("id", "unknown"), error=str(e))
        verified["verification_status"] = "ERROR"
        verified["verification_detail"] = {"error": str(e)}
        verified["confidence"] = max(verified.get("confidence", 0) - 0.3, 0.0)
        verdict = "ERROR"

    return verified, verdict


async def _verify_date(discovery: dict) -> Tuple[str, dict]:
    """Verify the publication/announcement date of a discovery."""
    published = discovery.get("published", "")
    result = {"status": "INSUFFICIENT", "details": {}}

    if not published:
        return result["status"], result["details"]

    try:
        from dateutil import parser as date_parser
        pub_date = date_parser.parse(published)
        now = datetime.now(timezone.utc)

        # Check if date is reasonable (not in the future, not too old)
        age_days = (now - pub_date.replace(tzinfo=timezone.utc)).days

        if age_days < 0:
            result["status"] = "DISPROVED"
            result["details"]["reason"] "Publication date is in the future"
        elif age_days > 365:
            result["status"] = "INSUFFICIENT"
            result["details"]["reason"] = f"Publication date is {age_days} days old, likely outdated"
        else:
            result["status"] = "CONFIRMED"
            result["details"]["age_days"] = age_days
            result["details"]["publication_date"] = pub_date.isoformat()

    except Exception as e:
        result["status"] = "INSUFFICIENT"
        result["details"]["error"] = str(e)

    return result["status"], result["details"]


async def _verify_pricing(discovery: dict, source_config: dict) -> Tuple[str, dict]:
    """Verify pricing status of a discovery."""
    result = {"status": "INSUFFICIENT", "details": {}}

    # Get pricing from discovery, default to UNVERIFIED
    pricing = discovery.get("pricing_status", "UNVERIFIED")

    # Try to verify by checking the source
    url = discovery.get("url", "")
    if not url:
        return result["status"], result["details"]

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            # Basic head request to check if page is accessible
            response = await client.head(url, follow_redirects=True, timeout=10.0)

            # Look for pricing indicators in the page
            # This is a basic check - real implementation would parse the page
            if response.status_code == 200:
                result["status"] = "CONFIRMED"
                result["details"]["url_accessible"] = True
                result["details"]["http_status"] = response.status_code
            else:
                result["status"] = "PARTIAL"
                result["details"]["url_accessible"] = False
                result["details"]["http_status"] = response.status_code
    except httpx.HTTPError as e:
        result["status"] = "INSUFFICIENT"
        result["details"]["http_error"] = str(e)

    # If we have pricing info in the discovery, honor it
    if pricing != "UNVERIFIED":
        result["status"] = "CONFIRMED" if pricing in ["FREE", "FREE_TIER", "OPEN_SOURCE"] else "PARTIAL"
        result["details"]["declared_pricing"] = pricing

    return result["status"], result["details"]


async def _verify_specifications(
    discovery: dict, source_config: dict
) -> Tuple[str, dict]:
    """Verify model/product specifications."""
    result = {"status": "INSUFFICIENT", "details": {}}

    # Check for key specification fields
    specs_needed = ["model_name", "release_date", "capabilities"]
    found_specs = sum(1 for s in specs_needed if discovery.get(s))

    if found_specs >= 2:
        result["status"] = "CONFIRMED"
        result["details"]["spec_fields_found"] = found_specs
    elif found_specs == 1:
        result["status"] = "PARTIAL"
        result["details"]["spec_fields_found"] = found_specs
    else:
        result["status"] = "INSUFFICIENT"
        result["details"]["spec_fields_found"] = found_specs

    return result["status"], result["details"]


async def _check_contradictions(
    discovery: dict, source_config: dict
) -> List[dict]:
    """Check for contradictory evidence about a discovery."""
    contradictions = []

    # Look for conflicting information
    title = discovery.get("title", "").lower()
    url = discovery.get("url", "").lower()

    # Basic check: if same source has multiple entries with different claims
    # This would be expanded with actual cross-source comparison

    # For now, return empty list - real implementation would compare across sources
    return contradictions