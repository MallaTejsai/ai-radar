# AI Radar V1.0 — Final Acceptance Report

**Report date:** 2026-11-24
**Baseline commit:** `97ca290` (working tree also contains this session's changes — see §12)
**Environment used for evidence:** Windows 11, Python 3.12.10, clean venv built from `requirements.lock`
**Owner:** you. **Author:** engineering handoff.

This report states only what has been executed. Anything not executed is labelled
NOT TESTED / BLOCKED / UNVERIFIED, and no status here is inferred from the
existence of code.

Status vocabulary: **PASSED** · **VERIFIED** · **PARTIAL** · **NOT TESTED** ·
**BLOCKED** · **UNVERIFIED** · **NOT VERIFIED**.

---

## 0. Declaration

| Claim | Status | Meaning |
|---|---|---|
| **Code complete for V1 scope** | **YES** | All brief features are implemented, wired and covered by tests. |
| **Integration verified** | **PARTIAL** | Database, sources and free.ai are verified against the real services. Telegram, Gemini, Groq and Drive are not. |
| **Production deployed** | **NO** | No Git remote exists; nothing has been pushed, no workflow has ever run in the cloud. |
| **PC-independent operation** | **NOT VERIFIED** | Requires a real cloud run with the PC powered off. Not attempted. |
| **Safe to push to GitHub now** | **YES** | Schedules are gated off by default (§6) and manual dispatch cannot notify (§7). |
| **Safe to run `research --dry-run` against production** | **NO — superseded** | It writes production rows and spends free.ai tokens. Validate on staging instead: §3A. |
| **Safe to run the full pipeline on staging** | **READY, NOT EXECUTED** | Procedure, guards and tests are in place; needs a second Supabase project — §3A. |

`PC_INDEPENDENCE_STATUS = NOT_VERIFIED`
`PRODUCTION_DEPLOYMENT_STATUS = NOT_DEPLOYED`

---

## 1. Evidence collected at this commit

| # | Check | Command | Result |
|---|---|---|---|
| 1 | Full offline suite | `python -m pytest tests -o addopts="" -q` | **341 passed, 20 skipped, 3.97 s** (326 before the staging work) |
| 2 | CI-equivalent selection | `pytest -m "not postgres and not live"` (strict markers) | **PASSED**, exit 0 |
| 3 | Byte compilation | `python -m compileall -q src` | exit 0 |
| 4 | Source registry | `ai-radar sources --validate` | 24 sources, 7 categories, `problems: []` |
| 5 | Config + DB + migrations + cron drift | `ai-radar doctor --database-only` | **5/5 PASS** (3 migrations applied) |
| 6 | Schema + write paths (non-destructive) | `python scripts/verify_database.py` | **14/14 PASS**, 13 tables, 0 leftover rows |
| 7 | Live collection, all sources | `ai-radar collect` | **24/24 sources, 436 entries** |
| 8 | Secret scan over `git ls-files` | pattern scan (token / API key / PEM / DSN) | 0 real secrets; only synthetic fixtures |
| 9 | Git state | `git rev-parse HEAD` | `28a87a9` + the staging commit in §12, branch `main`, **no remote, no tags** |
| 10 | Staging guard — no staging URL | `AI_RADAR_STAGING=1 ai-radar doctor --database-only` | `[FAIL] staging target` "not set", **exit 2**, no connection opened |
| 11 | Staging guard — production endpoint copied in | same, `AI_RADAR_STAGING_DATABASE_URL` = `DATABASE_URL` | `[FAIL] staging target` "same server and user", **exit 2** |
| 12 | Staging guard — separate project, unreachable host | same, staging DSN on `staging.invalid` | `[PASS] staging target` then `[FAIL] database` — fails **closed**, exit 2 |
| 13 | Staging guard on the run path | `AI_RADAR_STAGING=1 ai-radar research` with the production endpoint | refused before any collection or write, **exit 2** |

The 20 skipped tests are listed with their prerequisites in §8. They are not
passes and are not counted as such.

---

## 2. Verified completed functionality

### A. Research engine — **PASSED** (collection/verification), **NOT TESTED** (persistence in a real run)

| Capability | Status | Evidence |
|---|---|---|
| Seven categories monitored | PASSED | `sources --validate`: 5/4/3/4/3/3/2 sources across all seven |
| 24 curated sources (15–25 required) | PASSED | live probe 24/24, 436 entries |
| RSS + GitHub REST + HF Hub, no JS scraping | PASSED | three collectors, registry methods |
| Per-source failure isolation | PASSED | `collect_source` boundary + failure tests |
| Timeouts, retries, backoff, `Retry-After`, size caps | PASSED | `utils/net.py`, unit tests |
| ETag / Last-Modified preserved and used | PASSED | RSS + both JSON collectors; 304 treated as unchanged |
| Checkpoint advances only after durable writes | PASSED | `_advance_checkpoints` after persist; failure path leaves it untouched |
| Deterministic dedupe before AI calls | PASSED | canonical key + title fingerprint + DB uniqueness |
| Independent evidence verification (probe + date + corroboration) | PASSED | `verification/verifier.py`, per-URL probe cache, 120-probe budget |
| Pricing never guesses; trial ≠ free; weights ≠ open source | PASSED | `processing/pricing.py` + policy tests |
| End-to-end persistence of qualified discoveries | **NOT TESTED** | requires a real `research` run (§3) |

### B. Intelligence and evaluation — **PASSED** (logic), **PARTIAL** (live)

| Capability | Status | Evidence |
|---|---|---|
| Weights 30/25/20/15/10, 0–100, 1/5–5/5 mapping | PASSED | `scoring.py`, scoring tests |
| Confidence separate from priority; 0.57 operating threshold | PASSED | `confidence.py`, `config/confidence_thresholds.json` (explicitly *not* statistically calibrated) |
| Low-confidence discarded completely | PASSED | pipeline stage 8; no row, no alert, no digest entry |
| free.ai preferred, budget-capped | **provider PASSED live; in-pipeline wiring FIXED** | one authenticated call, HTTP 200, 571 tokens (`scripts/verify_freeai.py`, which passes no budget lookup). The pipeline's budget lookup was miswired and disabled the provider on every run — found by the staging run on 2026-11-24 and fixed; end-to-end free.ai in the pipeline remains **NOT TESTED** until the next staging run |
| Rolling 24 h budget 25 000 tokens, ≤2 calls/run, 402 = quota, no retry | PASSED | `intelligence/freeai.py`, budget tests |
| Gemini → Groq → deterministic fallback | PASSED (unit) | router/circuit-breaker tests; **live NOT TESTED** (no keys) |
| Usage accounting to `provider_usage` | PASSED (unit + schema) | live rows not yet produced |
| No paid API exists in the codebase | PASSED | no paid adapter present |

### C. Database — **PASSED**

| Capability | Status | Evidence |
|---|---|---|
| 3 migrations, idempotent, checksum-guarded | PASSED | second run returns `[]`; checksum drift raises |
| 13 tables, columns, indexes, unique + FK actions | PASSED | verifier 14/14 |
| Atomic claim (`FOR UPDATE SKIP LOCKED`) | PASSED | verifier probe (`attempts=1`) |
| Duplicate canonical-key rejection | PASSED | verifier probe |
| Durable checkpoint update | PASSED | verifier probe (rolled back) |
| No leftover probe rows | PASSED | `leftover rows=0` |
| Session-pooler safety (refuses port 6543) | PASSED | DSN guard + 5 tests |

The production database has **not** been modified or reset. All verification
writes were rolled back.

### D. Notifications — **PARTIAL / BLOCKED**

| Capability | Status | Evidence |
|---|---|---|
| Message rendering, escaping, 3 800-char cap | PASSED | format tests incl. hostile content and quote-in-URL |
| Delivery state machine (`PENDING→SENDING→SENT/RETRY_WAIT/UNKNOWN`) | PASSED | dispatcher + failure tests |
| `UNKNOWN` never auto-retried | PASSED | dispatcher logic + tests |
| 429 honours `retry_after` | PASSED | `TelegramError.retry_after` handling + tests |
| Token redaction in logs and DB | PASSED | observed in a real redacted error; tests |
| Max 5 alerts/run, priority ≥ 3/5, confidence gate, 48 h age cap | PASSED | three-layer cap + gate tests |
| Read-only token/chat validation | **PASSED (new)** | `notify --check` (getMe + getChat, sends nothing) + 3 tests |
| Telegram authentication | **UNVERIFIED** | one `getMe` timed out at the HTTP layer; no verdict |
| One approved live delivery | **NOT TESTED** | no chat id; not sent |
| Daily digest | PASSED (logic) / **NOT TESTED** (delivery) | digest tests; no live send |

### E. Backup — **BLOCKED** (code PASSED)

Logical export inside a repeatable-read transaction, gzip JSON, SHA-256 stored in
Drive `appProperties` and in the payload, re-download verification, retention only
after a newer backup validates, restore refuses the production DSN and requires a
separate database. All unit-tested against a mock transport. **No live upload,
download, checksum or restore has occurred.**

### F. Cloud independence — **NOT VERIFIED**

Workflows exist, are schema-valid and are validated by tests (crons, secrets,
permissions, concurrency, gate). No workflow has ever executed in the cloud.

### G. Security — **PASSED**

No secret in the tracked tree (only synthetic fixtures), `.env` ignored, redaction
verified against a real failure, parameterised SQL only, SSRF controls, fenced
untrusted content, `contents: read` on every workflow, no write-scoped workflow, no
secret echoed in any workflow, destructive DB suite guarded by database name.

### H. Documentation — **PASSED (updated this session)**

README status is evidence-backed; deployment runbook documents the gate; the
checklist uses the read-only Telegram check first; this report is the acceptance
record.

---

## 3. `research --dry-run` — exactly what it does

**It is not read-only.** `--dry-run` suppresses *notification delivery only*. The
full pipeline still executes against the production database.

### 3.1 Production database writes

| Table | Operation | Why |
|---|---|---|
| `sources` | UPSERT all 24, then UPDATE per source | registry sync, `last_checkpoint`, `etag`, `records_collected`, `health_status`, `consecutive_failures` |
| `workflow_runs` | INSERT then UPDATE | the run report |
| `discoveries` | UPSERT | every survivor above the confidence threshold |
| `discovery_sources` | UPSERT | evidence rows |
| `analysis_results` | UPSERT | model output |
| `pricing_history` | INSERT on change | pricing timeline |
| `analysis_cache` | UPSERT | prompt-fingerprint cache |
| `provider_usage` | INSERT per attempt | metering (including deterministic attempts) |
| `system_state` | UPSERT `heartbeat` | staleness bookkeeping |

**Not written in this mode:** `deliveries` (dispatcher is never constructed),
`daily_digests`, `backup_manifests`. **No Telegram call, no Drive call, no
migration, no TRUNCATE, no DELETE.**

It also takes the `ai_radar_pipeline` session advisory lock for the run, and
performs availability probes (HEAD/GET, capped at 120 URLs).

### 3.2 External AI calls

| Provider | Calls in one dry run | Notes |
|---|---|---|
| free.ai | **0–2** (`FREEAI_MAX_CALLS_PER_RUN`, default 2) | ~1 000–1 800 tokens each; the rolling 24 h budget (25 000) is checked first from `provider_usage` |
| Gemini | **0** | key is a placeholder → treated as unconfigured |
| Groq | **0** | key is a placeholder → treated as unconfigured |
| Deterministic analyser | the remainder | no network, no cost |

So an approved dry run spends at most **2 free.ai calls (~2–4 K tokens)** and no
other provider budget.

### 3.3 The important consequence

Because the dry run **persists** discoveries and advances source checkpoints, a
later run treats those items as already known and **will never alert on them**
(alerts require `created == true`). Therefore:

- the first real alert comes from discoveries collected **after** the dry run;
- do not expect the dry run's own rows to produce a Telegram message;
- if you want the first collected batch to alert, run the real pass first and the
  dry run afterwards.

This is documented in `docs/deployment.md` §9 and in the CLI help text. No
behaviour was changed.

### 3.4 Status

**NOT EXECUTED — and no longer the recommended path.** See §3A: the same
validation can be obtained on a disposable database with no production writes and
no risk of suppressing the first real alert.

---

## 3A. Safest validation path: staging (prepared, not executed)

The complete runbook is [`docs/staging-validation.md`](docs/staging-validation.md).
This section is the summary of what changed and why.

### 3A.1 Decision

The production dry run is replaced by a **staging run**: the same code path, the
same writes, the same single live provider, against a database that is not
production.

### 3A.2 Four guards, three of them structural

| # | Guard | Effect |
|---|---|---|
| 1 | Staging reads `AI_RADAR_STAGING_DATABASE_URL`, never `DATABASE_URL` | production is never read, so a forgotten override cannot fall back to it |
| 2 | Endpoint identity refusal (host + port + user) | a staging URL that is the production project is refused before connecting. Database names are not used: Supabase names every database `postgres` |
| 3 | `Settings` blanks Telegram, Gemini, Groq, healthchecks and all Drive credentials | no message, no other provider, no monitoring pollution, and **no `backup` run can delete production Drive backups via retention** |
| 4 | `doctor` pre-flight refuses before opening a connection | a misconfigured staging run exits 2 instead of touching any database |

Guards 1 and 3 are enforced by construction, not by operator discipline: with
`AI_RADAR_STAGING=1`, an absent staging URL is a hard error even though production
is configured and working.

### 3A.3 What it exercises

Collection (24 sources) → normalization → in-batch and stored-memory
deduplication → evidence verification and verdicts → live free.ai analysis (≤2
calls) → deterministic fallback → scoring and priority → persistence
(`discoveries`, `discovery_sources`, `analysis_results`, `pricing_history`,
`sources`, `workflow_runs`, `provider_usage`, `system_state`, `analysis_cache`).

A **second** run shows deduplication: `after_dedup` drops the keys already stored
as discoveries plus in-batch duplicates. It does not go to zero while the feeds
keep publishing, and only the first `max_analyses_per_run` (default 40) survivors
are analysed and persisted — so `persisted_new > 0` on a live feed is expected, not
a failure. Analyses already in `analysis_cache` cost no provider calls.

### 3A.4 Requirements and cost

| Item | Value |
|---|---|
| Required from you | one Supabase project (`ai-radar-staging`) + its **session-pooler** DSN |
| Required variables | `AI_RADAR_STAGING=1`, `AI_RADAR_STAGING_DATABASE_URL`, existing `FREEAI_API_KEY` |
| free.ai | ≤ 2 calls, ~2–4 K tokens on run 1; **0** on run 2 |
| Gemini / Groq | **0 calls — structurally unreachable** |
| Telegram / Drive | **0 messages, 0 operations — structurally unreachable** |
| Production | **0 writes, 0 reads changed** |
| Time / money | ~1–2 min per run / ₹0 |

### 3A.5 Constraints worth knowing

- Supabase Free allows **2 active projects**. Staging takes the second, so the
  restore drill and the 16 destructive tests must reuse this same project
  **after** the pipeline validation, since they truncate every table.
- `AI_RADAR_STAGING` must be set in the shell only, never in `.env`.
- Still **not** covered by staging: GitHub Actions scheduling, heartbeats, real
  Telegram delivery, Drive backup, PC independence.

### 3A.6 Executed on 2026-11-24 — staging is live

Staging project `<staging-ref>` (`ap-southeast-2`) was created, migrated and run
three times. Production `<production-ref>` (`ap-northeast-1`) was never written to.
Project refs are deliberately not recorded here; they belong in the operator's
password manager, not in the repository.

| Step | Result |
|---|---|
| `doctor --database-only` before migration | `[PASS] staging target` (staging vs production masked, different endpoints), `[FAIL] migrations schema not applied yet`, exit 2 |
| `migrate` run 1 | `001_initial_schema`, `002_runtime_state`, `003_freeai_provider` applied, exit 0 |
| `migrate` run 2 | `{"applied": []}`, exit 0 — idempotent |
| `doctor --database-only` after migration | **6/6 PASS**, exit 0 |
| Schema | 13 public tables |
| `research` run 1 | completed, 24/24 sources, 661 → 320 after dedup, 320 verified, 23 low-confidence discarded, 16 qualified, 17 persisted, **0 alerts**, 0 source failures |
| `research` run 2 | completed, 24/24 sources, 175 → 112 after dedup, 112 verified, 40 qualified, 40 persisted, **0 alerts** |
| Persistence | 57 discoveries (45 `CONFIRMED`, 12 `PARTIAL`), 57 evidence rows, 57 analysis rows, 57 pricing rows, all 24 sources `healthy` |
| Notifications | `deliveries` 0 rows, `daily_digests` 0 rows — staging mode kept Telegram unreachable |
| Production | not connected at any point; no read, no write |

**Defect found by this run (fixed in the working tree).** The pipeline passed
`repo.provider_tokens_last_24h` — which requires a provider argument — as the
free.ai budget lookup, so every budget check raised `TypeError`, the provider was
disabled for the run, and all 80 analyses fell back to deterministic while the run
still reported `completed`. `provider_usage` recorded 4 failed `freeai` attempts
(2 per run) and 80 `deterministic` rows. The lookup is now bound to
`Provider.FREEAI.value` by `cli._freeai_usage_lookup`, with a regression test.

**Consequence for acceptance:** the pipeline has never completed a free.ai
analysis. A third staging run is required to prove that path end to end; until
then the free.ai-in-pipeline row in §2.B stays **NOT TESTED**.

### 3A.7 Status

**EXECUTED (two runs).** free.ai end-to-end remains the only open item from this
phase. No deployment, push, schedule or production change was made.

---

## 4. Remaining blockers

| # | Blocker | Owner action | Blocks |
|---|---|---|---|
| 1 | `TELEGRAM_CHAT_ID` not configured | message the bot, read `result[0].message.chat.id` | alerts, digest, acceptance D |
| 2 | Telegram authentication unknown (`getMe` timed out) | run `notify --check` on another network or in a manual dispatch | acceptance D |
| 3 | No Git remote / no `gh` CLI | create the repository, authenticate `gh` | everything cloud |
| 4 | Gemini / Groq keys absent | optional: add keys | fallback tier live test |
| 5 | No Google OAuth grant | Cloud console + `scripts/google_oauth_authorize.py` | backup, restore drill |
| 6 | No disposable database | a second Supabase project named e.g. `ai-radar-staging` (also serves §3A and the restore drill) | staging validation, 16 destructive tests, restore drill |
| 7 | No `HEALTHCHECKS_PING_URL` | create the check | external monitoring |
| 8 | No PC-off window | shut the PC down | PC independence |
| 9 | Confidence threshold is a 24-case seed calibration | larger labelled set | statistical claim only |

---

## 5. Exact manual setup steps

Run in this order. Nothing below has been done for you.

### Step 1 — Telegram (no message sent)

```powershell
# 1a. BotFather: /newbot -> copy the token. Send the bot ONE message in a PRIVATE chat.
$env:TELEGRAM_BOT_TOKEN = "<token>"

# 1b. Validate the token read-only (getMe). Sends nothing.
python -m ai_radar notify --check
#    PASS -> {"token": "PASS", "bot": {...}, "chat": "NOT_CONFIGURED"}, exit 2
#    FAIL -> kind: unauthorized  => token wrong/revoked; /token in BotFather
#    FAIL -> kind: unavailable   => HTTP/network timeout; NOT a token verdict.
#           Retry from another network (mobile hotspot), or later inside a
#           GitHub Actions manual dispatch. Do not retry in a loop.

# 1c. Read the chat id (read-only Bot API call, sends nothing)
Invoke-RestMethod "https://api.telegram.org/bot$env:TELEGRAM_BOT_TOKEN/getUpdates" |
  ConvertTo-Json -Depth 8      # take result[0].message.chat.id
$env:TELEGRAM_CHAT_ID = "<numeric id, negative for groups>"

# 1d. Validate token AND chat id read-only (getMe + getChat). Sends nothing.
python -m ai_radar notify --check
#    PASS -> {"token":"PASS","chat":{"status":"PASS","type":"private",...}}, exit 0

# 1e. ONLY after approving a live message:
python -m ai_radar notify --test        # sends exactly one message
```

If step 1d reports `kind: bad_request`, the chat id is wrong or the bot was blocked
— that is a configuration error, not a transient failure.

### Step 2 — Google Drive (backup)

Follow `docs/backup-restore.md` §2 exactly:

1. Cloud project → enable **Google Drive API**.
2. OAuth consent screen: External, scope `.../auth/drive.file`, add yourself.
3. **Publishing status → In production** (Testing-mode refresh tokens expire in 7 days).
4. Credentials → **OAuth client ID → Desktop app**.
5. On this PC:

```powershell
$env:GOOGLE_OAUTH_CLIENT_ID = "<id>"
$env:GOOGLE_OAUTH_CLIENT_SECRET = "<secret>"
python scripts/google_oauth_authorize.py      # prints the refresh token ONCE
```

6. Keep `GOOGLE_DRIVE_FOLDER_ID` **empty** — `drive.file` cannot see a folder you
   created by hand; the app creates `AI Radar Backups` itself.
7. Never paste the refresh token into chat, an issue or a commit.

### Step 3 — GitHub repository, secrets and the schedule gate

```powershell
git remote add origin https://github.com/<you>/ai-radar.git
git push -u origin main
gh auth login

gh secret set DATABASE_URL            # session pooler, port 5432, sslmode=require
gh secret set TELEGRAM_BOT_TOKEN
gh secret set TELEGRAM_CHAT_ID
gh secret set FREEAI_API_KEY
gh secret set GEMINI_API_KEY          # optional
gh secret set GROQ_API_KEY            # optional
gh secret set HEALTHCHECKS_PING_URL   # optional but recommended
gh secret set GOOGLE_OAUTH_CLIENT_ID
gh secret set GOOGLE_OAUTH_CLIENT_SECRET
gh secret set GOOGLE_OAUTH_REFRESH_TOKEN
# optional, for the restore drill only:
gh secret set RESTORE_DATABASE_URL

gh variable list                       # SCHEDULES_ENABLED must NOT be present
```

`gh secret set` prompts without echoing. Verify names with `gh secret list`;
values are never displayed.

### Step 4 — healthchecks.io

Create one check: **Period 1 hour, Grace 11 hours**. Copy its ping URL into
`HEALTHCHECKS_PING_URL`. The workflow pings `/fail` when a run ends `FAILED`.

### Step 5 — First production sequence (manual dispatches only)

| # | Action | Expected |
|---|---|---|
| 1 | `tests` workflow on `main` | green; migrations applied |
| 2 | dispatch `research` with `dry_run = true` (default) | `completed`; rows in `discoveries`; **no** message |
| 3 | dispatch `research` with `dry_run = false` | 0–5 alerts; `deliveries.status = SENT` with `telegram_message_id` |
| 4 | dispatch `digest` with `dry_run = true` | rendered digest in the log, no message |
| 5 | dispatch `digest` with `dry_run = false` | one message |
| 6 | dispatch `backup` (real) | `backup_manifests.status = VALIDATED` |
| 7 | `restore --verify-only` | `"verified": true` |
| 8 | dispatch `maintenance` | no stale warning |

Only after steps 1–8: `gh variable set SCHEDULES_ENABLED --body true`.

### Step 6 — Restore drill (disposable database only)

```powershell
# a SECOND, empty database; its DSN differs from DATABASE_URL in host/path/port
$env:RESTORE_DATABASE_URL = "postgresql://...:5432/postgres?sslmode=require"
python -m ai_radar restore                 # refuses DATABASE_URL as a target
$env:TEST_DATABASE_URL = "<same disposable database>"
python -m pytest tests/integration/test_postgres.py -m postgres -q
```

The destructive suite refuses any database whose name does not contain
`test`, `ci`, `scratch` or `sandbox`.

### Step 7 — PC-off acceptance

See §9.

---

## 6. Deployment prerequisites

- [ ] `97ca290` + this session's commit pushed to `main`
- [ ] `tests` workflow green on `main` (Python 3.12 **and** 3.13 matrix)
- [ ] `DATABASE_URL` set; `ai-radar migrate` returns `[]` (already true today)
- [ ] `verify_database.py` → `RESULT: PASS`
- [ ] All required secrets present (`gh secret list`)
- [ ] `SCHEDULES_ENABLED` **absent** until the sequence in §5 passes
- [ ] `.env` never committed; secret scan clean
- [ ] Telegram `notify --check` → token PASS and chat PASS
- [ ] Confirmed free-tier accounts only; no billing enabled anywhere
- [ ] Repo public (₹0 Actions) **or** cadence adjusted deliberately (§7)

---

## 7. Monthly quota and cost analysis

### 7.1 GitHub Actions minutes (the binding constraint)

Cron is unchanged from the approved cadence: `30 2-16 * * *` = 15 runs/day.

| Workflow | Runs/month | Minutes/run | Minutes/month |
|---|---|---|---|
| research | 450 | 4–6 | **1 800–2 700** |
| digest | 30 | 1–2 | 30–60 |
| maintenance | 30 | 1–2 | 30–60 |
| backup | ~4.3 | 2–3 | 9–13 |
| tests | per push (2 matrix legs) | 1–2 each | small |
| **Total** | | | **≈ 1 870–2 840** |

- **Public repository:** standard runners are free → ₹0 by construction.
- **Private repository, Free plan:** **2 000 minutes/month**. At 6 min/run the
  cadence **exceeds** the allowance (~2 700); at 4 min/run it fits with almost no
  headroom for tests. GitHub rounds each job **up** to a whole minute, and the pip
  `Install` step is the largest fixed cost (~40–60 s cold, ~15 s warm with `cache: pip`).
- **Sustainable private-repo option (not applied):** every two hours → 8 runs/day
  = 240 runs/month ≈ 960–1 440 minutes, leaving 560–1 040 for everything else.
  Exact change, in **both** places or `doctor` fails:

  ```
  config/settings.yaml  ->  schedule.research.cron_utc: "30 2-16/2 * * *"
  .github/workflows/research.yml -> cron: "30 2-16/2 * * *"
  ```

  I have **not** changed it. Choose public, or approve this change.

Check your own usage: `gh api /users/{user}/settings/billing/actions`.

### 7.2 Other free tiers

| Service | Allowance | Expected use | Headroom |
|---|---|---|---|
| Supabase | 500 MB, 5 GB egress; **pauses after 1 week idle**; no auto-backups | 3–5 KB per discovery; 60/day ≈ 90 MB/year | years, but no auto-backup — the weekly Drive backup is the only safety net |
| free.ai | 30 000 tokens/day, 10 req/min | ≤2 calls/run, 25 000 rolling budget | budget-limited, not call-limited |
| Gemini / Groq | per-model RPD/TPM | fallback only | depends on a busy day |
| Google Drive | 15 GB shared | 12 backups × tens–hundreds KB | trivial |
| healthchecks.io | 20 checks, 100 log entries | 1 check, 15 pings/day | 1 week of logs |

**₹0/month is a target, not a guarantee.** It holds if the repository is public (or
the cadence is halved), no billing is enabled, and no free tier is upgraded. There
is no code path that can create a paid resource.

---

## 8. Integration test requirements

The 20 skipped tests and what unblocks each:

| Tests | Marker | Needs |
|---|---|---|
| 16 × `tests/integration/test_postgres.py` | `postgres` | `TEST_DATABASE_URL` pointing at a **disposable** database (name contains test/ci/scratch/sandbox). Truncates every table. |
| 1 × Gemini contract | `live` | `GEMINI_API_KEY`, no billing |
| 1 × Groq contract | `live` | `GROQ_API_KEY`, no billing |
| 1 × Telegram contract | `live` | `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` (and approval: it sends) |
| 1 × Google Drive contract | `live` | OAuth client + refresh token |

Commands:

```powershell
python -m pytest tests -o addopts="" -q                      # 326 passed, 20 skipped (today)
$env:TEST_DATABASE_URL="..."; python -m pytest -m postgres -q
python -m pytest -m live -q                                   # real credentials required
```

Additional requirements:

- the destructive suite must never be pointed at production; the name guard is a
  safety net, not a substitute for care;
- a restore must be rehearsed only into a database whose host/path/port differ from
  `DATABASE_URL`;
- `provider_usage` and `analysis_cache` grow with live runs; `maintenance` prunes
  them (45 / 120 days).

---

## 9. PC-off verification procedure

Do not start until §5 steps 1–8 passed and `SCHEDULES_ENABLED=true` is set.

1. Confirm three consecutive scheduled `research` runs succeeded in the cloud.
2. **Shut the PC down** — power off, not sleep, not hibernate.
3. Leave it off across **at least three scheduled research slots and one digest**.
4. From the phone (`docs/setup-android.md`), observe and record:

| # | Criterion | Evidence to capture | Result |
|---|---|---|---|
| 1 | Scheduled runs occurred | 3 `workflow_runs` rows with `started_at` in the window | not performed |
| 2 | Sources collected | `entries_collected > 0`, `sources_failed = 0` | not performed |
| 3 | AI calls worked | `analysis_results.provider`; `provider_usage` rows | not performed |
| 4 | Discoveries persisted | new rows with `discovered_at` in the window | not performed |
| 5 | Telegram delivered | alert/digest on the phone; `deliveries.status = SENT` | not performed |
| 6 | Digest generated | `daily_digests` row with non-null `telegram_message_id` | not performed |
| 7 | Heartbeat updated | healthchecks.io pings aligned with run completion | not performed |
| 8 | Backup validated | Sunday `backup_manifests.status = VALIDATED` | not performed |
| 9 | Recovery works | `recovered_missed_runs > 0`, or a skipped slot recovered | not performed |

5. If **any** row is not satisfied, the status stays `NOT_VERIFIED` and the window
   is repeated. A green local suite is not evidence for this table.

---

## 10. Final production acceptance checklist

| Ref | Criterion | Status | Basis |
|---|---|---|---|
| A1 | All seven categories work | **PASSED** | registry + live collection |
| A2 | Sources collect reliably | **PASSED** | 24/24, 436 entries |
| A3 | Evidence verification works | **PASSED** | verifier tests + probe budget |
| A4 | Duplicates rejected | **PASSED** | dedupe tests + DB constraint probe |
| A5 | Low-confidence discarded | **PASSED** | pipeline stage 8 + tests |
| A6 | Qualified discoveries persisted end to end | **NOT TESTED** | needs a real run (§3) |
| B1 | Scoring works | **PASSED** | 30/25/20/15/10 tests |
| B2 | Confidence gate works | **PASSED** | confidence tests |
| B3 | free.ai works | **PASSED** | live call, 571 tokens |
| B4 | Fallback safe | **PASSED** (unit) | router tests |
| B5 | Usage accounting | **PASSED** (unit) / **NOT TESTED** (live rows) | `provider_usage` |
| C1 | Migrations valid + idempotent | **PASSED** | 3 applied, second run `[]` |
| C2 | Persistent storage works | **PASSED** | verifier 14/14 |
| C3 | Concurrent claims work | **PASSED** | `SKIP LOCKED` probe |
| C4 | Checkpoints durable | **PASSED** | probe |
| C5 | Duplicate prevention works | **PASSED** | probe |
| D1 | Telegram authentication verified | **UNVERIFIED** | `getMe` timed out; `notify --check` available |
| D2 | One approved delivery confirmed | **NOT TESTED** | not sent |
| D3 | Delivery state accurate | **PASSED** (unit) | dispatcher tests |
| D4 | Duplicate alerts prevented | **PASSED** (unit) | unique constraint + intent tests |
| D5 | Daily digest works | **PASSED** (logic) / **NOT TESTED** (delivery) | digest tests; no live send |
| E1 | Drive integration verified | **BLOCKED** | no OAuth grant |
| E2 | Checksum verified | **BLOCKED** | needs E1 |
| E3 | Restore tested on disposable DB | **BLOCKED** | needs E1 + second DB |
| F1 | Workflows deployed | **NOT DEPLOYED** | no Git remote; nothing pushed |
| F2 | Scheduled execution verified | **NOT VERIFIED** | never ran |
| F3 | Cloud run with PC off | **NOT VERIFIED** | §9 |
| F4 | Heartbeat verified | **NOT TESTED** | no ping URL |
| G1 | No secrets committed | **PASSED** | scan |
| G2 | No token leakage | **PASSED** | redaction observed live |
| G3 | No production-destructive tests | **PASSED** | rollback probe + name guard |
| G4 | No unauthorised paid API use | **PASSED** | no paid adapter exists |
| H1 | README accurate | **PASSED** | updated with evidence |
| H2 | Setup instructions work | **PASSED** (local) | followed during this work |
| H3 | Deployment instructions work | **PARTIAL** | gate documented; not executed |
| H4 | Recovery instructions present | **PASSED** | `docs/recovery.md` |
| H5 | Backup/restore instructions present | **PASSED** | `docs/backup-restore.md` |
| H6 | Operational checklist current | **PASSED** | updated this session |

**Tally across the 38 rows above: 25 PASSED · 2 PARTIAL · 4 NOT TESTED · 3 BLOCKED · 4 UNVERIFIED / NOT VERIFIED.**

- 25 PASSED: A1–A5, B1–B4, C1–C5, D3–D4, G1–G4, H1–H2, H4–H6
- 2 PARTIAL: B5 (usage accounting: unit-tested, no live rows yet), H3 (deployment runbook documented but not executed)
- 4 NOT TESTED: A6, D2, D5 (delivery), F4
- 3 BLOCKED: E1, E2, E3
- 4 UNVERIFIED / NOT VERIFIED: D1, F1, F2, F3

V1 is **not** accepted as a production service until A6, D1, D2, D5, E1–E3 and
F1–F4 are green.

---

## 11. Controls added this session (safety, not features)

1. **Deployment gate.** Every schedule-driven job now carries
   `if: ${{ github.event_name != 'schedule' || vars.SCHEDULES_ENABLED == 'true' }}`.
   Pushing the repo cannot start the cadence. Activation is one explicit command,
   and deactivation is another.
2. **Safe manual dispatch.** `research` and `digest` now default `dry_run = true`,
   so an accidental click cannot notify anyone; a real run requires setting it to
   `false` deliberately.
3. **Read-only Telegram verification.** `ai-radar notify --check` validates the
   token (`getMe`) and the chat id (`getChat`) **without sending anything**, which
   was previously impossible without risking a live message.
4. **Honest dry-run help text** so the persistence side effect cannot be misread.
5. **Tests for all of the above** (gate shape, dispatch defaults, `getChat`
   read-only behaviour, token redaction in `getChat` errors).

---

## 12. Working-tree changes pending commit

| File | Change |
|---|---|
| `.github/workflows/{research,digest,maintenance,backup}.yml` | deployment gate; safe dispatch defaults |
| `src/ai_radar/notifications/telegram.py` | `get_chat()` (read-only, permanent-vs-transient classification) |
| `src/ai_radar/cli.py` | `notify --check`; explicit `--dry-run` help |
| `tests/unit/test_workflows.py` | gate + dispatch-default tests |
| `tests/unit/test_telegram_client.py` | `getChat` read-only tests |
| `README.md`, `docs/deployment.md`, `docs/cloud_integration_checklist.md` | gate, Telegram procedure, dry-run caveat |
| `FINAL_V1_ACCEPTANCE_REPORT.md` | this report |
| `src/ai_radar/storage/db.py` | `endpoint_signature()`; `assert_staging_target()` refuses the production endpoint |
| `src/ai_radar/settings.py` | `AI_RADAR_STAGING` / `AI_RADAR_STAGING_DATABASE_URL`; staging blanks Telegram, Gemini, Groq, healthchecks, Drive |
| `src/ai_radar/cli.py` | `_staging_settings()` used by `build_context` and `doctor`; `DatabaseError` → exit 2 |
| `tests/unit/test_staging_mode.py` | env names, blanking, DSN swap, refusal (new) |
| `tests/unit/test_db_dsn.py` | staging target cases |
| `docs/staging-validation.md` | the runbook (new) |
| `src/ai_radar/cli.py` | `_freeai_usage_lookup()` binds `Provider.FREEAI` into the budget lookup (defect found by the staging run) |
| `tests/unit/test_freeai.py` | regression test for the bound lookup |
| `FINAL_V1_ACCEPTANCE_REPORT.md`, `docs/staging-validation.md` | staging execution results; corrected second-run expectations |

---

## 13. Limits of this report

- No Telegram message was sent. No FreeAI/Gemini/Groq inference call was made. No
  research run (dry or real) was executed, on any database. The staging procedure
  in §3A was prepared and its guards were exercised only on the refusal paths
  (no staging database exists yet, so nothing was connected to or migrated).
  No Supabase schema or data was modified or reset. No Google OAuth flow and no
  Drive operation was performed.
- Python 3.13 was not available here; only the 3.12 matrix leg was executed.
- The Telegram `getMe` timeout is recorded as an HTTP-layer timeout. It is neither
  evidence of a bad token nor evidence of a good one.
- The confidence threshold remains a 24-case seed calibration and must not be
  described as statistically validated.

---

## 14. Next action requiring your approval

Exactly one external operation is queued:

> **Nothing is queued for me to run.** The next action requires a decision from you
> (free.ai 429, Telegram chat id, GitHub remote) — see §3A.8.

### 3A.8 Third staging run (2026-11-24) — free.ai reachable, upstream 429

The budget-lookup fix worked: no `TypeError`, and free.ai was actually called from
the pipeline for the first time. It returned **HTTP 429** twice, so the provider was
disabled for the run again and all 40 analyses fell back to deterministic.

| Field | Run 3 `…054120Z-465f55` |
|---|---|
| status / sources | completed / 24 attempted, 0 failed |
| entries_collected → after_dedup | 205 → 86 |
| qualified / persisted_new | 40 / 40 |
| alerts_sent | 0 |
| ai_provider_counts | `deterministic: 40` |
| ai_errors | **2** (`rate_limit`) |

`provider_usage` (staging, all runs): `deterministic` 120 rows, `freeai` **6 rows,
all `ok=false`** — 4 `unavailable` (the TypeError defect, runs 1–2) and 2
`rate_limit` (run 3, latencies 15.8 s and 21.6 s). **Zero tokens billed**; the
25 000-token rolling budget is untouched.

Conclusion: the plumbing is correct and the provider is reachable, but the upstream
refuses the request. Valid `AnalysisPayload` was therefore **not** obtained —
Phase 2 acceptance is **NOT MET**.

**Likely causes to check (operator):** free.ai per-minute or per-day limit reached
for this key; the free pool being throttled at that time; or a `Retry-After` the
client honours and then gives up on. The earlier successful call (571 tokens) was
made by `scripts/verify_freeai.py` hours earlier, so the key itself is valid.

**Not attempted:** further free.ai calls in this run (the 2-call cap was reached),
any retry loop, or any Gemini/Groq fallback.

---

**END OF REPORT — `PC_INDEPENDENCE_STATUS = NOT_VERIFIED`, `PRODUCTION_DEPLOYMENT_STATUS = NOT_DEPLOYED`**
