# Final engineering verification report

> **Historical record (2026-11-23).** This report is superseded by
> [FINAL_PRE_DEPLOYMENT_AUDIT.md](FINAL_PRE_DEPLOYMENT_AUDIT.md) (2026-11-24).
> Test counts and statuses below reflect the earlier audit; do not use them as
> current evidence.

**Build:** AI Radar V1.0
**Verification date:** 2026-11-23 (UTC) / 2026-11-24 00:09 IST
**Environment:** Windows 11, Python 3.13.15, PowerShell 5.1, no database and no
provider credentials on this machine.

This report separates what exists from what has been proven. A feature is only
"verified" here if a command was run and its output is quoted.

## 1. Status summary

| Layer | Status | Basis |
|---|---|---|
| Source code, migrations, workflows | **Implemented** | full tree; `compileall` exit 0 |
| Unit + failure + end-to-end suite | **Tested locally** | 242 passed, 20 skipped, 3.30 s |
| Collectors vs real endpoints | **Tested against real services** | 24/24 sources, 436 entries, 2026-11-23 |
| PostgreSQL integration | **Not executed** | no `TEST_DATABASE_URL`; 16 tests skipped |
| Gemini / Groq / Telegram / Drive live contracts | **Not executed** | no credentials; 4 tests skipped |
| Cloud deployment | **Not deployed** | needs the owner's accounts and secrets |
| PC-independence acceptance test | **NOT VERIFIED** | cannot be claimed without the PC off |
| Documentation set | **Implemented** | `docs/` (11 documents, including this one) |

**The system is not yet a running cloud service.** It is a complete, locally tested
implementation that is ready to be deployed with the owner's credentials.

## 2. Evidence

### 2.1 Static and unit verification

```
> python --version
Python 3.13.15

> python -m compileall -q src
compileall exit=0

> python -m pytest tests -o addopts="" -q
242 passed, 20 skipped in 3.30s
```

The 20 skips are not passes:

| Skipped | Count | Reason |
|---|---|---|
| `tests/integration/test_postgres.py` | 16 | `TEST_DATABASE_URL` is not set on this machine |
| Gemini contract | 1 | `GEMINI_API_KEY` not set |
| Groq contract | 1 | `GROQ_API_KEY` not set |
| Telegram contract | 1 | bot token / chat id not set |
| Google Drive contract | 1 | OAuth credentials not set |

### 2.2 Configuration verification

```
> python -m ai_radar doctor
[PASS] configuration    24 sources, 7 categories, confidence threshold 0.57 (calibrated)
[PASS] source registry  structure valid
[PASS] schedule policy  workflow crons match config/settings.yaml
[FAIL] database         DATABASE_URL not set
[WARN] Gemini           API key not set
[WARN] Groq             API key not set
[FAIL] telegram         bot token / chat id not set
[WARN] google drive     OAuth client / refresh token not set
[WARN] heartbeat        HEALTHCHECKS_PING_URL not set
exit=2
```

This is the expected output on a machine without credentials, and it is why
`doctor` exits `2` until the account is configured.

### 2.3 Source registry and live collection

```
> python -m ai_radar sources --validate
{"total": 24, "enabled": 24,
 "coverage": {"ai_models": 5, "ai_coding_tools": 4, "ai_agents": 3,
              "image_video_ai": 4, "audio_speech_ai": 3,
              "productivity_tools": 3, "ai_industry_updates": 2},
 "problems": []}
```

```
> python scripts/check_sources.py
24/24 sources healthy, 436 entries collected
```

Every source answered with HTTP 200 on 2026-11-23. One source
(`hf_image_models`) responded correctly but yielded **0 entries** because every
candidate was below the substance gate (3 likes / 200 downloads); that is counted
as "healthy but quiet", not as success in disguise.

### 2.4 End-to-end pipeline

`tests/integration/test_pipeline.py` drives COLLECT → VERIFY → ANALYZE → SAVE →
SEND → simulated failure → recover against fakes with the real pipeline code. It
passes locally. It does **not** prove the real Telegram or Postgres contracts; those
are the skipped tests above.

## 3. Defects found and fixed during this verification pass

These were not cosmetic. Each would have failed in production, and each now has a
regression test.

| # | Defect | Consequence | Fix | Test |
|---|---|---|---|---|
| 1 | No `src/ai_radar/__main__.py` | every workflow calls `python -m ai_radar` and would fail with "No module named ai_radar.__main__" | added the module delegating to `cli.main` | CLI smoke |
| 2 | CI never made `src/` importable | same failure even with `__main__.py`; `pip install -e .` was not run in any workflow | `PYTHONPATH: src` on every job | workflow review; verified locally with the same env |
| 3 | Drive token exchange sent JSON under `application/x-www-form-urlencoded` | Google rejects it with `400 invalid_request`, so no backup could ever authenticate | form-encode the body with `send_raw` | `test_drive.py::test_token_exchange_is_form_encoded_not_json` |
| 4 | Drive upload assumed a user-supplied folder id | `drive.file` cannot write into a folder the app did not create → `404` on the first backup | folder id optional; app finds or creates its own "AI Radar Backups" folder | `test_upload_creates_an_app_owned_folder_when_none_is_configured` |
| 5 | Backup checksum was stored only in the database, never with the file | restore compared against an empty string and **silently skipped** integrity checking | checksum in Drive `appProperties`, `tables_sha256` inside the payload, restore refuses a backup with no checksum | 5 new tests in `test_backup_and_lock.py` |
| 6 | Verification probe cache was keyed by domain | a dead release URL inherited the publisher's live home page and was marked reachable → false CONFIRMED | cache per exact URL, with a 120-probe per-run budget that degrades to "unprobed" | `test_verifier.py` (11 cases, incl. per-URL and budget) |
| 7 | Logger bound `sys.stderr` at configuration time | any later log line raised `ValueError: I/O operation on closed file` after a stream was replaced or closed — a logging failure could abort a run | stream resolved per write, write errors swallowed | full suite in one process; logging tests |
| 8 | `pytest` could not import `ai_radar` in CI | `tests.yml` installs only `requirements.lock`, so the suite would fail at collection before running a single test | `pythonpath = ["src"]` in `pyproject.toml` | suite run with the package uninstalled passed (236 at that point) |

Also added: `scripts/google_oauth_authorize.py` (obtains the Drive refresh token
without writing it to disk), `.github/dependabot.yml`, and the nine missing
documents the README already promised.

## 4. What is implemented, by area

| Area | Evidence in code |
|---|---|
| 24-source registry, 7 categories, A/B/C tiers | `config/sources.yaml`, `models/source.py` |
| RSS, GitHub releases, Hugging Face collectors | `collectors/` |
| 11-stage pipeline with total discard of low confidence | `operations/pipeline.py` |
| Deterministic dedupe + database uniqueness | `processing/dedupe.py`, migrations |
| Evidence verification and pricing classification | `verification/verifier.py`, `processing/pricing.py` |
| Gemini → Groq → deterministic with runtime model resolution | `intelligence/` |
| Confidence (measured) separated from priority (0–100, weights 30/25/20/15/10) | `processing/confidence.py`, `scoring.py` |
| 9 memory tables + runtime state, transactions, advisory lock | `database/migrations/`, `storage/` |
| Telegram delivery state machine with `UNKNOWN` | `notifications/`, `deliveries` |
| Daily digest, seven sections | `reporting/digest.py` |
| 24 h missed-run recovery, bounded backlog | `operations/recovery.py` |
| Drive backup, checksum chain, retention, isolated restore | `operations/backup.py`, `restore.py`, `drive.py` |
| Independent heartbeat | `operations/heartbeat.py` |
| Security controls | `utils/redact.py`, `utils/net.py`, `intelligence/prompts.py` |
| Workflows: research, digest, backup, maintenance, tests, migrations | `.github/workflows/` |

## 5. Not verified — stated plainly

| Claim | Why it is not verified | What would verify it |
|---|---|---|
| "Runs in the cloud with the PC off" | never deployed | §10 of `deployment.md`, executed with the PC shut down |
| "PostgreSQL persistence works" | no database on this machine | `TEST_DATABASE_URL` + `pytest -m postgres` |
| "Gemini analysis works against the real API" | no key | `GEMINI_API_KEY` + `pytest -m live` |
| "Groq fallback works against the real API" | no key | `GROQ_API_KEY` + `pytest -m live` |
| "Telegram alerts arrive" | no bot token | `ai-radar notify --test` |
| "Drive backup and restore work" | no OAuth grant | `ai-radar backup`, then `ai-radar restore --verify-only` |
| "Heartbeat pages when stale" | no healthchecks check | create a check, pause the schedule, observe |
| "Confidence threshold is calibrated" | 24 hand-labelled cases, not a study | larger labelled set; the file says so itself |

## 6. Blocked by the owner (not by code)

| # | Blocker | Needed for |
|---|---|---|
| 1 | Supabase project + `DATABASE_URL` | migrations, persistence, Postgres tests |
| 2 | Telegram bot + chat id | alert tests, daily digest |
| 3 | Gemini and Groq API keys | live analysis tests |
| 4 | Google Cloud OAuth client + consent + refresh token | backup, restore drill |
| 5 | healthchecks.io check | independent monitoring |
| 6 | GitHub repository + secrets, schedules enabled | cloud execution |
| 7 | PC shut down for a full window | PC-independence acceptance |

None of these should be pasted into a chat or a source file. Use the GitHub Secrets
UI or `gh secret set`, as described in `deployment.md` §7.

## 7. Known limitations (by design, not defects)

- **No exactly-once Telegram delivery.** The Bot API does not offer it. Ambiguous
  sends are recorded as `UNKNOWN` and are not retried automatically.
- **Verification cannot see behind JavaScript, logins or paywalls.** Such fields
  stay `UNVERIFIED`; the alert shows "Pricing unverified" rather than guessing.
- **The confidence threshold (0.57) comes from 24 labelled cases.** It is a seed
  calibration, labelled as such in `config/confidence_thresholds.json`.
- **Analysis is capped at 40 per run.** On a very busy launch day, items beyond the
  cap are not persisted and are re-collected next run; if a feed rotates them out
  first, they are lost. The cap is logged.
- **The Hugging Face source trades recall for precision** via a substance gate; it
  currently yields nothing for image models.
- **Recovery is bounded to 24 hours.** Older gaps are not re-collected.
- **Availability probes are bounded to 120 URLs per run.** Beyond that, items are
  treated as unprobed (lower confidence, no alert) rather than silently confirmed.
- **Scheduled workflows in a public repository are disabled after 60 days without
  repository activity.** The heartbeat will notice; a monthly keepalive commit is
  the fix.

## 8. Reproduction

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.lock
pip install -e .
python -m pytest tests -o addopts="" -q
python -m ai_radar sources --validate
python scripts/check_sources.py
python -m ai_radar doctor
```

Expected: `242 passed, 20 skipped`, `problems: []`, `24/24 sources healthy`, and a
`doctor` report whose only failures are the integrations you have not configured.

## 9. Verdict

The implementation is complete against the brief and has been exercised locally,
including real collection from all 24 sources. It is **not** a verified cloud
service: deployment, live provider contracts, database integration and the
PC-independence test are outstanding and blocked on credentials and accounts.

I will not describe this as "working in production" until the PC-off test has been
run and recorded below.

### PC-independence record

| Check | Timestamp | Result |
|---|---|---|
| PC powered off | — | not performed |
| Research runs occurred | — | not verified |
| Discoveries persisted | — | not verified |
| Telegram delivered | — | not verified |
| Heartbeat updated | — | not verified |
| Backup validated | — | not verified |

Fill this table in only after the system has run with the PC off.
