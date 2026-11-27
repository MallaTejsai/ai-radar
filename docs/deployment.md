# Cloud deployment

After this procedure the system runs without the PC. Nothing here requires a
local process, a local database or a local scheduler.

```
GitHub Actions (cron) ──▶ ai-radar CLI ──▶ Supabase PostgreSQL
                              │
                              ├──▶ Gemini ─▶ Groq ─▶ deterministic rules
                              ├──▶ Telegram Bot API
                              ├──▶ Google Drive (weekly backup)
                              └──▶ healthchecks.io (one ping per successful run)
```

## 1. Repository

Create an empty repository and push this code.

```powershell
git init
git add .
git commit -m "AI Radar V1.0"
git branch -M main
git remote add origin https://github.com/<you>/ai-radar.git
git push -u origin main
```

**Make the repository public if you want a guaranteed ₹0 Actions bill.** Standard
GitHub-hosted runners are free for public repositories; private repositories on
the Free plan include 2,000 minutes per month ([GitHub Actions billing
docs](https://docs.github.com/en/billing/managing-billing-for-your-products/about-billing-for-github-actions)).
The hourly schedule is roughly 450 runs per month, so a private repository can
approach that quota — see [cost-model.md](cost-model.md).

No secret is ever committed: `.gitignore` excludes `.env`, key files and service
account JSON, and the workflows read secrets from the environment.

## 2. Supabase

1. Create a project at <https://supabase.com> (Free plan: 500 MB database).
2. Project Settings → Database → **Connection string** → URI.
3. Use the **shared pooler in session mode**: port `5432`, username
   `postgres.<project-ref>`, and keep `?sslmode=require`. GitHub runners are
   IPv4-only and the direct host is IPv6-only on newer projects.
   **Do not use port 6543.** That is the transaction pooler: Supabase does not
   support prepared statements there, and a session-scoped advisory lock can be
   acquired on one backend and never released on another. The pipeline refuses a
   6543 DSN at startup for that reason. Session mode on the shared pooler is
   IPv4-reachable and supports both.
4. Keep the password in a password manager, not in a file in the repository.

Apply the schema in one of two ways:

- **Locally:** `ai-radar migrate` with `DATABASE_URL` set.
- **In CI:** the `migrations` job in `tests.yml` applies migrations on every push
  to `main`, but only after the `DATABASE_URL` secret exists.

The migration runner is checksum-guarded: an applied migration whose file changed
is an error, not a silent re-run.

## 3. Telegram

1. `@BotFather` → `/newbot` → copy the token.
2. Send your new bot any message.
3. Open `https://api.telegram.org/bot<TOKEN>/getUpdates` and read
   `result[0].message.chat.id`.

Use a private chat or a dedicated group. Do not use a public channel for alerts
that may contain unreleased-product details.

## 4. Free-tier intelligence keys

| Provider | Console | Notes |
|---|---|---|
| free.ai | <https://free.ai/api/> | preferred; OpenAI-compatible, `qwen7b`, **30K tokens/day**, 10 req/min, budget-capped in the adapter |
| Gemini | <https://aistudio.google.com/apikey> | fallback; model id resolved at runtime from `/models` |
| Groq | <https://console.groq.com/keys> | fallback; OpenAI-compatible |

free.ai's daily pool is smaller than one busy research run, so the adapter enforces
a rolling-24h token budget (`FREEAI_DAILY_TOKEN_BUDGET`, default 25,000) and a
per-run call cap (`FREEAI_MAX_CALLS_PER_RUN`, default 2). When either is reached it
raises `quota` and the router falls through to Gemini → Groq → deterministic.

Both adapters list the models available to the key and pick the first supported id
from a preference list, so a retired model id does not break the run. Set
`GEMINI_MODEL` / `GROQ_MODEL` only when you want to pin an exact id.

## 5. Google Drive (backup destination)

Follow [backup-restore.md](backup-restore.md) §2. You need three values:
`GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`,
`GOOGLE_OAUTH_REFRESH_TOKEN`. Leave `GOOGLE_DRIVE_FOLDER_ID` empty: the
`drive.file` scope cannot write into a folder the app did not create, so the app
creates and reuses its own **AI Radar Backups** folder.

A service account does not work: it has no storage quota of its own, so it cannot
own files in a personal Drive.

## 6. External heartbeat

1. Create a check at <https://healthchecks.io> (free: 20 checks).
2. **Period: 1 hour. Grace: 11 hours.**
   The research window is 08:00–22:00 IST; a strict one-hour grace would page you
   every night. Eleven hours means "no successful run by roughly 09:30 IST" is an
   alert, while a failed run still pages immediately because the pipeline pings
   `<url>/fail` when a run ends `FAILED`.
3. Copy the ping URL into `HEALTHCHECKS_PING_URL`.

The ping is sent only after the run row is written, so a workflow that starts and
then dies does not look healthy.

## 7. Secrets and variables

Repository → Settings → Secrets and variables → Actions.

| Secret | Used by | Required |
|---|---|---|
| `DATABASE_URL` | all jobs | yes |
| `TELEGRAM_BOT_TOKEN` | research, digest, maintenance, backup | yes |
| `TELEGRAM_CHAT_ID` | same | yes |
| `FREEAI_API_KEY` | research | recommended |
| `GEMINI_API_KEY` | research | recommended |
| `GROQ_API_KEY` | research | recommended |
| `HEALTHCHECKS_PING_URL` | research, maintenance | recommended |
| `GOOGLE_OAUTH_CLIENT_ID` | backup, restore | for backup |
| `GOOGLE_OAUTH_CLIENT_SECRET` | backup, restore | for backup |
| `GOOGLE_OAUTH_REFRESH_TOKEN` | backup, restore | for backup |
| `GOOGLE_DRIVE_FOLDER_ID` | backup, restore | optional — leave empty |
| `RESTORE_DATABASE_URL` | backup rehearsal | optional |

`GITHUB_TOKEN` is provided by GitHub automatically; `research.yml` passes it to the
collector to raise the API limit from 60 to 5,000 requests per hour. It is
read-only and scoped to this repository, and no workflow declares write
permissions.

| Variable | Value | Effect |
|---|---|---|
| `RESTORE_REHEARSAL` | `enabled` | runs the restore drill after each weekly backup |
| `FREEAI_MODEL` | e.g. `qwen7b` | optional; leave unset to use the adapter default |

`FREEAI_API_KEY` must be set for the preferred provider to be used at all:
`research.yml` passes it explicitly. If it is absent, the router starts at Gemini
and free.ai is never constructed.

Adding secrets with the CLI avoids the clipboard:

```powershell
gh secret set DATABASE_URL
gh secret set TELEGRAM_BOT_TOKEN
gh secret set TELEGRAM_CHAT_ID
gh secret set FREEAI_API_KEY
gh secret set GEMINI_API_KEY
gh secret set GROQ_API_KEY
gh secret set HEALTHCHECKS_PING_URL
gh variable set RESTORE_REHEARSAL --body enabled
```

`gh secret set` prompts for the value without echoing it.

## 8. Schedule

| Workflow | Cron (UTC) | IST | Job |
|---|---|---|---|
| `research.yml` | `30 2-16 * * *` | 08:00–22:00 hourly at :30 | pipeline |
| `digest.yml` | `5 17 * * *` | 22:35 | daily digest |
| `backup.yml` | `30 17 * * 0` | Sunday 23:00 | export + upload + verify |
| `maintenance.yml` | `15 1 * * *` | 06:45 | health, queue review, pruning |
| `tests.yml` | on push / PR | — | tests, then migrations on `main` |

The digest is at 22:35, not 22:00, because the last research run starts at 22:30.
A 22:00 digest would be built from an incomplete day.

`ai-radar doctor` parses these cron expressions and fails when they drift from
`config/settings.yaml`. Change the schedule in both places or not at all.

### The deployment gate (schedules are OFF until you opt in)

Every schedule-driven job starts with:

```yaml
if: ${{ github.event_name != 'schedule' || vars.SCHEDULES_ENABLED == 'true' }}
```

Consequences:

- Pushing this repository **does not** start the hourly cadence, even though the
  `schedule:` triggers are present.
- Manual dispatch still works (it is not a schedule event), and `research` and
  `digest` now default to `dry_run = true`, so an accidental click cannot notify
  anyone. A real run requires setting the input to `false` explicitly.
- Activation is one deliberate action:

```powershell
gh variable set SCHEDULES_ENABLED --body true    # turn the cadence on
gh variable delete SCHEDULES_ENABLED             # turn it back off immediately
```

Turning it off does not delete data and does not stop a manual dispatch. Keep it
unset until the first production sequence (§9) has passed.

## 9. First production sequence

Run each step by hand once, in order. Do not enable "trust me" mode.

| # | Action | Expected |
|---|---|---|
| 1 | `tests` workflow on `main` | tests green; migrations applied |
| 2 | `research` with `dry_run` = true (the default) | run `completed`, rows in `discoveries`, **no** Telegram message |
| 3 | one real alert: dispatch `research` with `dry_run` = false on a quiet hour | zero to five alerts, delivery rows `SENT` |

**Read this before step 2.** `--dry-run` suppresses *notifications only*. It still
writes to production: `sources` (registry sync + checkpoints), `workflow_runs`,
`discoveries`, `discovery_sources`, `analysis_results`, `pricing_history`,
`analysis_cache`, `provider_usage` and `system_state`. Because the discoveries it
stores are then "already known", **a later run will not alert on them**. So the
first real alert (step 3) necessarily comes from discoveries collected *after* the
dry run — do not expect the dry run's own rows to alert. See
[FINAL_V1_ACCEPTANCE_REPORT.md](../FINAL_V1_ACCEPTANCE_REPORT.md) §3 for the exact
write set.
| 4 | `digest` with `dry_run` = true | rendered digest in the log, no message |
| 5 | `digest` for real | one message at 22:35 IST |
| 6 | `backup` | `backup_manifests.status = VALIDATED`; file visible in Drive |
| 7 | `maintenance` | heartbeat evaluated, no stale warning |
| 8 | healthchecks.io | last ping within the hour |

If step 6 fails with `invalid_grant`, the refresh token was issued by an OAuth app
still in "Testing" status and has expired; see
[backup-restore.md](backup-restore.md) §2.

## 10. PC-independence acceptance test

This is the requirement that cannot be shortcut. It is **NOT VERIFIED** until it
has been executed.

1. Confirm all eight steps in §9 passed.
2. Confirm the cadence is actually enabled: `gh variable list` shows
   `SCHEDULES_ENABLED = true`. Without it every scheduled job is skipped by the
deployment gate, so the window would prove nothing.
3. Shut the PC down completely — not sleep, not hibernate.
4. Leave it off across at least three scheduled research slots and one digest.
5. From the phone (see [setup-android.md](setup-android.md)) verify:
   - new rows in `discoveries` with `discovered_at` inside the window;
   - `workflow_runs` rows with `status = completed`;
   - Telegram alerts or a digest arrived;
   - healthchecks.io shows pings;
   - after Sunday 23:00 IST, a new `VALIDATED` backup row.
6. Record the timestamps in [final-report.md](final-report.md).

If any of those fail while the PC is off, the system is not PC-independent, no
matter how green the local test suite is.

## 11. Operating notes

- **GitHub disables scheduled workflows in a public repository after 60 days
  without repository activity.** The external heartbeat will page you when that
  happens. Either make a trivial commit monthly ("keepalive") or add a narrow
  keepalive workflow with `contents: write`. This repository deliberately does not
  ship one, because a write-scoped workflow is a larger risk than a monthly
  reminder.
- **Cron is best effort.** Runs can be delayed by tens of minutes or skipped
  entirely under load. The pipeline detects missed slots and recovers the last 24
  hours; see [recovery.md](recovery.md).
- **Concurrency is enforced in the database**, not only by the workflow's
  `concurrency` group, so a manual dispatch cannot double-send.
- **Free Supabase projects pause after a week without activity.** Hourly runs keep
  it active; if it is paused, resume it in the dashboard and run `maintenance`.

## 12. Rollback

| Situation | Action |
|---|---|
| Bad deploy, database fine | revert the commit; the next run uses the old code |
| Bad migration | restore last `VALIDATED` backup into a new project, repoint `DATABASE_URL` |
| Runaway notifications | disable `research.yml` in the Actions UI; check `deliveries` |
| Suspected secret leak | rotate every secret listed in §7, revoke the bot token, reset the database password |
