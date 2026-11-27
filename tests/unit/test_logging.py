"""Logging security and resilience.

Two properties are tested because both have failed in real systems:

* a log line must never contain a registered secret;
* logging must not raise if the stream it writes to is replaced or closed.

The dynamic-stream tests call the logger object directly: pytest's own capture
replaces ``sys.stderr`` per phase, so monkeypatching it and then going through
structlog tests the capture machinery more than this code.
"""

from __future__ import annotations

import io
import sys

from ai_radar.utils.logging import _DynamicStreamLogger, configure_logging, get_logger
from ai_radar.utils.redact import register_secret


def test_dynamic_logger_follows_the_current_stream(monkeypatch):
    buffer = io.StringIO()
    monkeypatch.setattr(sys, "stderr", buffer)

    _DynamicStreamLogger().info("hello_after_stream_replacement")

    assert "hello_after_stream_replacement" in buffer.getvalue()


def test_dynamic_logger_swallows_a_closed_stream(monkeypatch):
    class ClosedStream:
        def write(self, *_args):
            raise ValueError("I/O operation on closed file")

        def flush(self):
            raise ValueError("I/O operation on closed file")

    monkeypatch.setattr(sys, "stderr", ClosedStream())

    # The point is that this returns instead of killing the pipeline.
    _DynamicStreamLogger().warning("must_not_raise")


def test_structlog_output_reaches_stderr(capsys):
    configure_logging(level="INFO", json_output=False)
    get_logger("test.structlog").info("visible_in_stderr")

    assert "visible_in_stderr" in capsys.readouterr().err


def test_registered_secrets_are_redacted_in_logs(capsys):
    configure_logging(level="INFO", json_output=False)
    register_secret("unit-test-secret-value-123")
    get_logger("test.redact").warning(
        "login_failed",
        dsn="postgresql://user:unit-test-secret-value-123@db.example.com/postgres",
        note="unit-test-secret-value-123",
    )

    output = capsys.readouterr().err
    assert "unit-test-secret-value-123" not in output
    assert "REDACTED" in output


def test_secret_shaped_values_are_redacted_without_registration(capsys):
    configure_logging(level="INFO", json_output=False)
    get_logger("test.pattern").info(
        "request_failed",
        url=(
            "https://api.telegram.org/bot123456789:"
            "AAFakeTokenForRedactionTestOnly_1234567890/sendMessage"
        ),
    )

    output = capsys.readouterr().err
    assert "AAFakeTokenForRedactionTestOnly" not in output
