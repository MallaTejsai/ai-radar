# AI Radar V1.0

An autonomous AI research agent that runs entirely on free cloud tiers. It
collects from 24 official sources across seven categories, verifies each candidate
against real evidence, analyses it with free-tier models, stores what qualifies in
PostgreSQL, alerts Telegram only for evidence-confirmed discoveries, sends a daily
digest, recovers missed runs, monitors its own heartbeat and backs itself up to
Google Drive — with the development PC switched off.

## Status

Read this before trusting anything else in the repo.

| Area | State |
|---|---|
| Source code, migrations, workflows, tests | **Implemented** |
| Unit + failure + end-to-end + workflow suites | **Tested locally** — 393 passed, 20 deselected (Python 3.12, 2026-11-25) |
| Credential scan (working tree + full history) | **PASSED** — 0 credential-shaped strings (2026-11-25) |
| Paid-provider accounting (atomic cap, attribution) | **Implemented + regression-tested offline** — not re-run against the live provider |
| Collectors against real endpoints | **Tested against real services** (24/24 responded, 436 entries on 2026-11-24) |
| PostgreSQL migrations + non-destructive verifier | **Executed against Supabase** (2026-11-24) — 3 migrations, 13 tables, 14/14 checks, 0 leftover rows |
| Destructive PostgreSQL integration suite | **Blocked** — needs a disposable test database; 16 tests skipped |
| free.ai live contract | **PASSED** — one authenticated call, HTTP 200, validated payload, 571 tokens |
| Gemini / Groq live contracts | **Not configured** — no keys; skipped |
| Telegram | **PARTIAL** — token present; `getMe` timed out from this PC, so authentication is UNVERIFIED; no chat id configured, so nothing was sent |
| Google Drive backup / restore | **Not configured** — no OAuth grant; skipped |
| Cloud deployment | **Not deployed** — no Git remote configured; publication is a manual owner action |
| Public repository | **Not published** — prepared and audited, nothing pushed |
| PC-independence acceptance test | **NOT VERIFIED** — cannot be claimed until run with the PC off |

The full evidence table is in [`docs/final-report.md`](docs/final-report.md).

`PC_INDEPENDENCE_STATUS = NOT_VERIFIED` — set to `VERIFIED` only after the
PC-off acceptance test in [docs/FINAL_PRE_DEPLOYMENT_AUDIT.md](docs/FINAL_PRE_DEPLOYMENT_AUDIT.md) §14 passes.

## Pipeline

```
COLLECT → NORMALIZE → FILTER → CANONICALIZE → DEDUPLICATE → VERIFY
        → AI ANALYSIS → CONFIDENCE CHECK → PRIORITY SCORING → PERSIST → NOTIFY
```

Hard rules enforced in code, not by convention:

- A collector never notifies anything; only the dispatcher talks to Telegram, and
  only about rows already committed.
- Low-confidence discoveries are discarded **completely** — no row, no alert, no
  digest entry, only a counter.
- A source checkpoint advances only after its records are durably persisted.
- Alerts require a CONFIRMED verdict, priority ≥ 3/5, a calibrated confidence
  threshold and Tier A evidence. PARTIAL discoveries are stored as a watchlist and
  never alerted.
- Pricing is `UNVERIFIED` unless the collected text states it. A free trial is
  never reported as free; open weights are never reported as open source.

## Quick start (development PC)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip setuptools wheel
pip install -r requirements.lock
pip install -e .
Copy-Item .env.example .env      # then fill in what you have
ai-radar doctor                  # PASS/FAIL/WARN per integration
ai-radar migrate                 # create the schema
ai-radar research --dry-run      # full pipeline, never sends Telegram
ai-radar collect --source openai_news   # inspect one source without persisting
```

`python -m pytest tests` runs the offline suite. `-m postgres` and `-m live` need a
database and real credentials respectively.

A documented placeholder such as `<github-read-only-pat>` is treated as **not
configured**, never as a credential: copying `.env.example` to `.env` cannot make
the app authenticate with a placeholder.

## Security: developing in a public repository

This repository is designed to be safe to publish. Nothing in it is a secret:
credentials live in a local, untracked `.env` or in GitHub Actions Secrets.

**Local**

- `.env` is gitignored; `.env.example` contains placeholders only.
- Copy `.env.example` to `.env` and fill in what you have. A placeholder is
  treated as "not configured", so a copied template cannot authenticate anything.
- The staging DSN never goes in `.env`: it is stored in Windows Credential
  Manager (`scripts\staging_credentials.ps1 -Setup`).

**Continuous integration**

- `tests.yml` runs with **no database credential** and cannot apply a migration.
- A `secret-scan` job runs `python scripts/check_secrets.py --history`: it scans
  every tracked/untracked file **and every line of every commit**, and prints only
  the rule, file and commit - never the matched value.
- A `dependency-audit` job runs `pip-audit` against `requirements.lock`, and
  `dependency-review` runs on pull requests.

Run the scanner yourself before publishing:

```bash
python scripts/check_secrets.py            # working tree
python scripts/check_secrets.py --history  # every commit, both directions
```

If it ever reports a real credential: rotate it first, then clean the history
(see [docs/security.md](docs/security.md)). Deleting the file in a new commit does
not unpublish the value - GitHub serves historical objects.

## Testing

```bash
pip install -r requirements.lock
pip install -r requirements-test.txt
python -m pytest -m "not postgres and not live"   # offline: no DB, no network
```

`-m postgres` and `-m live` need a disposable database and real credentials
respectively. Tests never send Telegram messages, never call a paid provider and
never touch production.

## Cloud deployment (manual, gated)

| Workflow | Trigger | Gate |
|---|---|---|
| `research.yml` | cron 08:00-22:00 IST | `vars.SCHEDULES_ENABLED == 'true'` |
| `digest.yml` | cron 22:35 IST | same variable |
| `backup.yml` | cron Sunday 23:00 IST | same variable |
| `maintenance.yml` | cron 06:45 IST | same variable |
| `tests.yml` | push / pull request | no credentials |
| `migrate-production.yml` | **manual only**, typed confirmation | `production` environment (required reviewers) + verified backup first |

Configure secrets under **Settings -> Secrets and variables -> Actions**.
Repository variables: `SCHEDULES_ENABLED` (leave unset until go/no-go). Provider
keys: `FREEAI_API_KEY`, `GEMINI_API_KEY`, `GROQ_API_KEY`, `SARVAM_API_KEY`
(paid), `XAI_API_KEY` (paid, optional). Database, Telegram, Drive and
`HEALTHCHECKS_PING_URL` are described in
[docs/deployment.md](docs/deployment.md). Never put a real value in a workflow
file, a test, an example or a report.

## Commands

| Command | Purpose |
|---|---|
| `ai-radar research` | full pipeline (what `research.yml` runs) |
| `ai-radar digest` | build/send the daily digest |
| `ai-radar backup` | export → upload → re-download → verify |
| `ai-radar backup --local-dir backups` | export → write → re-read → verify, without Drive |
| `ai-radar restore` | restore or `--verify-only` a backup into an isolated DB |
| `ai-radar maintenance` | staleness, queue review, pruning, quota report |
| `ai-radar heartbeat` | evaluate staleness from stored state |
| `ai-radar migrate` | apply SQL migrations (checksum-guarded) |
| `ai-radar doctor` | verify config and every live integration |
| `ai-radar sources --validate` | check the registry contract |
| `ai-radar notify --check` | validate the bot token and chat id read-only (`getMe`/`getChat`; sends nothing) |
| `ai-radar notify --test` | one Telegram test message (requires explicit approval) |
| `ai-radar collect` | collect without persisting (diagnostics) |

Exit codes: `0` success, `1` runtime failure, `2` configuration problem.

## Layout

```
.github/workflows/   research, digest, backup, maintenance, tests, migrate-production
config/              sources, scoring, categories, schedule policy, calibration
database/migrations/ idempotent SQL schema
src/ai_radar/        the monolith (see docs/architecture.md)
scripts/             evaluation, source probe, lock generation
tests/               unit, integration, failure, evaluation
docs/                architecture, setup, deployment, security, recovery, cost
```

## Documentation

- [**Public release audit**](artifacts/AI_RADAR_PUBLIC_RELEASE_AUDIT.md)
- [**Staging credential setup**](docs/STAGING_CREDENTIAL_SETUP.md)
- [**Final V1 acceptance report**](FINAL_V1_ACCEPTANCE_REPORT.md)
- [**Staging validation before production**](docs/staging-validation.md)
- [Pre-deployment audit checklist](docs/pre_deployment_audit.md)
- [Cloud integration checklist](docs/cloud_integration_checklist.md)
- [Final pre-deployment audit](docs/FINAL_PRE_DEPLOYMENT_AUDIT.md)
- [Repository audit and engineering plan](docs/project-audit.md)
- [Architecture](docs/architecture.md)
- [Source registry](docs/sources.md)
- [Windows setup](docs/setup-windows.md) · [Android management](docs/setup-android.md)
- [Cloud deployment](docs/deployment.md) · [Security](docs/security.md)
- [Backup and restore](docs/backup-restore.md) · [Recovery](docs/recovery.md)
- [Cost model](docs/cost-model.md) · [Final verification report](docs/final-report.md)

## Deliberate non-goals in V1

No LangChain or multi-agent framework, no vector search, no JavaScript-rendered
scraping, no paid API without an explicit key, a hard per-run cap and a rolling
token budget (Sarvam and Grok are opt-in and off by default), no local scheduler,
no exactly-once Telegram delivery claim. Each of these is either unnecessary for the requirements or impossible to
honour honestly.

## Cost

Target ₹0/month: GitHub Actions free minutes, Supabase free database, Gemini and
Groq free tiers, Google Drive free storage, healthchecks.io free checks. See
[docs/cost-model.md](docs/cost-model.md) for limits and what breaks first.
