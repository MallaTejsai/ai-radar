"""Probe every enabled registry source and report whether it still works.

This is the tool used to curate `config/sources.yaml`: an endpoint that answers 200
with an HTML page is not a feed, and this prints the difference.

    python scripts/check_sources.py
    python scripts/check_sources.py --source openai_news --json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from ai_radar.collectors import collect_all  # noqa: E402
from ai_radar.settings import load_config  # noqa: E402
from ai_radar.utils.net import SafeHttpClient  # noqa: E402


async def main_async(args: argparse.Namespace) -> int:
    config = load_config(args.config_dir)
    specs = config.sources.enabled()
    if args.source:
        wanted = set(args.source)
        specs = [spec for spec in specs if spec.id in wanted]
        missing = wanted - {spec.id for spec in specs}
        if missing:
            print(f"unknown source id(s): {', '.join(sorted(missing))}", file=sys.stderr)
            return 2

    async with SafeHttpClient(timeout=25.0, retries=2) as client:
        results = await collect_all(
            specs, {}, client, concurrency=6, github_token=config.settings.github_token
        )

    rows = []
    for source_id, result in sorted(results.items()):
        rows.append(
            {
                "source_id": source_id,
                "ok": result.ok,
                "http_status": result.http_status,
                "entries": len(result.entries),
                "not_modified": result.not_modified,
                "error": result.error,
                "note": result.note,
                "elapsed_ms": result.elapsed_ms,
            }
        )

    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        width = max(len(row["source_id"]) for row in rows)
        for row in rows:
            flag = "OK " if row["ok"] else "BAD"
            print(
                f"{flag} {row['source_id']:<{width}} "
                f"{str(row['http_status']):<4} entries={row['entries']:<4} "
                f"{row['elapsed_ms']:>5}ms {row['error'] or row['note'] or ''}"
            )
        ok = sum(1 for row in rows if row["ok"])
        print(f"\n{ok}/{len(rows)} sources healthy")

    return 0 if all(row["ok"] for row in rows) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe the curated source registry")
    parser.add_argument("--source", action="append", help="limit to one source id (repeatable)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--config-dir", default=None)
    return asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
