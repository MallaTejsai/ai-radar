"""Secret redaction for logs and error messages.

The pipeline handles a GitHub token, a Telegram bot token, database credentials
and OAuth refresh tokens. None of them may ever be written to a log, an error
string or a database column, so scrubbing happens centrally rather than by
review discipline.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

MASK = "***REDACTED***"

_PATTERNS: tuple[re.Pattern[str], ...] = (
    # postgres://user:password@host/db  and  postgresql+psycopg://...
    re.compile(r"(?i)\b(postgres(?:ql)?(?:\+\w+)?://)([^:/@\s]+):([^@\s]+)@"),
    # Telegram bot token inside a URL path
    re.compile(r"(?i)(/bot)\d{6,}:[A-Za-z0-9_-]{20,}"),
    # Authorization / x-api-key style headers
    re.compile(r"(?i)\b(bearer|token|api[_-]?key|apikey)\b(\s*[:=]\s*)([A-Za-z0-9._~+/=-]{8,})"),
    # Bare "Bearer <credential>" prose (no separator)
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]{8,}"),
    # key=... query parameters
    re.compile(r"(?i)([?&](?:key|api_key|apikey|token|access_token|refresh_token)=)([^&\s]+)"),
    # Well-known provider token shapes
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bya29\.[A-Za-z0-9._-]{20,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{20,}\b"),
    re.compile(r"\bgsk_[A-Za-z0-9]{20,}\b"),
)


class Redactor:
    """Redacts known literal secrets plus anything matching a secret-shaped pattern."""

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        self._secrets: set[str] = set()
        for secret in secrets:
            self.add(secret)

    def add(self, secret: str | None) -> None:
        """Register a literal value to scrub. Very short values are ignored."""
        if not secret or not isinstance(secret, str):
            return
        if len(secret) < 8:
            return
        self._secrets.add(secret)
        # Also register URL-decoded variants that appear in DSNs.
        try:
            from urllib.parse import unquote

            decoded = unquote(secret)
            if decoded != secret and len(decoded) >= 8:
                self._secrets.add(decoded)
        except Exception:  # pragma: no cover - defensive
            pass

    def scrub_text(self, text: str) -> str:
        if not text or not isinstance(text, str):
            return text
        result = text
        for secret in sorted(self._secrets, key=len, reverse=True):
            if secret in result:
                result = result.replace(secret, MASK)
        for pattern in _PATTERNS:
            if pattern.groups >= 3:
                result = pattern.sub(lambda m: f"{m.group(1)}{m.group(2)}{MASK}", result)
            elif pattern.groups == 2:
                result = pattern.sub(lambda m: f"{m.group(1)}{MASK}", result)
            else:
                result = pattern.sub(MASK, result)
        return result

    def scrub_obj(self, value: Any, depth: int = 0) -> Any:
        """Recursively scrub strings inside dicts/lists/tuples (bounded depth)."""
        if depth > 6:
            return MASK
        if isinstance(value, str):
            return self.scrub_text(value)
        if isinstance(value, dict):
            return {key: self.scrub_obj(item, depth + 1) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return type(value)(self.scrub_obj(item, depth + 1) for item in value)
        return value


GLOBAL_REDACTOR = Redactor()


def register_secret(secret: str | None) -> None:
    """Register a runtime secret with the global redactor."""
    GLOBAL_REDACTOR.add(secret)


def scrub(text: str) -> str:
    """Convenience wrapper used when storing error text in the database."""
    return GLOBAL_REDACTOR.scrub_text(text)
