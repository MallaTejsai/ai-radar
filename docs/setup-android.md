# Android management guide

The phone is a **control surface**, never a host. Nothing in production runs on
it, and losing it costs you visibility, not data.

What you can do from Android:

- read alerts and the daily digest in Telegram;
- watch workflow runs and trigger one manually;
- check the external heartbeat;
- inspect the database;
- stop the system in an emergency.

## 1. Install three apps

| App | Use |
|---|---|
| **Telegram** | alerts and the 22:35 IST digest |
| **GitHub** (official) | Actions runs, logs, secrets, manual dispatch |
| **A browser** (Chrome) | Supabase dashboard, healthchecks.io |

Sign in to GitHub with the account that owns the repository, and to Telegram with
the account that owns the chat configured in `TELEGRAM_CHAT_ID`.

## 2. Read the daily signal

- **Telegram** — up to five alerts per hourly run, plus one digest. If an hour
  produces nothing worth reporting, nothing arrives. Silence is a valid result.
- **Digest** — top five discoveries, model releases, coding tools, free/open-source
  findings, industry developments, one recommendation, source links.

If the digest does not arrive by about 22:45 IST, treat it as an incident and work
through §5.

## 3. Trigger or re-run a workflow

GitHub mobile → repository → **Actions** → choose a workflow → **Run workflow**.

| Workflow | When to run by hand |
|---|---|
| `research` | after changing sources or scoring; tick `dry_run` to avoid Telegram |
| `digest` | to re-send a digest; tick `dry_run` to render without sending |
| `backup` | before a risky change, or to test Drive credentials |
| `maintenance` | after a database or credential change |
| `tests` | to check a branch |

The web UI is more reliable for boolean inputs: open
`https://github.com/<owner>/<repo>/actions/workflows/research.yml` in Chrome and
use **Run workflow**. Inputs are `dry_run` / `verify_only`.

A manual run and a scheduled run can overlap; the database advisory lock makes the
second one exit with `skipped_locked` instead of double-sending. That is expected,
not an error.

## 4. Check health without a laptop

| What | Where | Healthy looks like |
|---|---|---|
| External heartbeat | <https://healthchecks.io> (or its app/PWA) | last ping minutes ago during 08:00–22:00 IST |
| Runs | GitHub → Actions | green runs roughly hourly |
| Last run report | Supabase → Table editor → `workflow_runs` | recent `finished_at`, `status = completed` |
| Discoveries | Supabase → `discoveries` | new rows with `verdict = CONFIRMED` |
| Delivery queue | Supabase → `deliveries` | no rows stuck in `SENDING` for hours |
| Backups | Drive folder | weekly `ai-radar-backup-*.json.gz`, plus `backup_manifests` rows |

A run that *starts* is not proof of success. Check `workflow_runs.status` and the
heartbeat, which is only pinged after the run row is written.

## 5. Incident runbook (phone only)

**Nothing arrives for two hours during the window**

1. GitHub → Actions → is `research` red? Open the failing step.
2. healthchecks.io → is the check late? If GitHub is fine but the ping is missing,
   the run is not reaching the end — read `workflow_runs.report`.
3. Supabase → is the project paused? Free projects pause after a week without
   activity; restore it from the dashboard and run `maintenance`.

**Telegram says `401`/`404`**

The token was revoked or the chat id changed. Recreate the bot with `@BotFather`,
update `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in GitHub Secrets, then run
`research` with `dry_run` off and check one message arrives.

**Suspected credential leak**

1. GitHub → Settings → Secrets → update the affected values.
2. `@BotFather` → `/revoke` for the bot token.
3. Supabase → Settings → Database → reset the database password.
4. Google Account → Security → Third-party access → remove the app's grant.
5. Rotate the Gemini and Groq keys in their consoles.
6. Run `maintenance` and read the delivery queue for anything unexpected.

**Runaway alerts**

GitHub → Actions → `research` → **⋯ → Disable workflow**. The cap is five alerts
per run, but disabling stops all sends immediately. Re-enable after the cause is
fixed.

## 6. Optional: Termux with `gh`

For people who prefer a shell on the phone:

```bash
pkg install git gh
gh auth login
gh workflow run research.yml -f dry_run=true
gh run list --workflow=research.yml --limit 5
gh run view <run-id> --log-failed
```

Do not store tokens in shell history or notes. `gh auth login` keeps its own
credential store; revoke it with `gh auth logout` when you change phones.

## 7. Rules for the phone

- Never paste a secret into Telegram, a note app or a screenshot.
- Never edit `.env` on the phone; production values belong in GitHub Secrets.
- Do not install "automation" apps that replay your GitHub session — a stolen
  session token is a stolen repository.
- Do not treat a green workflow list as proof of delivery: check Telegram and the
  `deliveries` table.
