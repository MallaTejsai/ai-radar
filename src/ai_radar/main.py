"""Entry point kept for compatibility with ``python -m ai_radar.main``.

All behaviour lives in :mod:`ai_radar.cli`; this module only forwards.
"""

from __future__ import annotations

from .cli import main

if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
