"""Credential scanning for public release.

Working tree
    Every tracked file and every untracked, non-ignored file. Ignored files
    (``.env``, ``artifacts/``) are not part of the repository, so they are not
    scanned.

History (``--history``)
    Every line added *or removed* by every commit on every branch. Deleting a
    secret in a later commit does not unpublish it: it stays in the object
    database and is served by GitHub.

Output never includes the matched value - only the rule, file and line (or
commit) - so this script is safe to run in CI logs and to paste into a report.

Usage:
    python scripts/check_secrets.py
    python scripts/check_secrets.py --history
    python scripts/check_secrets.py --history --json
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ai_radar.security.secret_scan import (  # noqa: E402
    Finding,
    is_allowlisted,
    load_allowlist,
    scan_sensitive_filename,
    scan_text,
    summarize,
)

ALLOWLIST = ROOT / ".github" / "secret-scan-allowlist.txt"

MAX_FILE_BYTES = 2_000_000
HISTORY_PREFIX = "__AI_RADAR_COMMIT__"


def _git(*args: str) -> bytes:
    return subprocess.run(
        ["git", *args],
        cwd=ROOT,
        check=True,
        capture_output=True,
    ).stdout


def _files_to_scan() -> list[str]:
    tracked = _git("ls-files", "-z").split(b"\0")
    untracked = _git("ls-files", "-z", "--others", "--exclude-standard").split(b"\0")
    names = {item.decode("utf-8", "replace") for item in (*tracked, *untracked) if item}
    return sorted(names)


def _read(path: Path) -> str:
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    if len(data) > MAX_FILE_BYTES or b"\0" in data[:8192]:
        return ""
    return data.decode("utf-8", "replace")


def scan_working_tree() -> list[Finding]:
    findings: list[Finding] = []
    allowed = load_allowlist(ALLOWLIST)
    for name in _files_to_scan():
        if name in allowed:
            continue
        filename_finding = scan_sensitive_filename(name)
        if filename_finding is not None:
            findings.append(filename_finding)
            continue
        text = _read(ROOT / name)
        if text:
            findings.extend(scan_text(text, location=name))
    return findings


def scan_history() -> list[Finding]:
    """Scan every diff line in every commit, including deleted lines."""
    try:
        stream = _git(
            "log",
            "--all",
            "--no-color",
            "--unified=0",
            "--format=" + HISTORY_PREFIX + "%H",
            "-p",
        ).decode("utf-8", "replace")
    except subprocess.CalledProcessError:  # pragma: no cover - not a git checkout
        return []

    findings: list[Finding] = []
    commit = "unknown"
    file = "unknown"
    for line in stream.splitlines():
        if line.startswith(HISTORY_PREFIX):
            commit = line[len(HISTORY_PREFIX) :][:12] or "unknown"
            continue
        if line.startswith("+++ ") or line.startswith("--- "):
            candidate = line[4:].strip()
            if candidate and candidate != "/dev/null":
                file = candidate.removeprefix("b/").removeprefix("a/")
            continue
        if line.startswith(("+", "-")) and not line.startswith(("+++", "---")):
            body = line[1:]
            for finding in scan_text(body, location=f"{file}@{commit}"):
                findings.append(finding)
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scan for committed credentials")
    parser.add_argument("--history", action="store_true", help="also scan every commit")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    allowed = load_allowlist(ALLOWLIST)
    findings = scan_working_tree()
    if args.history:
        findings.extend(scan_history())
    findings = [item for item in findings if not is_allowlisted(item.location, allowed)]

    # One finding per location+rule; history repeats the same line across commits.
    unique: dict[tuple[str, str], Finding] = {}
    for finding in findings:
        unique.setdefault((finding.rule, finding.location), finding)
    ordered = [unique[key] for key in sorted(unique)]

    if args.json:
        print(
            json.dumps(
                {
                    "ok": not ordered,
                    "findings": [
                        {"rule": f.rule, "location": f.location} for f in ordered
                    ],
                    "summary": summarize(ordered),
                },
                indent=2,
            )
        )
    else:
        for finding in ordered:
            print(f"[FAIL] {finding.render()}")
        if ordered:
            print(
                f"\n{len(ordered)} credential-shaped finding(s). "
                "Rotate the credential and clean history before publishing.",
                file=sys.stderr,
            )
        else:
            print(
                "No credential-shaped strings found "
                f"({'working tree + history' if args.history else 'working tree'})."
            )
    return 1 if ordered else 0


if __name__ == "__main__":
    raise SystemExit(main())
