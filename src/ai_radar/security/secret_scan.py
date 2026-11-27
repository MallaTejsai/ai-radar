"""Credential discovery for the working tree and Git history.

This module exists so that "no secrets are committed" is a test, not a promise.
It reports **where** and **what kind**, never the value: a scanner that prints
the match it found has published the secret a second time, into CI logs.

Nothing here contacts a network service. The rules are shape-based (the prefixes
and structures credentials actually have), plus a few assignment patterns for
provider keys whose shape is not distinctive.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Rule:
    name: str
    pattern: re.Pattern[str]


#: Ordered by specificity; the first matching rule names a line.
RULES: tuple[Rule, ...] = (
    Rule(
        "private-key-block",
        re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----"),
    ),
    Rule("aws-access-key-id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    Rule("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    Rule("github-fine-grained-token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{40,}\b")),
    Rule("slack-token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    Rule("openai-style-key", re.compile(r"\bsk-(?:free-)?[A-Za-z0-9_-]{24,}\b")),
    Rule("telegram-bot-token", re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b")),
    Rule("telegram-chat-id-literal", re.compile(r"TELEGRAM_CHAT_ID\s*[=:]\s*['\"]?-?\d{6,}")),
    Rule("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    Rule("google-oauth-client-secret", re.compile(r"\bGOCSPX-[A-Za-z0-9_-]{20,}\b")),
    Rule(
        "database-url-with-password",
        re.compile(r"\bpostgres(?:ql)?://[^/\s:@]+:[^@\s/]{8,}@"),
    ),
    Rule(
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
    ),
    Rule(
        "provider-key-assignment",
        re.compile(
            r"(?i)\b(?:SARVAM|XAI|GEMINI|GROQ|FREEAI|OPENAI|ANTHROPIC)[A-Z_]*API_KEY"
            r"\s*[=:]\s*['\"]?[A-Za-z0-9_-]{16,}"
        ),
    ),
    Rule(
        "bearer-literal",
        re.compile(r"(?i)\bBearer\s+(?!<|\$\{|\$)[A-Za-z0-9._~+/-]{24,}=*"),
    ),
)

#: Filenames that must never be tracked, whatever they contain.
SENSITIVE_FILE = re.compile(
    r"(^|/)("
    r"\.env(?!\.example$)(\..*)?"
    r"|\.tg-chat-id"
    r"|[^/]*\.(pem|key|p12|pfx)"
    r"|id_rsa"
    r"|service-account[^/]*\.json"
    r"|client_secret[^/]*\.json"
    r"|[^/]*\.(dump|sql\.gz|bak)"
    r")$"
)

#: A shape match inside one of these is documentation, not a credential.
_PLACEHOLDER_MARKERS = (
    "<",
    ">",
    "your",
    "example",
    "placeholder",
    "redacted",
    "changeme",
    "xxxxxxxx",
    "…",
    "...",
    "fake",
    "dummy",
    "sample",
)


def looks_like_placeholder(value: str) -> bool:
    lowered = value.lower()
    return any(marker in lowered for marker in _PLACEHOLDER_MARKERS)


@dataclass(frozen=True)
class Finding:
    rule: str
    location: str

    def render(self) -> str:
        return f"{self.rule}: {self.location}"


def scan_text(text: str, *, location: str) -> list[Finding]:
    """Return credential-shaped findings in one document, without the values."""
    findings: list[Finding] = []
    for number, line in enumerate(text.splitlines(), 1):
        for rule in RULES:
            for match in rule.pattern.finditer(line):
                if looks_like_placeholder(match.group(0)) or looks_like_placeholder(line):
                    continue
                findings.append(Finding(rule=rule.name, location=f"{location}:{number}"))
    return findings


def scan_sensitive_filename(path: str) -> Finding | None:
    if SENSITIVE_FILE.search(path.replace("\\", "/")):
        return Finding(rule="tracked-sensitive-file", location=path)
    return None


def load_allowlist(path: Path) -> tuple[str, ...]:
    """Exact repository-relative paths whose synthetic values are expected."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return ()
    return tuple(
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )


def is_allowlisted(location: str, entries: tuple[str, ...]) -> bool:
    """Match the file part of ``path:line`` or ``path@commit`` against entries."""
    path = location.split("@", 1)[0]
    head, _, tail = path.rpartition(":")
    if head and tail.isdigit():
        path = head
    return path in entries


def summarize(findings: list[Finding]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for finding in findings:
        counts[finding.rule] = counts.get(finding.rule, 0) + 1
    return dict(sorted(counts.items()))
