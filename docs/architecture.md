# Architecture

AI Radar V1.0 is a modular Python monolith that runs entirely on free cloud tiers.
The development PC is never part of the production path.

```
GitHub Actions (cron)                    Supabase PostgreSQL            Google Drive
┌────────────────────┐                  ┌────────────────────┐        ┌──────────────┐
│ research.yml  :30  │──┐               │ 9 memory tables    │        │ weekly       │
│ digest.yml   22:35 │  │   ┌───────────┤ + runtime state    │        │ .json.gz     │
│ backup.yml   Sun   │  ├──▶│  ai-radar │                    │        │ backups      │
│ maintenance 06:45  │  │   │    CLI    │                    │        └──────▲───────┘
└────────────────────┘  │   └─────┬─────┘                    │               │
                        │         │                          │               │
                   Telegram ◀─────┘                          │        OAuth2 refresh
                   Bot API          free.ai→Gemini→Groq      │        (drive.file)
                                    (free tiers only)        │
                                                             │
                        healthchecks.io  ◀── one ping after a successful run
```

Provider order: **free.ai → Gemini → Groq → deterministic**.

## Module map

| Module | Responsibility | Key file |
|---|---|---|
| `settings` | Typed config: env secrets + reviewed YAML policy, loaded once | `src/ai_radar/settings.py` |
| `models` | Pydantic domain objects; closed enums for every state | `src/ai_radar/models/` |
| `collectors` | RSS/Atom, GitHub releases, Hugging Face Hub; per-source isolation | `src/ai_radar/collectors/` |
| `processing` | Normalise, canonicalise, filter, dedupe, pricing, confidence, scoring | `src/ai_radar/processing/` |
| `verification` | Evidence checks and verdicts | `src/ai_radar/verification/verifier.py` |
| `intelligence` | Provider adapters, prompt hardening, router with fallback | `src/ai_radar/intelligence/` |
| `storage` | psycopg3 pool, migrations, all SQL in one repository | `src/ai_radar/storage/` |
| `notifications` | Telegram client, HTML formatter, delivery state machine | `src/ai_radar/notifications/` |
| `reporting` | Daily digest assembly | `src/ai_radar/reporting/digest.py` |
| `operations` | Pipeline, recovery, heartbeat, backup, restore, maintenance | `src/ai_radar/operations/` |
| `cli` | One entry point per workflow job | `src/ai_radar/cli.py` |

## Pipeline stages

The orchestrator is `operations/pipeline.py::ResearchPipeline.run`. Stage order is
fixed and each stage has one job:

| # | Stage | Input → output | Failure behaviour |
|---|---|---|---|
| 1 | COLLECT | registry → `CollectResult` per source | isolated per source; recorded as source health |
| 2 | NORMALIZE | `RawEntry` → `NormalizedEntry` | entry dropped with a reason code |
| 3 | FILTER | `NormalizedEntry` → kept/rejected | rejection counted, never silent |
| 4 | CANONICALIZE | entry → `canonical_key` + `title_fingerprint` | deterministic, no model |
| 5 | DEDUPLICATE | batch → unique set; then DB check | existing rows skipped, not rewritten |
| 6 | VERIFY | entry → `VerificationOutcome` (verdict, evidence, confidence) | per-entry exception is isolated |
| 7 | AI ANALYSIS | entry → `AnalysisResult` (advisory) | provider chain, then deterministic |
| 8 | CONFIDENCE CHECK | confidence → keep/discard | **discard is total**: no row, no alert, no digest |
| 9 | PRIORITY SCORING | evidence → `ScoreBreakdown` (0-100) | fully attributable |
| 10 | PERSIST | records → transaction | failure blocks checkpoint advance |
| 11 | NOTIFY | delivery queue → Telegram | state machine, ambiguous ≠ retry |

Stage 8 is the blunt instrument the spec demands: a discovery below the threshold
leaves no trace other than a counter in `workflow_runs.report`.

## Why these choices

**psycopg3 instead of a Supabase SDK.** The spec requires atomic claims, real
transactions and `FOR UPDATE SKIP LOCKED`. Those are SQL features, and the SDK
hides the SQL. Everything the system reads or writes is in
`storage/repository.py`, so it can be reviewed in one place.

**Runtime model resolution instead of pinned model IDs.** Google retired
`gemini-1.5` and now redirects `gemini-2.5`; Groq retired `mixtral-8x7b-32768`.
Both adapters therefore call the provider's `/models` endpoint and pick the first
available ID from a preference list. `GEMINI_MODEL` / `GROQ_MODEL` pin an exact ID
when reproducibility matters more than longevity.

**HTML parse mode for Telegram.** MarkdownV2 requires escaping 18 characters;
one miss makes the API reject the whole message. In HTML only `&`, `<`, `>`
matter, and `html.escape` cannot get that wrong.

**OAuth2 refresh token for Drive, not a service account.** A service account has
no storage quota, so it cannot own files in a personal Drive. `drive.file` scope
also limits the app to files it created itself.

**Job-level modules instead of one long script.** Each workflow calls one CLI
subcommand. No business logic lives in YAML, so a workflow edit cannot change
behaviour without a code change.

## State and durability

Three independent states, each with one owner:

| State | Owner | Survives |
|---|---|---|
| Research memory | Supabase PostgreSQL | runner teardown, PC shutdown |
| Delivery intent | `deliveries` table | crash between send and record |
| Off-site archive | Google Drive | database loss |

GitHub Actions' filesystem is treated as ephemeral everywhere. Nothing that must
be remembered is written to disk; the run lock, checkpoint and queue all live in
PostgreSQL.

## Failure containment

- A collector exception becomes one `CollectResult(error=...)`.
- A verifier exception becomes one discarded entry.
- A provider exception becomes a fallback step, never an aborted run.
- A delivery ambiguity becomes `UNKNOWN` and stops the queue for that run.
- A database failure aborts the run before any checkpoint moves, so the next run
  re-collects the same window.

## Known limits

- Telegram has no exactly-once guarantee. Duplicates are minimised, not eliminated.
- Verification cannot read behind JavaScript or logins; such fields stay
  `UNVERIFIED` by design. Availability is probed per URL with a 120-probe budget
  per run; past the budget a URL is recorded as unprobed rather than confirmed.
- The Hugging Face Hub source applies a substance gate (3 likes / 200 downloads),
  which trades long-tail recall for precision. See `docs/sources.md`.
- Analysis is capped at 40 discoveries per run. Items beyond the cap are not
  persisted and are re-collected next run.
- Backups carry their SHA-256 in Drive `appProperties` and a `tables_sha256` inside
  the payload. A backup without a checksum is refused on restore.
- The database must be reached through the shared pooler in **session mode** (port
  5432) or directly: the run lock is session-scoped and psycopg prepares
  statements, neither of which survives a transaction pooler. Port 6543 is refused
  at startup.
- Internal staleness is schedule-aware: the latest eligible slot plus a 60-minute
  grace, so the deliberate 22:00–08:00 gap is not an incident.
- The confidence threshold (0.57) was derived from a 24-case seed set, not a
  statistically valid study. See `FINAL_PRE_DEPLOYMENT_AUDIT.md` §10.
