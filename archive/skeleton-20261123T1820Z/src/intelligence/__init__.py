"""Intelligence module for AI Radar V1.0 - Gemini/Groq adapters and analysis."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import structlog

from ...settings import get_settings

logger = structlog.get_logger()


class ProviderAdapter:
    """Adapter for AI provider APIs (Gemini, Groq, fallback)."""

    def __init__(self):
        self.settings = get_settings()
        self.primary_provider = "gemini"
        self.backup_provider = "groq"
        self.fallback_provider = "deterministic"

    async def analyze_discovery(
        self,
        discovery: dict,
        context: dict = None,
    ) -> Tuple[dict, str]:
        """Analyze a discovery using AI provider.

        Returns:
            Tuple of (analysis_result, provider_used)
        """
        # Try providers in order: Gemini -> Groq -> Deterministic fallback
        providers = [
            ("gemini", self._analyze_with_gemini),
            ("groq", self._analyze_with_groq),
            ("deterministic", self._analyze_deterministic),
        ]

        for provider_name, provider_func in providers:
            try:
                result = await provider_func(discovery, context)
                if result[0] is not None:
                    logger.info(
                        "ai_analysis_success",
                        provider=provider_name,
                        discovery_title=discovery.get("title", "unknown")[:50],
                    )
                    return result, provider_name
            except Exception as e:
                logger.warning(
                    "ai_analysis_provider_failed",
                    provider=provider_name,
                    error=str(e),
                )
                continue

        # All providers failed
        return {"error": "All AI providers failed"}, "fallback"

    async def _analyze_with_gemini(
        self, discovery: dict, context: dict = None
    ) -> Tuple[dict, str]:
        """Analyze using Gemini free-tier API."""
        import os

        api_key = os.environ.get("GEMINI_API_KEY", "")
        if not api_key:
            raise ValueError("GEMINI_API_KEY not set")

        # Build analysis prompt
        prompt = self._build_analysis_prompt(discovery)

        try:
            import httpx

            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/"
                    f"gemini-1.5-flash:generateContent?key={api_key}",
                    json={"contents": [{"role": "user", "parts": [{"text": prompt}]}]},
                    headers={"Content-Type": "application/json"},
                )

                if response.status_code != 200:
                    raise ValueError(f"Gemini API error: {response.status_code}")

                data = response.json()

                # Extract response text
                candidates = data.get("candidates", [])
                if not candidates:
                    raise ValueError("No candidates in Gemini response")

                content = candidates[0].get("content", {})
                parts = content.get("parts", [])
                if not parts:
                    raise ValueError("No parts in Gemini response")

                text = parts[0].get("text", "")

                # Parse structured JSON response
                analysis = self._parse_gemini_response(text)

                return analysis, "gemini"

        except Exception as e:
            logger.error("gemini_analysis_failed", error=str(e))
            raise

    async def _analyze_with_groq(
        self, discovery: dict, context: dict = None
    ) -> Tuple[dict, str]:
        """Analyze using Groq free-tier API."""
        import os

        api_key = os.environ.get("GROQ_API_KEY", "")
        if not api_key:
            raise ValueError("GROQ_API_KEY not set")

        # Build analysis prompt
        prompt = self._build_analysis_prompt(discovery)

        try:
            import httpx

            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": "mixtral-8x7b-32768",
                        "messages": [
                            {"role": "user", "content": prompt}
                        ],
                        "temperature": 0.3,
                        "max_tokens": 1000,
                    },
                )

                if response.status_code != 200:
                    raise ValueError(f"Groq API error: {response.status_code}")

                data = response.json()

                choices = data.get("choices", [])
                if not choices:
                    raise ValueError("No choices in Groq response")

                text = choices[0].get("message", {}).get("content", "")

                # Parse structured JSON response
                analysis = self._parse_structured_json(text)

                return analysis, "groq"

        except Exception as e:
            logger.error("groq_analysis_failed", error=str(e))
            raise

    async def _analyze_deterministic(
        self, discovery: dict, context: dict = None
    ) -> Tuple[dict, str]:
        """Analyze using deterministic Python processing (fallback)."""
        analysis = self._deterministic_analysis(discovery)

        return analysis, "deterministic"

    def _build_analysis_prompt(self, discovery: dict) -> str:
        """Build an AI analysis prompt for a discovery."""
        category = discovery.get("category", "ai_industry_updates")
        title = discovery.get("title", "Unnamed discovery")
        published = discovery.get("published", "")

        # Map category to specific analysis focus
        category_prompts = {
            "ai_models": f"""Analyze this AI model announcement: {title}
            Published: {published}
            Provide:
            1. A 2-sentence summary
            2. Target users/practitioners
            3. Key capabilities/features
            4. Pricing status (FREE, FREE_TIER, OPEN_SOURCE, PAID)
            5. License type
            6. Practical use cases
            Return as valid JSON with keys: summary, target_users, capabilities, pricing_status, license, use_cases""",

            "ai_coding_tools": f"""Analyze this AI coding tool announcement: {title}
            Published: {published}
            Provide:
            1. A 2-sentence summary
            2. Target users (developers, teams, etc.)
            3. Key capabilities (code completion, agents, etc.)
            4. Pricing status
            5. License type
            6. Practical use cases
            Return as valid JSON with the same keys.""",

            "ai_agents": f"""Analyze this AI agent announcement: {title}
            Published: {published}
            Provide:
            1. A 2-sentence summary
            2. Target users/automation scenarios
            3. Key capabilities (autonomy, tool use, reasoning)
            4. Pricing status
            5. License type
            6. Practical use cases
            Return as valid JSON with the same keys.""",

            "image_video_ai": f"""Analyze this image/video AI announcement: {title}
            Published: {published}
            Provide:
            1. A 2-sentence summary
            2. Target users (designers, developers, creators)
            3. Key capabilities (text-to-image, video generation, etc.)
            4. Pricing status
            5. License type
            6. Practical use cases
            Return as valid JSON with the same keys.""",

            "audio_speech_ai": f"""Analyze this audio/speech AI announcement: {title}
            Published: {published}
            Provide:
            1. A 2-sentence summary
            2. Target users (developers, creators, businesses)
            3. Key capabilities (TTS, STT, voice cloning, etc.)
            4. Pricing status
            5. License type
            6. Practical use cases
            Return as valid JSON with the same keys.""",

            "productivity_tools": f"""Analyze this productivity tool announcement: {title}
            Published: {published}
            Provide:
            1. A 2-sentence summary
            2. Target users (professionals, teams, etc.)
            3. Key capabilities (automation, integration, etc.)
            4. Pricing status
            5. License type
            6. Practical use cases
            Return as valid JSON with the same keys.""",

            "ai_industry_updates": f"""Analyze this AI industry update: {title}
            Published: {published}
            Provide:
            1. A 2-sentence summary
            2. Key developments and implications
            3. Companies involved
            4. Market impact assessment
            5. Pricing/status info if applicable
            6. Practical implications
            Return as valid JSON with keys: summary, key_developments, companies, market_impact, pricing, implications""",
        }

        base_prompt = category_prompts.get(
            category,
            f"""Analyze this AI announcement: {title}
            Published: {published}
            Provide a summary and key details as valid JSON."""
        )

        return base_prompt

    def _parse_gemini_response(self, text: str) -> dict:
        """Parse Gemini API response into structured dict."""
        try:
            # Try to extract JSON from the response
            start = text.index('{')
            end = text.rindex('}') + 1
            json_str = text[start:end]
            parsed = json.loads(json_str)

            # Validate required keys
            required = ["summary", "target_users", "capabilities", "pricing_status", "license", "use_cases"]
            for key in required:
                if key not in parsed:
                    parsed[key] = f"MISSING: {key}"

            return parsed

        except (ValueError, IndexError) as e:
            logger.warning("gemini_json_parse_failed", error=str(e))
            return {
                "summary": f"Failed to parse Gemini response: {str(e)[:100]}",
                "target_users": "unknown",
                "capabilities": "unknown",
                "pricing_status": "UNVERIFIED",
                "license": "UNKNOWN",
                "use_cases": "unknown",
            }

    def _parse_structured_json(self, text: str) -> dict:
        """Parse structured JSON from any provider."""
        try:
            start = text.index('{')
            end = text.rindex('}') + 1
            json_str = text[start:end]
            return json.loads(json_str)
        except (ValueError, IndexError) as e:
            logger.warning("json_parse_failed", error=str(e))
            return {"summary": text[:200] if text else "Parse failed", "pricing_status": "UNVERIFIED"}

    def _deterministic_analysis(self, discovery: dict) -> dict:
        """Perform deterministic analysis using Python rules."""
        title = discovery.get("title", "").lower()
        category = discovery.get("category", "")
        published = discovery.get("published", "")

        # Basic keyword-based classification
        analysis = {
            "summary": self._generate_summary(title, category),
            "target_users": self._guess_target_users(category, title),
            "capabilities": self._guess_capabilities(category, title),
            "pricing_status": self._classify_pricing(title),
            "license": "UNKNOWN",
            "use_cases": self._guess_use_cases(category, title),
        }

        return analysis

    def _generate_summary(self, title: str, category: str) -> str:
        """Generate a brief summary based on title and category."""
        templates = {
            "ai_models": f"New AI model announced: {title[:100]}",
            "ai_coding_tools": f"AI coding tool update: {title[:100]}",
            "ai_agents": f"AI agent framework: {title[:100]}",
            "image_video_ai": f"Image/video AI advancement: {title[:100]}",
            "audio_speech_ai": f"Speech AI update: {title[:100]}",
            "productivity_tools": f"Productivity tool: {title[:100]}",
            "ai_industry_updates": f"AI industry news: {title[:100]}",
        }
        template = templates.get(category, "AI development: " + title[:100])
        return template

    def _guess_target_users(self, category: str, title: str) -> str:
        """Guess target users based on category."""
        users = {
            "ai_models": "researchers, developers, AI practitioners",
            "ai_coding_tools": "software developers, engineering teams",
            "ai_agents": "automation enthusiasts, developers",
            "image_video_ai": "designers, content creators, developers",
            "audio_speech_ai": "developers, creators, businesses",
            "productivity_tools": "professionals, teams, businesses",
            "ai_industry_updates": "industry stakeholders, investors",
        }
        return users.get(category, "tech community")

    def _guess_capabilities(self, category: str, title: str) -> str:
        """Guess capabilities based on category."""
        caps = {
            "ai_models": "new architecture, improved performance, multimodal capabilities",
            "ai_coding_tools": "code completion, agent assistance, debugging",
            "ai_agents": "autonomous task completion, tool use, planning",
            "image_video_ai": "text-to-image, text-to-video, image editing",
            "audio_speech_ai": "text-to-speech, speech recognition, voice cloning",
            "productivity_tools": "workflow automation, integration, collaboration",
            "ai_industry_updates": "mergers, funding, regulatory changes, market shifts",
        }
        return caps.get(category, "unspecified capabilities")

    def _classify_pricing(self, title: str) -> str:
        """Classify pricing based on title keywords."""
        title_lower = title.lower()

        free_keywords = ["free", "open source", "community", "free tier"]
        paid_keywords = ["pro", "paid", "subscription", "enterprise"]

        if any(kw in title_lower for kw in free_keywords):
            return "FREE"
        elif any(kw in title_lower for kw in paid_keywords):
            return "PAID"
        else:
            return "FREE_TIER"  # Default assumption for new tools

    def _guess_use_cases(self, category: str, title: str) -> str:
        """Guess practical use cases based on category."""
        use_cases = {
            "ai_models": "experimentation, production deployment, research",
            "ai_coding_tools": "faster development, code review, pair programming",
            "ai_agents": "automated workflows, research assistants, code generation",
            "image_video_ai": "content creation, design prototyping, visual assets",
            "audio_speech_ai": "voiceovers, transcription, audio content creation",
            "productivity_tools": "meeting assistance, note-taking, task automation",
            "ai_industry_updates": "market awareness, investment decisions, strategy",
        }
        return use_cases.get(category, "general AI usage")