"""``python -m ai_radar`` entry point.

The GitHub Actions workflows invoke ``python -m ai_radar <command>``. Without this
module the package cannot be executed as a module even when it is installed, which
is exactly how a workflow can look correct and still fail on its first step.

Kept deliberately thin: argument parsing, exit codes and logging live in
:mod:`ai_radar.cli` so the console script and the module entry point cannot drift.
"""

from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":  # pragma: no cover - exercised through the CLI tests
    sys.exit(main())
