"""One-shot free.ai verification for the G0 gate.

This is deliberately not the research pipeline:

* exactly **one** HTTP request (the chat call); model discovery is skipped;
* retries disabled (``SafeHttpClient(retries=1)``);
* no router, so no Gemini/Groq/deterministic fallback;
* no database, Telegram, Drive or heartbeat clients are constructed;
* the key is never printed, and errors pass through the redactor.

Exit codes: 0 adapter passed, 1 adapter failed, 2 no key (no request made).
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from ai_radar.intelligence import FreeAIProvider, ProviderError  # noqa: E402
from ai_radar.intelligence.prompts import build_user_prompt  # noqa: E402
from ai_radar.models.core import Category, SourceTier  # noqa: E402
from ai_radar.models.discovery import NormalizedEntry  # noqa: E402
from ai_radar.settings import Settings  # noqa: E402
from ai_radar.utils.net import SafeHttpClient  # noqa: E402
from ai_radar.utils.redact import scrub  # noqa: E402
from ai_radar.utils.timeutil import utcnow  # noqa: E402


def _entry() -> NormalizedEntry:
    return NormalizedEntry(
        source_id="freeai_verification",
        title="Verification: a small model release",
        url="https://example.com/freeai-verification",
        canonical_url="https://example.com/freeai-verification",
        summary=(
            "A small model was released with a free tier for developers. "
            "This is a single verification prompt, not a real discovery."
        ),
        published_at=utcnow(),
        canonical_key="verify:freeai",
        title_fingerprint="verify-freeai",
        category=Category.AI_MODELS,
        tier=SourceTier.A,
    )


async def _run(settings: Settings, key: str, prompt: str) -> int:
    async with SafeHttpClient(retries=1, timeout=45.0) as http:
        provider = FreeAIProvider(
            http,
            key,
            model=settings.freeai_model,
            base_url=settings.freeai_base_url,
            max_output_tokens=settings.freeai_max_output_tokens,
            daily_token_budget=settings.freeai_daily_token_budget,
            max_calls_per_run=1,
            usage_lookup=None,
            # Skip GET /v1/models: the gate allows exactly one request.
            verify_models=False,
        )
        started = time.monotonic()
        try:
            call = await provider.analyze(prompt)
        except ProviderError as exc:
            print("calls: 1")
            print(f"http_status: not recorded ({exc.kind})")
            print(f"adapter: FAIL — {exc.kind}: {scrub(str(exc))}")
            return 1
        except Exception as exc:  # noqa: BLE001
            print("calls: 1")
            print(f"adapter: FAIL — {scrub(f'{type(exc).__name__}: {exc}')}")
            return 1
        elapsed_ms = int((time.monotonic() - started) * 1000)

    payload = call.payload
    summary_present = bool(payload.summary.strip())
    print("calls: 1")
    print(f"http_status: {call.http_status}")
    print("pydantic: PASS")
    print(f"summary_present: {'yes' if summary_present else 'no'}")
    print(f"summary_chars: {len(payload.summary)}")
    print(f"model: {call.model_id}")
    print(f"latency_ms: {call.latency_ms or elapsed_ms}")
    billed = call.prompt_tokens
    print(f"tokens_charged: {billed if billed is not None else 'not returned'}")
    print(f"adapter: {'PASS' if summary_present else 'FAIL — empty summary'}")
    return 0 if summary_present else 1


def main() -> int:
    settings = Settings()
    key = settings.freeai_api_key.strip()
    print(f"key_detected: {'yes' if key else 'no'}")
    if not key:
        print("No request made: FREEAI_API_KEY is not configured.", file=sys.stderr)
        return 2

    settings.register_secrets()
    prompt = build_user_prompt(_entry(), "freeai-verification")
    return asyncio.run(_run(settings, key, prompt))


if __name__ == "__main__":
    raise SystemExit(main())
