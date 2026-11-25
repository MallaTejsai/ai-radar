# Cost and quota model

Target: **₹0 per month**, on free tiers only. There is no code path that can enable
a paid API, so the failure mode is degraded output, not a bill.

All limits below were read from the providers' own pages on 2026-11-23. Free tiers
change; re-check the links before you rely on the headroom.

## 1. Services and limits

| Service | Free allowance | Source |
|---|---|---|
| GitHub Actions | unlimited for standard runners in **public** repos; 2,000 min/month + 500 MB storage on Free for **private** repos | [GitHub Actions billing](https://docs.github.com/en/billing/managing-billing-for-your-products/about-billing-for-github-actions) |
| Supabase | 500 MB database, 5 GB egress, 2 active projects; **pauses after 1 week of inactivity**; **no automatic backups** | [Supabase pricing](https://supabase.com/pricing) |
| Gemini API | per-model RPM / TPM / RPD on the Free tier; exact numbers are shown in AI Studio for your project | [Gemini rate limits](https://ai.google.dev/gemini-api/docs/rate-limits) |
| Groq | e.g. `openai/gpt-oss-120b`: 30 RPM, 1,000 RPD, 8K TPM (account-specific) | [Groq rate limits](https://console.groq.com/docs/rate-limits) |
| Google Drive | 15 GB shared with Gmail and Photos (new accounts after 2026-03-09: 5 GB, +10 GB after phone verification) | [Google storage](https://support.google.com/drive/answer/9312312) |
| healthchecks.io | 20 checks, 100 log entries per check | [healthchecks.io pricing](https://healthchecks.io/pricing/) |

## 2. GitHub Actions minutes

Scheduled runs per month:

| Workflow | Runs/day | Minutes/run (estimate) | Minutes/month |
|---|---|---|---|
| `research` | 15 | 4–6 | 1,800–2,700 |
| `digest` | 1 | 1–2 | 30–60 |
| `maintenance` | 1 | 1–2 | 30–60 |
| `backup` | ~0.14 (weekly) | 2–3 | 8–12 |
| `tests` | on push | 1–2 | small |
| **Total** | | | **≈ 1,870–2,830** |

That straddles the 2,000-minute private-repo allowance. Two consequences:

1. **A public repository is the only configuration that is ₹0 by construction.**
   Standard runners are free there.
2. If the repository must stay private, cut the cadence to every two hours
   (08:00–22:00 → 8 runs/day): ≈ 960–1,440 minutes, leaving room for tests. Edit
   both `config/settings.yaml` and `research.yml`; `doctor` fails if they drift.

Check your own usage:

```powershell
gh api /users/{your-user}/settings/billing/actions
```

For an organisation: `gh api /orgs/{org}/settings/billing/actions`.

GitHub rounds each job up to the nearest whole minute, so a 40-second run costs a
minute. The `Install` step (pip + lock) is usually the largest fixed cost; the
`cache: pip` setting in every workflow keeps it around 10–20 seconds after the
first run.

## 3. Supabase

| Resource | Free | Expected use |
|---|---|---|
| Database | 500 MB | see below |
| Egress | 5 GB/month | a few MB per run → ≈ 0.5–1 GB/month |
| Projects | 2 active | 1 production + 1 restore drill |

Row size is dominated by JSONB (`evidence`, `verification`, `score_breakdown`,
`analysis`). Budget ~3–5 KB per discovery including its evidence and analysis rows.

| Discoveries/day | Rows/year | Storage/year (≈4 KB/row) |
|---|---|---|
| 20 | 7,300 | ≈ 30 MB |
| 60 | 21,900 | ≈ 90 MB |

At 60/day the 500 MB ceiling is roughly five years away, and `analysis_cache` and
`provider_usage` are pruned automatically (45 and 120 days). Discoveries are never
pruned automatically — they are the product. If you approach the limit, take a
backup, then archive rows older than a cutoff; do not delete without a verified
restore.

A paused project means failed runs. Hourly activity keeps it awake; if it does
pause, resume it in the dashboard and run `maintenance`.

## 4. free.ai, Gemini and Groq

Per run the pipeline analyses at most `max_analyses_per_run = 40` discoveries
(typical: 0–10 new items per hour across 24 sources).

### free.ai (preferred, budget-capped)

The free plan is **30,000 tokens/day and 10 requests/minute**, one shared pool for
the website and the API. One analysis costs roughly 1,000–1,800 tokens, so the pool
is about 15–25 analyses per day — less than one busy run.

The adapter therefore enforces:

- `FREEAI_DAILY_TOKEN_BUDGET` (default 25,000) over a rolling 24-hour window,
  computed from `provider_usage`;
- `FREEAI_MAX_CALLS_PER_RUN` (default 2);
- `402` from the API is classified as `quota` and disables the provider for the run.

At 2 calls/run and 15 runs/day the theoretical maximum is 30 calls, but the token
budget will stop it much earlier on long prompts. That is deliberate: the pool must
last the day, not the first busy hour.

### Fallback chain

1. free.ai while budget remains;
2. Gemini (per-model RPM/TPM/RPD free tier);
3. Groq (≈1,000 RPD on `gpt-oss-120b`);
4. the deterministic analyser, which needs no model at all.

A `429` marks a provider rate-limited for the run; `402` marks free.ai quota-
exhausted; two `unavailable` failures disable a provider for the run. The run still
completes, marked `degraded`, and `maintenance` prints per-provider usage for the
day from `provider_usage`.

The adapters resolve model ids at runtime from each provider's `/models` endpoint
(free.ai uses the configured `FREEAI_MODEL`, default `qwen7b`), so a retired model
does not turn into a hard failure. Pin `GEMINI_MODEL` / `GROQ_MODEL` only if you
need reproducibility.

## 5. Google Drive

| Item | Value |
|---|---|
| Backups retained | 12 |
| Size each | tens to hundreds of KB (gzip JSON) |
| Monthly growth | < 5 MB |
| Limit | 15 GB shared with Gmail/Photos |

The backup is compressed JSON, not a binary dump, which keeps it small and
inspectable. Retention deletes the oldest archive only after a newer one is
validated.

## 6. healthchecks.io

One check, one ping per successful run plus a `/fail` ping on failure. The free
plan allows 20 checks and 100 log entries per check; at 15 pings/day the log covers
about a week, which is enough to see a pattern, not to keep an audit trail. The
audit trail lives in `workflow_runs`.

## 7. What breaks first

In order of likelihood:

| # | Constraint | Symptom | Fix |
|---|---|---|---|
| 1 | GitHub minutes (private repo only) | runs start failing near month end with a billing error | make the repo public, or halve the cadence |
| 2 | Gemini daily quota on a busy day | `429`, analyses fall back to Groq | none needed; check `maintenance` output |
| 3 | Supabase 500 MB after years | inserts fail with disk full | archive old discoveries after a verified backup |
| 4 | Supabase pause | every job fails to connect | resume in dashboard, run `maintenance` |
| 5 | Drive 15 GB | upload fails `storageQuotaExceeded` | free space or use a Workspace account |
| 6 | Scheduled workflows disabled after 60 days of repo inactivity (public repos) | heartbeat goes late | make a keepalive commit, re-enable the workflow |

## 8. Guard rails in code

- `max_alerts_per_run` = 5, enforced as the minimum of three config layers.
- `max_analyses_per_run` = 40.
- `analysis_cache` avoids re-analysing the same prompt fingerprint.
- Provider calls have no automatic paid fallback; there is no paid provider adapter
  at all.
- `provider_usage` records requests and quota errors per provider per day.
- Workflow timeouts (20/10/20/10 minutes) cap a runaway job.

## 9. Monthly review

```powershell
gh api /users/{your-user}/settings/billing/actions     # minutes used
ai-radar maintenance                                    # quota, queue, staleness
```

Dashboard checks: Supabase → Reports → Database size; Drive → storage; AI Studio
and Groq console → usage. Five minutes a month is enough to see a trend before it
becomes an outage.
