"""Structured logging with mandatory secret redaction.

JSON is emitted when running in CI (so GitHub Actions logs stay machine
readable); a coloured human format is used locally.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any, MutableMapping

import structlog

from .redact import GLOBAL_REDACTOR

_CONFIGURED = False

_LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}


def _redaction_processor(
    logger: Any, method_name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    scrubbed = GLOBAL_REDACTOR.scrub_obj(dict(event_dict))
    assert isinstance(scrubbed, dict)
    return scrubbed


class _DynamicStreamLogger:
    """structlog logger that resolves ``sys.stderr`` on every write.

    ``structlog.PrintLogger`` binds the stream when it is constructed. That is
    wrong for a long-running process - and fatal under pytest capture, under a
    replaced stream, or when a parent closes the descriptor - because every later
    log call writes to a closed file object. Resolving the stream per write also
    keeps log capture working.
    """

    def __init__(self, stream_name: str = "stderr") -> None:
        self._stream_name = stream_name

    def _write(self, message: str) -> None:
        stream = getattr(sys, self._stream_name, None) or sys.stderr
        try:
            print(message, file=stream, flush=True)
        except (ValueError, OSError):
            # The stream was closed underneath us; losing a log line must never
            # take down the pipeline.
            pass

    debug = info = warning = error = critical = exception = _write

    def bind(self, **_kwargs: Any) -> "_DynamicStreamLogger":
        return self

    def unbind(self, *_args: Any, **_kwargs: Any) -> "_DynamicStreamLogger":
        return self

    def try_unbind(self, *_args: Any, **_kwargs: Any) -> "_DynamicStreamLogger":
        return self

    def new(self, **_kwargs: Any) -> "_DynamicStreamLogger":
        return self


def configure_logging(level: str | None = None, json_output: bool | None = None) -> None:
    """Configure structlog once per process."""
    global _CONFIGURED

    level_name = (level or os.environ.get("AI_RADAR_LOG_LEVEL", "INFO")).upper()
    numeric_level = _LEVELS.get(level_name, logging.INFO)

    if json_output is None:
        json_output = os.environ.get("GITHUB_ACTIONS", "").lower() == "true"

    renderer: Any
    if json_output:
        renderer = structlog.processors.JSONRenderer(sort_keys=True)
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=False)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _redaction_processor,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
        logger_factory=lambda *_args, **_kwargs: _DynamicStreamLogger(),
        cache_logger_on_first_use=True,
    )

    # Third-party libraries are noisy and occasionally log URLs verbatim.
    for noisy in ("httpx", "httpcore", "urllib3", "feedparser"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _CONFIGURED = True


def get_logger(name: str = "ai_radar") -> Any:
    """Return a configured structlog logger."""
    if not _CONFIGURED:
        configure_logging()
    return structlog.get_logger(name)
