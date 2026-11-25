# Cloud integration checklist

A tick-box runbook for the first deployment. Detailed failure handling lives in
[pre_deployment_audit.md](pre_deployment_audit.md); this page is the order of
operations and the gates between them.

**Rule:** do not tick a box because the command was typed. Tick it when the stated
evidence exists. Nothing here has been executed against real services yet.

```
[ ] 0. Pre-flight
[ ] A. Supabase project and connection
[ ] B. Telegram bot and private test chat
[ ] C. Gemini API key
[ ] D. Groq API key
[ ] E. Google Drive OAuth
[ ] F. GitHub encrypted secrets
[ ] G. Database migrations
[ ] H. First dry-run
[ ] I. First controlled real alert
[ ] J. Backup upload and restore drill
[ ] K. Independent heartbeat
[ ] L. Cloud deployment
[ ] M. PC-off acceptance test
```

---

## 0. Pre-flight (local, no cloud)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip setuptools wheel
pip install -r requirements.lock
pip install -e .
python -m pytest tests -q          # expect: 295 passed, 20 skipped
python -m compileall -q src        # expect: exit 0
git status --short                 # expect: clean, no .env
```

Gate G0: tests pass, no secret-shaped file is tracked, `git log` has no credential.

---

## A. Supabase

Create the project (Free), then **Connect → Shared pooler → Session mode**:

```powershell
$env:DATABASE_URL = "postgresql://postgres.<ref>:<password>@aws-0-<region>.pooler.supabase.com:5432/postgres?sslmode=require"
python -m ai_radar doctor     # [PASS] database
```

- [x] Port is **5432**, not 6543. The app refuses 6543 by design.
- [x] `sslmode=require` is present.
- [x] `doctor --database-only` shows `[PASS] database`.
- [x] After migrations: `python scripts/verify_database.py` prints
      `RESULT: PASS — schema and write paths verified; no rows were left behind.`
      It inserts, updates and claims inside one transaction and rolls it back.

Gate G1: connection works from the PC.

---

## B. Telegram

Do this in two steps. Step 1 proves the token and chat id **without sending
anything**; step 2 is the single approved live message.

```powershell
# after /newbot, in a PRIVATE chat, message the bot once
$env:TELEGRAM_BOT_TOKEN = "<token>"
python -m ai_radar notify --check     # token only: getMe, sends nothing
# -> {"token": "PASS", "chat": "NOT_CONFIGURED"} and exit code 2

# obtain the chat id (read-only Bot API call, no message sent)
Invoke-RestMethod "https://api.telegram.org/bot$env:TELEGRAM_BOT_TOKEN/getUpdates" |
  ConvertTo-Json -Depth 6      # result[0].message.chat.id

$env:TELEGRAM_CHAT_ID = "<numeric-chat-id>"
python -m ai_radar notify --check     # token AND chat: getMe + getChat
# -> {"token": "PASS", "chat": {"status": "PASS", ...}} and exit code 0
```

- [ ] `--check` prints `token: PASS`. If it prints `kind: unavailable`, the network
      path to `api.telegram.org` is timing out — try a different network, or run
      the same command inside a GitHub Actions manual dispatch. If it prints
      `kind: unauthorized`, the token is wrong or revoked; issue a new one with
      `/token` in BotFather. A timeout is **not** a token verdict.
- [ ] `--check` prints `chat.status: PASS` with the expected chat type.
- [ ] Only then, with explicit approval, `python -m ai_radar notify --test`
      sends exactly one message.
- [ ] The message arrives in a **private** chat.
- [ ] JSON contains the bot username and a message id.

Gate G2: one real test message, no other sends.

---

## C–D. free.ai, Gemini and Groq

```powershell
$env:FREEAI_API_KEY = "<sk-free-key>"
$env:GEMINI_API_KEY = "<key>"
$env:GROQ_API_KEY = "<key>"
python -m ai_radar doctor
```

- [ ] `[PASS] free.ai reachable, resolved model qwen7b`.
- [ ] `[PASS] Gemini <model>` (or WARN if you are not using it).
- [ ] `[PASS] Groq <model>` (or WARN if you are not using it).
- [ ] No billing account exists on any console.
- [ ] You understand free.ai's 30K/day pool: the adapter stops at
      `FREEAI_DAILY_TOKEN_BUDGET` and falls through, it does not overspend.

Gate G3: the configured providers resolve a model id at runtime.

---

## E. Google Drive

1. Cloud project → enable Drive API.
2. OAuth consent screen: External, scope `.../auth/drive.file`, add yourself.
3. **Publish to In production** (Testing-mode refresh tokens expire in 7 days).
4. OAuth client: **Desktop app**.
5. Run the helper:

```powershell
$env:GOOGLE_OAUTH_CLIENT_ID = "<id>"
$env:GOOGLE_OAUTH_CLIENT_SECRET = "<secret>"
python scripts/google_oauth_authorize.py
```

- [ ] Refresh token printed once and stored only in the password manager / secret.
- [ ] `GOOGLE_DRIVE_FOLDER_ID` left **empty**.
- [ ] `python -m ai_radar backup` creates `AI Radar Backups` and a `VALIDATED`
      manifest.
- [ ] `python -m ai_radar restore --verify-only` returns `"verified": true`.

Gate G4: upload **and** verification download succeeded; a real restore is step J.

---

## F. GitHub secrets

```powershell
gh secret set DATABASE_URL
gh secret set TELEGRAM_BOT_TOKEN
gh secret set TELEGRAM_CHAT_ID
gh secret set FREEAI_API_KEY
gh secret set GEMINI_API_KEY
gh secret set GROQ_API_KEY
gh secret set HEALTHCHECKS_PING_URL
gh secret set GOOGLE_OAUTH_CLIENT_ID
gh secret set GOOGLE_OAUTH_CLIENT_SECRET
gh secret set GOOGLE_OAUTH_REFRESH_TOKEN
# optional:
# gh secret set RESTORE_DATABASE_URL
```

- [ ] `gh secret list` shows every name (values are never displayed).
- [ ] `GOOGLE_DRIVE_FOLDER_ID` is either absent or empty.
- [ ] No secret has been pasted into a file, an issue or a chat.

Gate G5: the names match `.github/workflows/*.yml` exactly.

## G. Migrations

```powershell
python -m ai_radar migrate      # first run: ["001_initial_schema","002_runtime_state","003_freeai_provider"]
python -m ai_radar migrate      # second run: []
```

- [x] `schema_migrations` has exactly three rows.
- [x] 13 tables exist (9 required + `analysis_cache`, `provider_usage`,
      `system_state`, `schema_migrations`).
- [x] `analysis_results_provider_check` accepts `freeai`.

Then, non-destructively:

```powershell
python scripts/verify_database.py
```

- [x] Every check `PASS`; `RESULT: PASS`; no probe rows left behind.

Gate G6: fresh schema applied, idempotent, and the write paths verified without
leaving data behind.

### G0 execution record

Fill this only after the commands above ran for real. Keep the target masked; never
record the password or the full DSN.

| Field | Value |
|---|---|
| Date (UTC) | 2026-11-23 20:02 UTC (2026-11-24 01:32 IST) |
| Target (masked) | `postgres.***@aws-***.pooler.supabase.com:5432/postgres` |
| `doctor` database check | PASS — database connected; 3 migrations applied; exit 0 |
| Migrations applied | `001_initial_schema`, `002_runtime_state`, `003_freeai_provider`; second run `[]` |
| `verify_database.py` result | PASS — 13 tables, all indexes/constraints/FKs, checkpoint + atomic claim + duplicate rejection, 0 leftover rows |
| Integration tests (dedicated test DB) | BLOCKED — 16 destructive tests skipped; no disposable test database |
| Problems | none |
| Next user action | approve the next gate (Telegram or AI provider); G0 database is verified |

---


---

## H. First dry run

GitHub → Actions → `research` → **Run workflow** → `dry_run = true`.

- [ ] Run `completed`.
- [ ] `discoveries` has rows.
- [ ] `deliveries` has **no** new `SENT` row.
- [ ] Telegram is silent.

Gate G7: the pipeline works end to end without notifying anyone.

---

## I. First controlled alert

Dispatch `research` with `dry_run = false` (or wait for a scheduled run).

- [ ] At most 5 messages.
- [ ] Each message has title, priority, confidence, what is new, why it matters,
      practical use case, pricing/licence, evidence link, publication date.
- [ ] `deliveries.status = SENT` and `telegram_message_id` is set.

Gate G8: real delivery evidence.

---

## J. Backup and restore drill

```powershell
python -m ai_radar backup
python -m ai_radar restore --verify-only
# after creating a SECOND empty database:
$env:RESTORE_DATABASE_URL = "postgresql://postgres.<ref>:<password>@...:5432/postgres?sslmode=require"
python -m ai_radar restore
```

- [ ] Manifest `VALIDATED`.
- [ ] Drive file carries `appProperties.sha256`.
- [ ] Restore refuses `DATABASE_URL` as a target.
- [ ] Per-table counts match the manifest.

Gate G9: a database can be rebuilt from the archive.

### Destructive integration tests (separate database only)

`tests/integration/test_postgres.py` **truncates every table**. It now refuses to
run unless the target database name contains `test`, `ci`, `scratch` or `sandbox`.
For a second free Supabase project named `ai-radar-test`:

```powershell
$env:TEST_DATABASE_URL = "postgresql://postgres.<ref>:<password>@aws-0-<region>.pooler.supabase.com:5432/postgres?sslmode=require"
python -m pytest tests/integration/test_postgres.py -m postgres -q
```

- [ ] All 16 tests pass against the **test** database.
- [ ] Production `DATABASE_URL` was never used for this suite.

---

## K. Heartbeat

Create a healthchecks.io check: **Period 1 hour, Grace 11 hours**.

- [ ] `HEALTHCHECKS_PING_URL` secret set.
- [ ] A ping appears within a minute of a completed run.
- [ ] A failed run pings `/fail`.
- [ ] `ai-radar heartbeat` is not stale during the day.

Gate G10: the monitor is independent of GitHub Actions.

---

## L. Cloud deployment

- [ ] Repository public (recommended for ₹0 Actions), or cadence reduced.
- [ ] Secrets set (F), migrations done (G).
- [ ] `tests` workflow green on `main`.
- [ ] `SCHEDULES_ENABLED` is **unset** at push time (the deployment gate keeps
      every scheduled job skipped), and every manual dispatch used `dry_run`.
- [ ] Only now: `gh variable set SCHEDULES_ENABLED --body true`.
- [ ] Scheduled runs observed for 3 consecutive slots.
- [ ] Digest observed once at 22:35 IST.
- [ ] `maintenance` observed once at 06:45 IST **without** a false stale alert.

Gate G11: scheduled operation is green and quiet.

---

## M. PC-off acceptance

- [ ] PC shut down (not sleep).
- [ ] ≥ 3 research slots completed with the PC off.
- [ ] Telegram alerts or digest arrived.
- [ ] `workflow_runs` rows created.
- [ ] healthchecks pinged.
- [ ] A Sunday backup reached `VALIDATED`.
- [ ] A missed slot was recovered.

Gate G12: `PC_INDEPENDENCE_STATUS = VERIFIED`. Until then it stays
`NOT_VERIFIED`.

---

## Stop conditions

Stop and fix before continuing if any of these happen:

| Signal | Meaning |
|---|---|
| `port 6543 is a transaction-mode pooler` | wrong DSN; use session mode 5432 |
| `invalid_grant` from Google | re-consent; app must be In production |
| Any secret visible in a log or a commit | rotate it now, then continue |
| `UNKNOWN` deliveries accumulating | check the chat before retrying |
| Actions minutes exhausted | make the repo public or halve the cadence |

## Emergency stop

1. GitHub → Actions → disable `research`, `digest`, `backup`, `maintenance`.
2. Rotate any credential you suspect.
3. Keep the database: it is the memory. Do not delete projects.
