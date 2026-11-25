# Repository audit and engineering plan

This is the pre-implementation audit required by the build brief, kept current as
the delivery record. It states what the repository contained, what was built
against it, which dependencies and integrations exist, what can go wrong, and where
each milestone stands.

Status vocabulary used throughout: **Implemented**, **Tested locally**, **Tested
against real services**, **Deployed**, **Not verified**. Anything not proven is
labelled as such.

## 1. Method

1. Inventory every file, not just the ones referenced by the README.
2. Read the existing skeleton before changing it; archive it instead of deleting.
3. Map every requirement in the brief to a module and a test.
4. Implement stage by stage, running the suite after each change.
5. Prefer a real failure over a simulated success.

## 2. Audit of the starting repository

The repository was not empty. It contained a generated skeleton, preserved at
`archive/skeleton-20261123T1820Z/` for comparison.

| Finding | Evidence | Impact |
|---|---|---|
| Logic lived inside package `__init__.py` files | `archive/.../src/intelligence/__init__.py` (15 KB), `storage/__init__.py` (18 KB) | untestable units, import cycles, no clear module boundary |
| Hard-coded, already-retired model ids | `gemini-1.5-flash` and `mixtral-8x7b-32768` in the archived intelligence module | every analysis call would fail on a current account |
| API key passed in the URL query string | `...:generateContent?key={api_key}` | key leaks into logs, proxies and error messages |
| Free-form string states | no closed enums for verdict, delivery or run state | invalid states reach the database |
| No migrations runner, two copies of one SQL file | `database/20240101_001_initial_schema.sql` and `database/migrations/...` | drift between schema and code |
| No tests | none present | no evidence any path worked |
| No documentation beyond a README | `docs/` absent | deployment and recovery undocumented |
| `.env.example` contained placeholders but no explanation of the OAuth flow | — | Drive setup impossible to follow |

The skeleton was archived rather than edited, and the production tree was rebuilt
around a tested module layout. Nothing in the new tree imports from the archive.
The duplicated legacy migration was deleted from `database/migrations/` once the
new schema replaced it (the copy remains under `archive/`), so the runner sees two
ordered migrations instead of applying an obsolete third one that created an
unused `source_checkpoints` table.

## 3. Architecture implementation checklist

| Brief § | Requirement | Implementation | Evidence | Status |
|---|---|---|---|---|
| 4 | 15–25 official sources, 7 categories, tiers | `config/sources.yaml` (24 sources), `SourceRegistry.validate_registry` | `sources --validate`; 24/24 live probe 2026-11-23 | Implemented, tested live |
| 4 | Timeouts, retries, backoff, `Retry-After`, ETag, size caps, isolation | `utils/net.py`, `collectors/base.py` | `tests/unit/test_net.py`, `test_collectors.py` | Implemented, tested locally |
| 5 | 11-stage pipeline with explicit failure behaviour | `operations/pipeline.py` | `tests/integration/test_pipeline.py` | Implemented, tested locally |
| 6 | Deterministic dedupe, multi-source evidence, DB uniqueness, concurrency | `processing/dedupe.py`, `storage/repository.py`, advisory lock | `test_dedupe.py`, `test_backup_and_lock.py` (lock) | Implemented, tested locally |
| 7 | Evidence verification, pricing statuses, no invented facts | `verification/verifier.py`, `processing/pricing.py` | `test_verifier.py` (11 cases), `test_pricing.py` | Implemented, tested locally |
| 8 | Gemini → Groq → deterministic, structured output, retries, health | `intelligence/router.py`, `gemini.py`, `groq.py`, `deterministic.py` | `test_intelligence.py`, `test_prompts.py` | Implemented, live credentials not tested |
| 9 | Confidence discarded completely, priority weights 30/25/20/15/10 | `processing/confidence.py`, `scoring.py`, pipeline stage 8 | `test_confidence.py`, `test_scoring.py` | Implemented, threshold calibrated on 24 cases |
| 10 | 9 memory tables + runtime state, transactions, atomic claims | `database/migrations/*.sql`, `storage/repository.py` | schema review; Postgres tests skipped locally | Implemented, DB tests not executed |
| 11 | Telegram engine, max 5/hour, persistent states, no exactly-once claim | `notifications/*`, `deliveries` table | `test_format.py`, failure suite | Implemented, live credentials not tested |
| 12 | Daily digest with 7 sections, sent 22:35 IST | `reporting/digest.py`, `digest.yml` | `test_digest.py` | Implemented, tested locally |
| 13 | GitHub Actions schedules, concurrency, locks | `.github/workflows/*.yml` | `doctor` cron check | Implemented, not deployed |
| 14 | 24 h missed-run recovery, bounded backlog | `operations/recovery.py`, `utils/timeutil.py` | `test_registry_and_recovery.py` | Implemented, tested locally |
| 15 | Drive backup, manifest, SHA-256, restore test | `operations/backup.py`, `restore.py`, `drive.py` | `test_backup_and_lock.py`, `test_drive.py` | Implemented, live credentials not tested |
| 16 | Independent heartbeat | `operations/heartbeat.py`, healthchecks.io | `test_backup_and_lock.py` partially | Implemented, not deployed |
| 17 | Secrets, SSRF, injection resistance, least privilege | `utils/redact.py`, `utils/net.py`, `intelligence/prompts.py`, workflow `permissions` | `test_net.py`, `test_prompts.py`, `docs/security.md` | Implemented, tested locally |
| 18 | ₹0 cost control, no paid fallback | `config/settings.yaml`, provider chain | `docs/cost-model.md` | Implemented, limits verified 2026-11-23 |
| 19 | Unit, integration, failure, end-to-end tests | `tests/unit`, `integration`, `failure`, `evaluation` | 295 passed, 20 skipped (local) | Implemented, partially executed |
| 20 | Project structure | tree matches the brief, with `__main__.py` added | `find`/`ls` | Implemented |
| 22 | PC-independence acceptance test | procedure in `docs/deployment.md` §10 | none | **NOT VERIFIED** |
| 23 | Documentation set | `docs/*` | this repository | Implemented |

## 4. Dependency and integration plan

### Runtime dependencies

| Package | Why | Rejected alternative |
|---|---|---|
| `pydantic` / `pydantic-settings` | typed config and closed schemas at every boundary | dataclasses + hand validation (silent coercion) |
| `httpx` | async client with a test transport, one client for all outbound calls | `requests` (sync, no mock transport), `aiohttp` (heavier) |
| `psycopg[binary]`, `psycopg-pool` | real SQL: transactions, `FOR UPDATE SKIP LOCKED`, advisory locks | Supabase SDK (hides the SQL), `asyncpg` (fine, but psycopg3 covers both) |
| `feedparser` | tolerant RSS/Atom parsing | hand-rolled XML (fails on real feeds) |
| `python-dateutil` | robust date parsing across feed formats | `datetime.fromisoformat` alone |
| `structlog` | structured, redaction-friendly logs | `logging` + string formatting |
| `PyYAML` | human-reviewable policy files | JSON config (no comments) |
| `pytest`, `pytest-asyncio` (dev) | tests | unittest (weaker fixtures) |

Not used, deliberately: LangChain, any agent framework, any vector database, any
Google SDK, any HTML scraping library. Each was either unnecessary for the brief or
would have added a failure mode without adding capability.

### External integrations

| Integration | Contract | Local test | Live test |
|---|---|---|---|
| RSS/Atom + GitHub releases + HF Hub | HTTP GET, ETag/Last-Modified | mock transport | **24/24 sources probed live 2026-11-23** |
| Supabase Postgres | psycopg3 over TLS | fake repository | **not executed** (no database) |
| Gemini | `v1beta/models`, `generateContent`, JSON mime type | mock transport | **not executed** (no key) |
| Groq | OpenAI-compatible `/chat/completions` | mock transport | **not executed** (no key) |
| Telegram | `sendMessage` HTML, `getMe` | fake client | **not executed** (no token) |
| Google Drive | OAuth2 refresh, multipart upload, `appProperties` | `test_drive.py` | **not executed** (no grant) |
| healthchecks.io | ping URL, `/fail` | fake | **not executed** |
| GitHub Actions | cron + dispatch | YAML review, `doctor` cron check | **not deployed** |

## 5. Risk register

Likelihood and impact are for the first month of operation.

| ID | Risk | L | I | Mitigation | Residual |
|---|---|---|---|---|---|
| R1 | Telegram duplicates after a timeout | M | L | `UNKNOWN` state, no auto-retry, reconciliation in `maintenance` | duplicates possible; exactly-once impossible |
| R2 | Refresh token expires (OAuth app left in Testing) | H if misconfigured | H | helper + deployment doc require "In production"; backup fails loudly | none if followed |
| R3 | `drive.file` cannot write into a user-created folder | H if folder id set | M | folder id optional; app creates its own folder | none if empty |
| R4 | Supabase project pauses after inactivity | L (hourly runs) | H | heartbeat + `maintenance`; resume procedure | manual action needed |
| R5 | Model ids retired again | H over 12 months | M | runtime `/models` resolution, preference list, pin option | brief fallback to a weaker model |
| R6 | Gemini daily quota exceeded | M | L | Groq fallback, deterministic fallback, `max_analyses_per_run` | degraded summaries |
| R7 | Private-repo Actions minutes exhausted | M | M | public repo recommended; cost model documents the 2-hour alternative | billing block until fixed |
| R8 | Prompt injection in a feed item | H | L | fenced untrusted text, advisory output, deterministic pricing/priority | misleading prose in a summary |
| R9 | Dependency compromise | L | H | exact pins, Dependabot, minimal dependency set | supply-chain risk not zero |
| R10 | Scheduled workflows disabled after 60 days (public repo) | M | M | heartbeat alert; monthly keepalive commit | manual action |
| R11 | Backup exists but is unreadable | L | H | checksum chain, restore drill, `verify_only` weekly | undetected bit rot between drills |
| R12 | Analysis cap drops a busy hour's items | M on launch days | L | items are re-collected next run; cap logged | an item can be missed if the feed rotates |
| R13 | HF substance gate suppresses a whole source | M | L | gate documented; source health is visible | image-model recall is thin |
| R14 | Timezone confusion between cron, IST and UTC | M | M | crons in UTC, policy file, `doctor` cross-check | human error on edits |

## 6. Milestones

The brief requires milestone-by-milestone delivery. Each milestone below is
complete as *code + local tests*; cloud milestones require the owner's accounts.

### M0 — Repository audit and foundation

- Inventory and archive of the skeleton.
- Python package layout with `pyproject.toml`, `src/` layout, lock file.
- Typed settings (`settings.py`) and closed enums (`models/core.py`).
- Migrations for 9 memory tables plus `system_state`, `analysis_cache`,
  `provider_usage`, `schema_migrations`.
- CI (`tests.yml`) on Python 3.12 and 3.13, byte-compile + tests.
- `.env.example`, `.gitignore`, logging with redaction.

### M1 — First real discovery

- RSS, GitHub releases and Hugging Face collectors.
- Normalise → canonicalise → filter → dedupe → verify.
- Gemini adapter with runtime model resolution; deterministic fallback.
- Persistence through the repository layer.
- Telegram client and a real test message path.
- Result: `collect --source openai_news` returns live entries; `research --dry-run`
  exercises the whole pipeline without sending.

### M2 — Full research coverage

- 24 sources across all seven categories, tiers A/B/C.
- Groq adapter and the provider router.
- Confidence scoring, calibration on a labelled seed set, priority scoring with the
  mandated weights.
- Daily digest with seven sections.
- Result: `sources --validate` reports 24/24; digest renders from stored rows.

### M3 — Reliability

- Persistent delivery state machine.
- Retry policy and `UNKNOWN` reconciliation.
- 24-hour recovery planning, checkpoint discipline, advisory lock.
- Heartbeat plus independent external monitor.
- Result: failure suite covers timeout, 429, invalid JSON, provider outage, DB
  outage, concurrent runs, crash after send, failed and corrupted backup.

### M4 — Backup and production readiness

- Drive backup with checksum chain and retention.
- Restore into an isolated database with row-count verification.
- Security documentation and audit.
- Evaluation dataset (24 labelled cases) and threshold calibration.
- Deployment, Android, Windows, recovery and cost documentation.
- Result: local suite green; cloud deployment and PC-independence test remain with
  the owner.

## 7. Deliberate deviations from the brief

| Brief | Implementation | Why |
|---|---|---|
| Digest at 22:00 IST | 22:35 IST | 22:00 would race the 22:30 research run and digest an incomplete day |
| "Create migrations" | idempotent SQL + checksum-guarded runner | re-running a migration must not corrupt state |
| `example cron` for research | identical cron retained | 08:00–22:00 IST at :30 is exactly the brief's example |
| Drive folder in secrets | folder optional | `drive.file` cannot write into a folder it did not create |
| Workflow installs the package | `PYTHONPATH=src` | no build step in CI; the same code is installed for local use |
| Confidence threshold | 0.57 from 24 labelled cases | the brief forbids inventing a "validated" threshold; this one is labelled as a seed calibration |

## 8. Open items before production

1. Provide the nine production secrets; run the §9 sequence in
   [deployment.md](deployment.md).
2. Execute the Postgres integration tests against the real database.
3. Execute the live Gemini, Groq, Telegram and Drive contract tests.
4. Perform the PC-off acceptance test and record the timestamps in
   [final-report.md](final-report.md).
5. Optional hardening from [security.md](security.md) §3.
