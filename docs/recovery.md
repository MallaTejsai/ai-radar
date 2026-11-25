# Recovery

GitHub Actions cron is best-effort. Runs are delayed under load, coalesced, or
skipped entirely. The system is built on the assumption that the previous slot may
never have happened.

## 1. What "recovery" means here

On every run the pipeline:

1. reads the last successful run and the start times of recent runs;
2. computes which scheduled hourly slots inside the last **24 hours** have no
   successful run;
3. widens collection for this pass (2× items per source, capped at 100) so a gap
   yields more than the usual window;
4. normalises, deduplicates and verifies as usual;
5. persists everything recoverable;
6. records `recovered_missed_runs` in the run report;
7. refuses to alert on discoveries older than **48 hours**, so recovering a gap
   does not flood Telegram with old news;
8. advances a source checkpoint only after its records are durably stored.

The plan is computed by `operations/recovery.py::plan_recovery` and the slot
arithmetic lives in `utils/timeutil.py::missing_slots`.

## 2. Why checkpoints matter more than the schedule

Each source row stores `last_success_at`, `etag` and `last_modified`. A checkpoint
is a promise: "everything published before this moment has been examined".

- The checkpoint moves **only** when every collected record was persisted or
  deliberately discarded by a rule (duplicate, filtered, low confidence).
- If the database write fails, the checkpoint does not move. The next run
  re-collects the same window, and deduplication stops the work from doubling.
- A source that fails is recorded as failed and does not block the others.

This is why a missed run is recoverable at all: nothing was marked "done" that was
not actually stored.

## 3. What each failure looks like

| Failure | Detection | Behaviour |
|---|---|---|
| Run delayed 40 minutes | slot arithmetic | next run sees a gap, widens collection |
| Run skipped entirely | `missing_slots` | same; the slot is recovered inside 24 h |
| Two runs overlap | advisory lock | second exits `skipped_locked`, no double send |
| Database unreachable | connect error | run fails before any checkpoint moves; retried next hour |
| One source down | per-source error | other 23 continue; source marked unhealthy |
| AI providers down | provider chain | Groq, then deterministic; run completes `degraded` |
| Telegram timeout after send | `UNKNOWN` state | not retried automatically; reported in the run |
| Process killed mid-persist | transaction rollback | rows are either fully written or absent |
| Backup upload fails | `FAILED` manifest | run fails, next week retries; local export is logged |

## 4. Reading the state

```sql
-- last 10 runs and what they recovered
select run_id, started_at, finished_at, status, trigger,
       recovered_missed_runs, entries_collected, persisted_new, alerts_sent
from workflow_runs
order by started_at desc
limit 10;

-- sources that have not succeeded recently
select id, name, last_success_at, last_error, consecutive_failures, health
from sources
order by last_success_at nulls first;

-- deliveries stuck mid-flight
select id, discovery_id, status, attempts, last_attempt_at, telegram_message_id
from deliveries
where status in ('PENDING', 'SENDING', 'RETRY_WAIT', 'UNKNOWN')
order by created_at;
```

`workflow_runs.report` is JSON and holds the stage counters, including
`discarded_unverified`, `discarded_low_confidence` and per-source persisted counts.

## 5. Manual recovery

**A single missed hour.** Do nothing. The next scheduled run recovers it. If you do
not want to wait:

```
GitHub → Actions → research → Run workflow (dry_run off)
```

**A whole morning missed.** Check Actions first. If the workflow was disabled or
broken, fix it, then dispatch a run. Recovery is bounded to 24 hours; older items
are not re-collected, by design — the feeds have moved on and the archive is not a
news database.

**A source has been failing for days.** Run the diagnostic collector, which does
not persist anything:

```powershell
ai-radar collect --source openai_news
```

Then check the endpoint by hand. If the feed URL changed, update
`config/sources.yaml` and run `ai-radar sources --validate`. The registry contract
rejects duplicate ids, unknown categories, missing tiers and disabled-by-default
entries.

**Checkpoints look wrong.** Inspect first:

```sql
select id, last_success_at, etag, last_modified, consecutive_failures
from sources where id = 'openai_news';
```

To force a full re-read of one source, clear its conditional-request headers in a
transaction, so a mistake is reversible:

```sql
begin;
update sources
set last_success_at = null, etag = null, last_modified = null
where id = 'openai_news';
-- inspect, then commit or rollback
commit;
```

Clearing `last_success_at` does not delete discoveries; deduplication keeps the
memory intact.

**Deliveries stuck in `SENDING`.** The process died between claim and result. After
a grace period, `maintenance` reports them. Resolve by checking Telegram: if the
message is there, mark it sent; if not, return it to the queue.

```sql
-- inspect, do not blind-update
select id, discovery_id, status, attempts, last_attempt_at
from deliveries where status = 'SENDING' and last_attempt_at < now() - interval '1 hour';
```

There is no exactly-once guarantee from Telegram. A manual resend can duplicate a
message; a missed alert is the more likely outcome of guessing, so prefer
`UNKNOWN` over a blind retry unless you have checked the chat.

**A run failed but the checkpoint moved.** That should be impossible by
construction: checkpoints move after persistence, inside the same run, and only if
every write succeeded. If you observe it, treat it as a bug and inspect
`workflow_runs.report.errors` before changing anything.

## 6. Recovery is deliberately bounded

| Bound | Value | Reason |
|---|---|---|
| Recovery window | 24 h | older feeds have rotated; re-collecting them is noise |
| Items per source | 2× configured, max 100 | a week of backlog must not become a 2,000-item run |
| Analyses per run | 40 | free-tier quota and 20-minute job timeout |
| Alerts per run | 5 | Telegram usability |
| Alert age | 48 h | old discoveries are stored, not announced |

Anything beyond those bounds is *not* recovered. The run reports it rather than
pretending otherwise.

## 7. When to intervene

| Signal | Threshold | Action |
|---|---|---|
| No successful run | 2 hours inside the window | inspect Actions |
| Heartbeat late | 11 hours after the last ping | treat as a stopped system |
| Source failures | 3 consecutive | fix or disable the source |
| `workflow_runs.status = failed` | any | read `report.errors` |
| Backups | 2 missed weeks | run `backup` manually; check OAuth grant |
