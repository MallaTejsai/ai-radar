# Staging validation (validate the whole pipeline before production)

The purpose of this procedure is to run the **complete** research pipeline —
collection, normalization, deduplication, evidence verification, free.ai analysis,
scoring, persistence — against a database that is *not* production, with every
external side effect except free.ai switched off.

It replaces the earlier plan of pointing `research --dry-run` at the production
database. A staging run gives strictly more evidence (the same code path, the same
writes, the same provider call) with none of the production risk.

**Status: prepared, not executed.** The staging database does not exist yet; this
document is the procedure awaiting approval.

---

## 1. Why not the production dry run

`--dry-run` only suppresses notifications. It still writes `sources`
(registry sync + checkpoints), `workflow_runs`, `discoveries`,
`discovery_sources`, `analysis_results`, `pricing_history`, `analysis_cache`,
`provider_usage` and `system_state` in whatever database `DATABASE_URL` names.
Worse, the discoveries it stores become "already known", so a later real run
**never alerts on them**. A staging database removes both problems.

---

## 2. Feasibility (verified)

| Question | Answer | Evidence |
|---|---|---|
| Is the pipeline tied to production? | **No** — every DB access goes through one DSN | single `Database(settings.database_url)` construction in `build_context` |
| Does it need anything but the schema? | **No** — `ai-radar migrate` creates all 13 tables | migration runner is DSN-agnostic |
| Is a local database available on this PC? | **No** | no `docker`, no `psql`, nothing listening on 5432 |
| Practical option | **A second Supabase project** (Free plan: 2 active projects) | one production + one staging |
| Does environment beat `.env`? | **Yes** | verified: an env `DATABASE_URL` overrides the `.env` value |

A local Postgres (Docker or native) is equally suitable if you install one later —
the guard accepts a disposable database *name* when no production DSN is known
(see §3.2).

### Supabase project-slot note

Supabase Free allows **2 active projects**. Production is one. If staging takes the
second, the restore rehearsal and the 16 destructive integration tests must reuse
the **same** staging project rather than a third one. Order matters:

1. pipeline validation (this document) — first;
2. restore rehearsal and destructive tests — afterwards, because they truncate
   every table and would destroy the evidence from step 1.

---

## 3. How staging cannot touch production

Four independent layers, three of them structural.

### 3.1 Staging reads a different variable

`AI_RADAR_STAGING=1` makes the run use **`AI_RADAR_STAGING_DATABASE_URL`** and
never `DATABASE_URL`. Production is not avoided; it is never read. A forgotten
override therefore cannot fall back to it.

### 3.2 Endpoint identity is checked

`assert_staging_target()` refuses the run when the staging URL reaches the same
**host + port + user** as `DATABASE_URL`. The comparison deliberately ignores the
database name, because Supabase names every database `postgres`, so the project
ref (in the host and username) is the only real identifier.

When no `DATABASE_URL` is present at all, the staging database *name* must contain
`test`, `ci`, `scratch`, `sandbox` or `stag` instead.

Both refusals happen before a connection is opened, exit `2`, and never print the
DSN.

### 3.3 Every other integration is blanked

While `AI_RADAR_STAGING=1`, `Settings` clears:

| Blanked | Consequence |
|---|---|
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | the dispatcher is never constructed; no message can be sent |
| `GEMINI_API_KEY`, `GROQ_API_KEY` | the router can only build free.ai; those providers are never contacted |
| `HEALTHCHECKS_PING_URL` | staging runs do not pollute production monitoring |
| `GOOGLE_OAUTH_*`, `GOOGLE_DRIVE_FOLDER_ID` | `backup` cannot upload into the production folder — important, because backup **retention deletes old files** there |

### 3.4 A pre-flight that refuses before connecting

```powershell
python -m ai_radar doctor --database-only
```

prints either a masked target —

```
[PASS] staging target  staging postgres.stagingref@aws-0-<region>.pooler.supabase.com:5432/postgres; production postgres.prodref@aws-0-<region>.pooler.supabase.com:5432/postgres; different endpoints, production never connected; fingerprint 3f9c1a7d2b6e4c08
```

— or a `[FAIL] staging target` and exits `2` **without opening a connection**.

The masked form shows host, port, user and database but **never the password**.
The fingerprint is a truncated SHA-256 of the exact DSN string: it let you confirm
that the shell variable is the URL you intended without printing it, and two
different URLs never share one.

The production URL is read only to compute the endpoint comparison in §3.2; no
connection is ever opened with it, and no production state is read or written.

---

## 4. Exact environment variables

Set these **in the shell session only**. Do not put `AI_RADAR_STAGING` in `.env`:
it would switch every later command on that machine into staging mode.

| Variable | Value | Required | Why |
|---|---|---|---|
| `AI_RADAR_STAGING` | `1` | yes | enables the guards in §3 |
| `AI_RADAR_STAGING_DATABASE_URL` | staging session-pooler DSN | yes | the only database staging will read |
| `DATABASE_URL` | *(unchanged, from `.env`)* | — | used **only** for the endpoint comparison |
| `FREEAI_API_KEY` | *(from `.env`)* | effectively yes | the one live provider under test |
| `FREEAI_DAILY_TOKEN_BUDGET` | `25000` (default) | no | rolling 24 h cap |
| `FREEAI_MAX_CALLS_PER_RUN` | `2` (default) | no | hard per-run cap |
| `GEMINI_API_KEY` / `GROQ_API_KEY` | **ignored** | — | blanked by staging mode |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | **ignored** | — | blanked by staging mode |
| `GOOGLE_OAUTH_*` / `HEALTHCHECKS_PING_URL` | **ignored** | — | blanked by staging mode |

Create the staging database (**you** do this; it is an external account action):

1. Supabase dashboard → **New project** → name it `ai-radar-staging`, same region.
2. **Connect → Shared pooler → Session mode** (port **5432**, `sslmode=require`).
3. Copy the URI. Keep the password in your password manager, **not** in a file.

---

## 5. Procedure

```powershell
# --- 0. one run, no production writes -----------------------------------------
$env:AI_RADAR_STAGING = "1"
$env:AI_RADAR_STAGING_DATABASE_URL = "postgresql://postgres.<staging-ref>:<pw>@aws-0-<region>.pooler.supabase.com:5432/postgres?sslmode=require"

# --- 1. pre-flight: refuses to continue if the target is production ----------
python -m ai_radar doctor --database-only
#   expect: [PASS] staging target  staging <masked>; production <masked>; ... fingerprint <16 hex>
#           [PASS] database  connected and responding
#           [PASS] migrations  0 applied     (an empty staging project)
#   exit 0, or exit 2 with [FAIL] staging target (nothing connected)
#   NOTE: doctor never migrates. Step 2 does that, and only after approval.

# --- 2. schema on staging only ------------------------------------------------
python -m ai_radar migrate
python -m ai_radar migrate                 # second run must print {"applied": []}

# --- 3. the full pipeline -----------------------------------------------------
python -m ai_radar research --trigger manual
#   --dry-run is unnecessary here: the Telegram dispatcher cannot be constructed
#   in staging mode, so both invocations are notification-free. Use plain
#   `research` so the code path is identical to production.

# --- 4. second run: dedupe against stored memory + analysis cache ------------
python -m ai_radar research --trigger manual
#   expect: entries_collected > 0 (the feeds move between runs) and
#           after_dedup < entries_collected, because keys already stored as
#           discoveries and in-batch duplicates are removed.
#   Only the first max_analyses_per_run (default 40) survivors are analysed and
#   persisted, so persisted_new is normally > 0 on a live feed. Analyses found in
#   analysis_cache cost no provider calls. after_dedup reaches 0 only when the
#   sources produced nothing new since the previous run.

# --- 5. read the evidence (read-only SQL on the STAGING project) --------------
```

### Verification queries (run in the staging SQL editor)

```sql
-- run history: one completed run, then a second with nothing new
select run_id, status, entries_collected, after_dedup, verified,
       persisted_new, alerts_sent, recovered_missed_runs
  from workflow_runs order by started_at;

-- persistence
select count(*) as discoveries,
       count(*) filter (where verdict = 'CONFIRMED') as confirmed,
       count(*) filter (where priority_score >= 40)  as three_of_five_or_better,
       min(priority_score), max(priority_score)
  from discoveries;

select count(*) from discovery_sources;     -- evidence rows
select count(*) from analysis_results;      -- one row per persisted discovery
select count(*) from pricing_history;

-- provider usage: <= 2 free.ai calls on the first run, 0 on the second
select provider, count(*) as calls,
       coalesce(sum(prompt_tokens),0) + coalesce(sum(completion_tokens),0) as tokens
  from provider_usage group by provider;

-- notifications must be absent
select count(*) from deliveries;            -- must be 0
select count(*) from daily_digests;         -- must be 0

-- source health and checkpoints
select source_id, health_status, consecutive_failures, records_collected
  from sources order by source_id;
```

### Expected outcome

| Signal | Expected |
|---|---|
| `sources` rows | 24, mostly `healthy` |
| `entries_collected` | a few hundred (436 on 2026-11-24) |
| `after_dedup` (run 1) | lower than collected (in-batch duplicates merged) |
| `verified` | > 0 |
| `persisted_new` | > 0 |
| `alerts_sent` | **0** |
| `deliveries` | **0 rows** |
| `provider_usage` run 1 | `freeai` with ≤ 2 calls, ≤ ~4 K tokens |
| `provider_usage` run 2 | 0 new calls for cached analyses; otherwise ≤ 2 `freeai` |
| `after_dedup` run 2 | < `entries_collected` (stored keys + in-batch duplicates removed) |
| `persisted_new` run 2 | up to `max_analyses_per_run` (default 40); `0` only if nothing new arrived |
| `discoveries` verdicts | mixture of `CONFIRMED` / `PARTIAL`; low confidence absent entirely |

---

## 6. What this validates — and what it does not

| Pipeline stage | Exercised by |
|---|---|
| Collection from all 24 sources | run 1 |
| Normalization + filtering | run 1 (`entries_collected` vs `after_dedup`) |
| In-batch deduplication | run 1 (`after_dedup < collected`) |
| Deduplication against stored memory | run 2 (`after_dedup == 0`) |
| Evidence verification + verdicts | run 1 (`verified > 0`, verdict mix) |
| free.ai analysis (live, ≤2 calls) | run 1 (`provider_usage`) |
| Deterministic fallback for the rest | run 1 (`provider_usage` has no other provider) |
| Scoring + priority labels | run 1 (`priority_score`, `priority_label`) |
| Confidence gate / complete discard | run 1 (no low-confidence rows exist) |
| Persistence + evidence rows | run 1 (`discoveries`, `discovery_sources`, `analysis_results`) |
| Notification suppression | both runs (`deliveries` is empty) |

**Not validated by staging** (needs the cloud): GitHub Actions scheduling,
concurrency in a real runner, heartbeats, Telegram delivery, Drive backup,
PC independence.

---

## 7. Cost and time

| Item | Value |
|---|---|
| free.ai | ≤ 2 calls, ~2–4 K tokens on run 1; **0** on run 2 |
| Gemini / Groq | **0** (structurally disabled) |
| Telegram | **0 messages** |
| Google Drive | **0 operations** |
| Supabase | 2nd free project; a few hundred rows |
| Wall time | ~1–2 min per run (24 feeds + ≤120 probes + 2 model calls) |
| Money | ₹0 |

---

## 8. After the run

1. Record the two run reports and the query output in
   [`FINAL_V1_ACCEPTANCE_REPORT.md`](../FINAL_V1_ACCEPTANCE_REPORT.md) §3A.
2. Confirm production is untouched (read-only, Supabase SQL editor on the
   **production** project):

   ```sql
   select count(*) from discoveries;                 -- same as before the run
   select max(occurred_at) from provider_usage;      -- no staging rows
   ```

3. Keep or delete the staging project as you prefer. If you intend to run the
   restore rehearsal or the destructive suite, keep it and run those **after**
   this validation, because they truncate every table.

---

## 9. Reference: the guard in code

```python
# cli._staging_settings
if settings.ai_radar_staging:
    assert_staging_target(
        settings.ai_radar_staging_database_url,
        staging=True,
        production_dsn=settings.database_url,
    )
    settings = settings.model_copy(
        update={"database_url": settings.ai_radar_staging_database_url}
    )
```

Covered by `tests/unit/test_staging_mode.py` and the staging cases in
`tests/unit/test_db_dsn.py` (env names, endpoint refusal, missing URL,
disposable-name fallback, DSN never echoed).
