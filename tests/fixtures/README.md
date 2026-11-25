# Test fixtures

This directory is intentionally almost empty.

Most fixtures in this project are **built in code**, not stored as files:

- `tests/support/helpers.py` builds `RawEntry` / `NormalizedEntry` objects and an
  `httpx.MockTransport` client, so a test states exactly the HTTP response it is
  testing instead of pointing at a captured blob.
- `tests/support/fakes.py` provides an in-memory repository, Telegram client and
  Drive, including switchable failure modes.
- `tests/support/pipeline.py` wires the real pipeline against those fakes.
- `tests/evaluation/labels.jsonl` is the labelled confidence set, kept next to the
  evaluation test rather than here.

Storing whole feed captures would hide which field a test actually depends on and
would rot as the real feeds change. Add a file here only when a test genuinely
needs a byte-exact artefact (for example a malformed multipart payload).
