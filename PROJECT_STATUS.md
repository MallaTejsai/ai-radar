# AI Radar — Project Status

Last updated: 2026-11-25, after the blocker-fix and public-release preparation cycle.
Evidence base: `artifacts/AI_RADAR_PUBLIC_RELEASE_AUDIT.md`,
`artifacts/AI_RADAR_BLOCKER_INVESTIGATION.md`,
`artifacts/AI_RADAR_CLOUD_DEPLOYMENT_CHECKLIST.md`,
`artifacts/staging_audit_report.json` (read-only staging audit, exit 0), and the
offline suite (`393 passed, 20 deselected`, exit 0).

Only facts supported by a database row, a workflow file, a command that was run or
a test that passed appear below. Anything else is labelled *not verified*.

## Git state

- Branch `main`, 19 commits, HEAD `8ce6b8c` (2026-11-24).
- **No remote is configured.** Nothing was committed or pushed in this cycle.
- Working tree: the fixes below plus the previously untracked staging scripts and
  migration 004. All uncommitted.
- Secret scan of working tree **and full history**: 0 credential-shaped findings.
- Two commits (`a8d5009`, `861adea`) contain Supabase project refs (private
  identifiers, not credentials); they are absent from HEAD. See §Public repository.

## Architecture (unchanged)

Modular Python monolith, entry point `python -m ai_radar` (`src/ai_radar/cli.py`).
Pipeline: COLLECT → NORMALIZE → FILTER → CANONICALIZE → DEDUPLICATE → VERIFY →
**CONFIDENCE GATE** → AI ANALYSIS → PRIORITY SCORING → PERSIST → NOTIFY.
Storage: Supabase PostgreSQL over psycopg3 (session pooler, port 5432).
Providers in router order: free.ai → Gemini → Groq → Grok (paid) → Sarvam (paid) →
deterministic. Notifications: Telegram HTML with a persisted delivery state machine.
Runtime: GitHub Actions cron; all state lives in Postgres.

## Fixed in this cycle

| ID | Defect | Fix | Verified by |
|---|---|---|---|
| F1 | Sarvam per-run cap racy → 2 paid requests against a cap of 1 | `asyncio.Lock` around check+increment (`_reserve_call`), before the socket opens; same for Grok | `test_sarvam_concurrency.py` counts **outbound requests** (1 at cap 1; 0 for a local quota rejection) |
| F2 | Paid analysis spent on a candidate the gate discarded; 0/112 analyses attributable | Confidence gate moved before analysis; explicit `discard_reasons`; provider-aware persist logging | `test_analysis_attribution.py` (persisted provider, zero calls on rejection, fallback reason, cache/row agreement) |
| F3 | `SARVAM_API_KEY` absent from every workflow | Added to `research.yml` with optional `SARVAM_MODEL`; caps keep their enforced defaults | `test_workflows.py` asserts the secret is wired |
| F4 | `tests.yml` migrated production on push to `main` | Job deleted; `migrate-production.yml` is manual + typed confirmation + `production` environment + verified backup before DDL | 4 workflow tests |
| F5 | `invalid_response` undiagnosable; `max_tokens` forced to 2,000 | `http_status` + `finish_reason` + scrubbed ≤200-char excerpt in the error/log; configured ceiling used | `test_sarvam_concurrency.py` (key never in the message) |
| F8 | Alert age checked only at queue time | Age re-checked in `claim_deliveries()`; `expire_stale_deliveries()` closes old intents as `UNKNOWN` | `test_delivery_age.py` |
| F9 | Queue drained one alert per run | Claims one at a time up to the cap, stops on failure | pipeline test sends 2 of 2 in one run |
| F10 | "0.57 (calibrated)" was untrue | Prints `seed value, not calibrated` unless a calibration file is loaded | `doctor` output |
| F12 | Digest comment named the wrong final slot | Comment corrected (22:00 IST) | — |
| F14 | Staging audit left `checks` empty | Mirrors `checks_actionable` | — |
| F15 | CI pinned pytest outside the lock | `requirements-test.txt` shared by CI and local runs | tests.yml installs it |

## Implemented, only partly verified

- **F6 failure visibility — PARTIAL.** Every scheduled workflow now has an
  `if: failure()` monitor step that never echoes the URL, and `research.yml` gates
  on a blocking `doctor --database-only`. A real page cannot be proven while
  `HEALTHCHECKS_PING_URL` is unset.
- **F7 backup path — PARTIAL.** `ai_radar backup --local-dir` exports, writes
  atomically, re-reads and SHA-256-verifies, recording a `VALIDATED` manifest
  (unit-tested). The **offsite** Drive upload still needs OAuth and has never run.
- **Cloud workflows — PARTIAL.** All YAML parses and is asserted by tests, but no
  workflow has ever executed in GitHub Actions.
- **F11 digest timing — unchanged by design.** Cron is correct; the 35-minute
  margin is an accepted operational trade-off.
- **F13 stale `LIKE` patterns — not reproducible.** No executable statement
  contains a literal `%` LIKE pattern.

## Verified in staging (unchanged evidence, 2026-11-25)

- Migrations 001–004 applied with matching checksums; 004 at `17:52:37Z`; the live
  `analysis_results` CHECK includes `sarvam` and `grok`.
- Latest run `20261125T175441Z-c0914d`: 24/24 sources, 550 collected, 205 after
  dedupe, 15 persisted, 1 alert sent, 25 discarded low-confidence, 148.0 s.
- Telegram delivery `ab168a38…` is `SENT` with `telegram_message_id 6`; five further
  intents were `PENDING` with `attempts = 0` — queued, not lost.
- `analysis_results` = 112 rows, all `deterministic / rules-v1`; `analysis_cache` =
  160 (159 deterministic + 1 sarvam). This is the evidence F2 addressed.
- `daily_digests = 0`, `backup_manifests = 0`: never run, not failed.

**Not re-run after the fixes.** No research, provider call, Telegram send,
migration or database write happened in this cycle, so the staging rows above
describe the *pre-fix* behaviour. The fixes are proven offline only.

## Not implemented / not executable today

- Offsite backup and restore rehearsal (needs the Drive OAuth grant).
- Digest delivery (workflow never executed; `daily_digests = 0`).
- Production database: never connected from this workspace; migrations 001–004 are
  not applied there (unverified).
- Confidence calibration: 0.57 remains a 24-sample seed value. It is now labelled
  honestly rather than called calibrated.
- Schedules: `vars.SCHEDULES_ENABLED` is unset, so every scheduled job is skipped.

## Public repository

| Item | Status |
|---|---|
| Secrets in working tree / history | **None found** (scanner exit 0, 19 commits, both directions) |
| `.env`, `.tg-chat-id`, keys, dumps ever committed | Never (only `.env.example`) |
| `.gitignore` coverage | Secrets, dumps, SQL gzips, backups, test/scan output, tool state |
| CI secret scanning | `.github/workflows/tests.yml` → `scripts/check_secrets.py --history` |
| Dependency checks | `pip-audit` (advisory) + `dependency-review` on PRs, public repositories only (skipped, not failed, while private) |
| Private identifiers in history | Supabase project refs in `a8d5009`/`861adea`; absent from HEAD |
| Author identity | Personal e-mail on every commit |
| Repository created / published | **No** — no remote, nothing pushed |

Publication requires a history rewrite or a fresh single-commit repository; see
`artifacts/AI_RADAR_PUBLIC_RELEASE_AUDIT.md` §7 and §9.

## Test results (offline, executed 2026-11-25)

- `pytest -m "not postgres and not live"` → **393 passed, 20 deselected**, exit 0.
  (364 before this cycle; 29 new tests.)
- `python -m compileall -q src scripts` → exit 0.
- `python scripts/check_secrets.py --history` → exit 0, 0 findings.
- Not run: database-marked tests (20), live provider tests, Telegram sends, Actions.

## Deployment status

**NOT DEPLOYED — READY FOR A GATED FIRST RUN.**

The blockers that prevented unattended operation (F1, F2, F3, F4) are fixed and
regression-tested offline. What remains is operational and cannot be done from this
workspace: publish the repository safely, configure secrets and the `production`
environment, complete the Drive grant, apply production migrations through the
manual workflow, then run research and digest once before enabling schedules.

Nothing in this cycle enabled a schedule, deployed, pushed, or contacted production.
