# Pre-deployment audit checklist

Every step below is written so that a second person could execute it without
asking a question. Nothing here has been performed against real cloud services yet;
this is the procedure, not evidence that it succeeded.

Conventions:

- Commands run from the repository root in PowerShell.
- `<placeholders>` are never real values. Never paste a secret into chat, an
  issue, a commit or this file.
- Exit codes: `0` success, `1` runtime failure, `2` configuration/precondition.
- A step is complete only when its **Verify** line has produced the stated result.

---

## A. Supabase project and connection

| Field | Detail |
|---|---|
| **Action** | Create a Supabase project on the Free plan, region close to the runners (e.g. `ap-south-1`). Dashboard → **Connect** → **Shared pooler, session mode**. Copy the URI; replace `[YOUR-PASSWORD]`; keep `sslmode=require`. Set `DATABASE_URL` locally in `.env` and later as a GitHub secret. |
| **Expected** | Connection string uses host `aws-0-<region>.pooler.supabase.com`, **port 5432**, user `postgres.<project-ref>`. |
| **Verify** | `python -m ai_radar doctor` shows `[PASS] database`; `python -m ai_radar migrate` prints `{"applied": ["001_initial_schema", "002_runtime_state"]}`; a second `migrate` prints `{"applied": []}`; `python scripts/verify_database.py` prints `RESULT: PASS` and leaves no rows behind. |
| **Common failure** | Using port **6543** (transaction pooler) — the app refuses it at startup because prepared statements and the session advisory lock are unsupported there. `password authentication failed` — password not URL-encoded. Timeout — using the IPv6-only direct host. `verify_database.py` reports a missing table — migrations did not run or ran against a different database. |
| **Recovery** | Copy the session-mode string again from **Connect**. If the password contains `@ : / ? # &`, percent-encode it. If the project is paused, resume it in the dashboard and rerun. Migrations are idempotent; a partial apply is completed by rerunning. |

Do **not** use the `postgres` superuser string from "Direct connection" in CI: it
is IPv6-only on newer projects and GitHub-hosted runners are IPv4-only.

---

## B. Telegram bot and private test chat

| Field | Detail |
|---|---|
| **Action** | `@BotFather` → `/newbot` → token. Send the bot one message (or add it to a private group), then read `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy `result[0].message.chat.id` (negative for groups). Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`. |
| **Expected** | `python -m ai_radar notify --test` prints `{"bot": "<name>", "message_id": <n>}` and the message appears in the chat. |
| **Verify** | The message text begins `AI Radar test message`; the JSON contains a bot username, not an error. |
| **Common failure** | `404` — chat id wrong or the bot was never started. `401` — token revoked. `400 can't parse entities` — a formatting defect, report it with the message. |
| **Recovery** | Re-read `getUpdates` after messaging the bot. If the token leaked, `/revoke` in BotFather and update the secret. |

The test message is the only message you should send before step I.

---

## C1. free.ai API key (preferred provider)

| Field | Detail |
|---|---|
| **Action** | Create a free account at <https://free.ai>, generate an `sk-free-` key, set `FREEAI_API_KEY`. Default model is `qwen7b`; the free plan allows 30,000 tokens/day and 10 requests/minute. |
| **Expected** | `doctor` shows `[PASS] free.ai reachable, resolved model qwen7b`; a research run stores `analysis_results.provider = 'freeai'` until the budget is reached. |
| **Verify** | `select provider, count(*) from analysis_results group by 1;` and `select sum(coalesce(prompt_tokens,0)+coalesce(completion_tokens,0)) from provider_usage where provider='freeai' and occurred_at > now() - interval '24 hours';` |
| **Common failure** | `402` — daily pool exhausted (expected on a busy day; the router falls through). `401` — bad key. `400` — model id not available; check `GET /v1/models`. |
| **Recovery** | Wait for the pool to reset (the adapter uses a rolling 24h window), lower `FREEAI_MAX_CALLS_PER_RUN`, or rely on Gemini/Groq/deterministic. Never add a paid plan without approval. |

The adapter never spends money: free.ai's plan is token-only and there is no paid
fallback in the codebase.

## C. Gemini API key

| Field | Detail |
|---|---|
| **Action** | <https://aistudio.google.com/apikey> → create key in a project with **no billing account**. Set `GEMINI_API_KEY`. Optionally pin `GEMINI_MODEL`; otherwise the adapter resolves an available `generateContent` model at runtime. |
| **Expected** | `doctor` shows `[PASS] Gemini <model-id>`; a research run writes `analysis_results.provider = 'gemini'` and `provider_usage` rows. |
| **Verify** | `select provider, model_id, count(*) from analysis_results group by 1,2;` and `select * from provider_usage order by occurred_at desc limit 5;` |
| **Common failure** | `403` key restricted to another API; `429` daily/minute quota; no model returned (key in a region/project without access). |
| **Recovery** | Do not enable billing. Wait for the quota reset (midnight Pacific), pin a cheaper model, or let the router fall back to Groq/deterministic. |

The adapter never sends private data: only the public announcement text, bounded to
3,000 characters, with our registered secrets scrubbed first.

---

## D. Groq API key

| Field | Detail |
|---|---|
| **Action** | <https://console.groq.com/keys> → create key. Set `GROQ_API_KEY`. Optionally `GROQ_MODEL`. |
| **Expected** | `doctor` shows `[PASS] Groq <model-id>`; when Gemini is rate-limited, `analysis_results.provider = 'groq'` with a `fallback_reason`. |
| **Verify** | `select provider, fallback_reason, count(*) from analysis_results group by 1,2;` |
| **Common failure** | `401` invalid key; `429` RPD/TPM exceeded; `400` model does not support `response_format=json_object`. |
| **Recovery** | The router disables the provider for the run and falls through. Check the account limits page; do not upgrade to a paid tier without approval. |

---

## E. Google Drive OAuth

| Field | Detail |
|---|---|
| **Action** | Google Cloud project → enable **Google Drive API** → OAuth consent screen (External, scope `.../auth/drive.file`, add yourself as a test user) → **publish to In production** → create an OAuth client of type **Desktop app**. Then run `scripts/google_oauth_authorize.py` with `GOOGLE_OAUTH_CLIENT_ID` / `GOOGLE_OAUTH_CLIENT_SECRET` set. Store the three values; leave `GOOGLE_DRIVE_FOLDER_ID` empty. |
| **Expected** | The helper prints a refresh token once; `python -m ai_radar backup` creates `AI Radar Backups` and uploads `ai-radar-backup-*.json.gz`; `backup_manifests.status = VALIDATED`. |
| **Verify** | `python -m ai_radar restore --verify-only` returns `"verified": true`; the Drive file's `appProperties.sha256` equals the manifest checksum. |
| **Common failure** | `invalid_grant` — app left in **Testing** (refresh tokens expire after 7 days), grant revoked, or 100-token limit per client/user reached. `403` Drive API not enabled. `404` when a user-created folder id is configured — clear it. |
| **Recovery** | Publish the app, re-run the helper (`prompt=consent` forces a new refresh token), update secrets. A production refresh token can still be revoked by the user, a password change, or inactivity — the weekly backup failing is the signal. |

If the OAuth client is type **Web application** instead of **Desktop app**, add
exactly `http://127.0.0.1:8765/oauth2callback` as an authorised redirect URI, or
run the helper with `--port` matching what you registered.

**Do not claim Drive backup works until upload, download and a restore into a
separate database have actually run.** The code path is tested with a mock
transport only.

---

## F. GitHub encrypted secrets

| Field | Detail |
|---|---|
| **Action** | Repository → Settings → Secrets and variables → Actions. Add: `DATABASE_URL`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `GEMINI_API_KEY`, `GROQ_API_KEY`, `HEALTHCHECKS_PING_URL`, `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`, `GOOGLE_OAUTH_REFRESH_TOKEN`. Optional: `GOOGLE_DRIVE_FOLDER_ID` (leave empty), `RESTORE_DATABASE_URL`. `GITHUB_TOKEN` is automatic. Use `gh secret set NAME` so the value is never echoed. |
| **Expected** | `gh secret list` shows names only. A `research` run with `dry_run=false` reaches `status: completed`. |
| **Verify** | `gh secret list`; Actions log contains no secret values (grep the log for a known prefix). |
| **Common failure** | Secret name typo; value has trailing whitespace/newline; environment secrets not visible to the job; `DATABASE_URL` set to the 6543 pooler. |
| **Recovery** | Re-set the secret with `gh secret set`. Rotate immediately if a value was pasted anywhere public. |

Never commit `.env`; it is git-ignored for a reason.

---

## G. Database migration procedure

| Field | Detail |
|---|---|
| **Action** | Either local `python -m ai_radar migrate`, or push to `main` and let the `migrations` job in `tests.yml` run it (it is skipped when the secret is absent). |
| **Expected** | `schema_migrations` has `001_initial_schema`, `002_runtime_state` and `003_freeai_provider`; 13 tables exist (9 required + `analysis_cache`, `provider_usage`, `system_state`, `schema_migrations`). |
| **Verify** | `select table_name from information_schema.tables where table_schema='public' order by 1;` and `select version, checksum from schema_migrations order by 1;` |
| **Common failure** | `extension "pgcrypto" is not available`; permission denied (not the owner); checksum drift after editing an applied file; a partial apply after a dropped connection. |
| **Recovery** | Rerun — each file is a transaction and idempotent. Never edit an applied migration; add a new one. If the schema is corrupt, restore the last `VALIDATED` backup into a fresh database. |

---

## H. First dry-run execution

| Field | Detail |
|---|---|
| **Action** | Actions → `research` → **Run workflow** → `dry_run = true`. |
| **Expected** | Run ends `completed`; `workflow_runs.report` shows collected/verified/persisted counters; `discoveries` gains rows; `deliveries` gains **no** new `SENT` rows; Telegram receives nothing. |
| **Verify** | `select run_id, status, entries_collected, persisted_new, alerts_sent from workflow_runs order by started_at desc limit 1;` |
| **Common failure** | Exit `2` configuration; all sources failing (network/DNS); `skipped_locked` because a scheduled run is in progress. |
| **Recovery** | Read `report.errors`; run `ai-radar doctor` locally with the same secrets; rerun a single source with `ai-radar collect --source <id>`. |

---

## I. First controlled real alert

| Field | Detail |
|---|---|
| **Action** | Wait for a run that qualifies at least one discovery, or dispatch `research` with `dry_run=false` during the window. Do not lower thresholds to force one. |
| **Expected** | At most 5 messages; every message has title, priority, confidence, what is new, why it matters, use case, pricing, evidence link, date; `deliveries.status = SENT` with a `telegram_message_id`. |
| **Verify** | Telegram message content; `select status, count(*) from deliveries group by 1;` |
| **Common failure** | `429` after several sends; `400` markup; ambiguous timeout → `UNKNOWN`. |
| **Recovery** | Rate limits defer automatically. `UNKNOWN` is never auto-retried: check the chat first, then reconcile manually. |

---

## J. Backup upload and restore test

| Field | Detail |
|---|---|
| **Action** | `python -m ai_radar backup` → `python -m ai_radar restore --verify-only` → create a second empty Supabase project, set `RESTORE_DATABASE_URL`, run `python -m ai_radar restore`. |
| **Expected** | Manifest `VALIDATED`; `verified: true`; restore reports per-table counts matching the manifest; the production DSN is refused as a target. |
| **Verify** | `select backup_id, status, sha256 from backup_manifests order by created_at desc limit 3;` and the restore JSON `total_rows`. |
| **Common failure** | `invalid_grant`; Drive quota; checksum mismatch; row-count mismatch. |
| **Recovery** | Re-consent or use an older archive. A checksum or count mismatch means do **not** use that restore; investigate before repointing anything. |

---

## K. Independent heartbeat

| Field | Detail |
|---|---|
| **Action** | Create a healthchecks.io check: **Period 1 hour, Grace 11 hours**; copy the ping URL to `HEALTHCHECKS_PING_URL`. |
| **Expected** | A ping arrives within a minute of a completed run; a failed run pings `/fail`; disabling the workflow produces a late/alert state after the grace. |
| **Verify** | healthchecks.io log shows pings aligned with `workflow_runs.finished_at`; `ai-radar heartbeat` reports `"stale": false` during the day. |
| **Common failure** | Placeholder URL; a 403 from a wrong slug; 11-hour grace hiding a same-morning failure (the `/fail` ping covers crashes, not silence). |
| **Recovery** | Recreate the check, update the secret, rerun `research`. |

The internal staleness check is schedule-aware: at 06:45 the overnight gap is not a
failure, and a missed 08:00 slot is reported after a 60-minute grace.

---

## L. Cloud deployment

| Field | Detail |
|---|---|
| **Action** | Push to `main` with all secrets set; keep the repository **public** unless you accept the 2,000-minute private quota (see `cost-model.md`). Do not disable the workflows; do not add write permissions. |
| **Expected** | `tests` green; `research` runs hourly 08:00–22:00 IST; `digest` at 22:35; `backup` Sunday 23:00; `maintenance` 06:45. |
| **Verify** | Actions history for 3 consecutive slots; `workflow_runs` rows; healthchecks pings; Telegram. |
| **Common failure** | Actions minutes exhausted (private repo); scheduled workflows disabled after 60 days of repo inactivity (public repo); cron drift from `config/settings.yaml` (doctor fails); Supabase paused. |
| **Recovery** | Make the repo public or reduce cadence (edit both config and workflow); resume Supabase; make a keepalive commit; re-enable the workflow. |

---

## M. PC-off acceptance test

| Field | Detail |
|---|---|
| **Action** | Shut the PC down completely (not sleep). Leave it off across ≥ 3 research slots, one digest and one Sunday backup. |
| **Expected** | See the acceptance table in `FINAL_PRE_DEPLOYMENT_AUDIT.md` §14 — all rows pass. |
| **Verify** | From the phone: Telegram, Supabase table editor, healthchecks, Drive. |
| **Common failure** | The PC was only asleep; schedules were disabled; a secret only existed in `.env`, not GitHub; the digest ran before the last research run finished. |
| **Recovery** | Fix, then repeat the whole window. Partial evidence is not acceptance. |

Until this passes, `PC_INDEPENDENCE_STATUS = NOT_VERIFIED`.

---

## Cost guard rails for the operator

- Never add a billing account to Gemini or Groq for this project.
- Never enable a Supabase paid plan or IPv4 add-on to "make it work".
- Keep the repository public, or reduce the schedule before the 2,000-minute quota
  becomes a surprise.
- Check `gh api /users/<you>/settings/billing/actions` monthly.
