"""One-shot Telegram verification for the integration gate.

Exactly one API request: ``sendMessage``. Deliberately not ``getMe`` + send, and
not ``ai-radar notify --test`` (which sends a different message).

* retries disabled (``SafeHttpClient(retries=1)`` and ``post_json`` never retries);
* no database, research pipeline, scheduler or other provider client;
* the bot token and chat id are never printed;
* errors are built from a URL whose token segment is redacted.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from ai_radar.notifications import TelegramClient, TelegramError  # noqa: E402
from ai_radar.settings import Settings  # noqa: E402
from ai_radar.utils.net import SafeHttpClient  # noqa: E402
from ai_radar.utils.redact import scrub  # noqa: E402

TEXT = "AI Radar integration test — Telegram connection verified."


async def _run(token: str, chat_id: str) -> int:
    recorded: dict[str, object] = {}

    async with SafeHttpClient(retries=1, timeout=30.0) as http:
        original_post = http.post_json

        async def recording_post(*args, **kwargs):
            response = await original_post(*args, **kwargs)
            recorded["status"] = response.status
            recorded["ok"] = bool((response.data or {}).get("ok")) if isinstance(response.data, dict) else False
            return response

        # Instance-level instrumentation only: the production client is unchanged.
        http.post_json = recording_post  # type: ignore[method-assign]
        telegram = TelegramClient(http, token, chat_id)

        try:
            message_id = await telegram.send_message(TEXT)
        except TelegramError as exc:
            status = recorded.get("status", exc.status)
            print("requests: 1")
            print(f"http_status: {status if status is not None else 'not observed'}")
            print(f"telegram_ok: {recorded.get('ok', False)}")
            print(f"delivery: FAILED — {exc.kind}: {scrub(str(exc))}")
            return 1
        except Exception as exc:  # noqa: BLE001
            print("requests: 1")
            print("http_status: not observed")
            print(f"delivery: FAILED — {scrub(f'{type(exc).__name__}: {exc}')}")
            return 1

    print("requests: 1")
    print(f"http_status: {recorded.get('status')}")
    print(f"telegram_ok: {recorded.get('ok')}")
    print(f"message_id: {message_id}")
    print("delivery: SENT")
    return 0


def main() -> int:
    settings = Settings()
    token = settings.telegram_bot_token.strip()
    chat_id = settings.telegram_chat_id.strip()
    print(f"bot_token_detected: {'yes' if token else 'no'}")
    print(f"chat_id_detected: {'yes' if chat_id else 'no'}")

    if not token or not chat_id:
        missing = [
            name
            for name, value in (("TELEGRAM_BOT_TOKEN", token), ("TELEGRAM_CHAT_ID", chat_id))
            if not value
        ]
        print(f"No request made: missing {', '.join(missing)}.", file=sys.stderr)
        return 2

    settings.register_secrets()
    return asyncio.run(_run(token, chat_id))


if __name__ == "__main__":
    raise SystemExit(main())
