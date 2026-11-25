# Final pre-deployment audit — AI Radar V1.0

**Audit date:** 2026-11-24 (IST) / 2026-11-23 (UTC)
**Auditor role:** senior Python / cloud DevOps / database reliability / security
**Scope:** read-only audit of the repository as committed, plus three minimal fixes
for defects proven during the audit. No deployment, no live notifications, no paid
API calls, no credential handling.

Status labels used throughout:

- **PASS** — verified by execution or direct inspection.
- **PARTIAL** — implemented, not fully verified, or a known limitation.
- **BLOCKED** — requires the owner's credentials or external configuration.
- **FAIL** — defect found. Every FAIL below was fixed during this audit and is
  marked `FAIL → fixed`.
- **NOT_TESTED** — no evidence available.

`PC_INDEPENDENCE_STATUS = NOT_VERIFIED`

---

## 1. Summary

The implementation is coherent and locally tested: **267 passed, 20 skipped**, byte
compilation clean, all 24 sources live, five workflows structurally valid, and the
command-line entry point works with the package uninstalled. The eight previously
reported fixes are real, not renames: each was inspected in code, exercised by a
substantive test, and checked for the edge case that could still break it.

The audit found **three additional defects** that would have caused production
failures:

1. Telegram error text, which embeds the bot token in the request URL, was written
   to `deliveries.last_error` and therefore into backups. **Fixed** (scrubbed at
   the HTTP layer and at the persistence boundary).
2. The documentation told the operator to use Supabase's **transaction pooler**
   (port 6543). It does not support prepared statements, and a session-scoped
   advisory lock can be acquired on one backend and never released on another —
   which would have made every later run exit `skipped_locked`. **Fixed** (startup
   guard + session-mode instructions).
3. The internal staleness check used a fixed 150-minute window, so the 06:45 IST
   maintenance job would have paged the owner **every morning** for the deliberate
   overnight gap. **Fixed** (schedule-aware slots with a grace period).

Everything that depends on a real external service remains unverified:
PostgreSQL execution, Gemini, Groq, Telegram delivery, Drive upload/download/restore,
cloud scheduling and PC independence. Those are BLOCKED on the owner, not on code.

**Recommendation:** READY FOR CLOUD CONFIGURATION. **Not** production-ready.

---

## 2. Eight-defect regression audit

Each row was checked in the implementation, in its regression test, and by running
the test. A test name alone was not accepted as evidence.

| # | Defect | Code evidence | Test evidence | Result | Remaining edge case |
|---|---|---|---|---|---|
| 1 | `python -m ai_radar` had no entry point | `src/ai_radar/__main__.py` imports `cli.main` and exits with its code; `main.py` forwards identically | `test_cli.py::test_module_entry_point_exists`, `test_help_exits_zero`; both `ai-radar --help` and `python -m ai_radar --help` run | **PASS** | None found. The two entry points share one function, so they cannot drift. |
| 2 | CI could not import the package | `pyproject.toml` sets `pythonpath = ["src"]`; every workflow job that runs the CLI sets `PYTHONPATH: src` | Suite run with `ai-radar` **uninstalled**: `import ai_radar` fails, `pytest` still reports 267 passed | **PASS** | Test tooling (`pytest`, `pytest-asyncio`) is installed unpinned in CI — see §3. |
| 3 | Drive token exchange sent JSON as form data | `drive.py` builds `urlencode(body)` and posts via `send_raw` with `application/x-www-form-urlencoded` | `test_drive.py::test_token_exchange_is_form_encoded_not_json` asserts content type, parsed fields, and the absence of `{` | **PASS** | No live token exchange has run (BLOCKED). Retry is deliberately absent; a failure surfaces as `invalid_grant`/HTTP error. |
| 4 | Drive upload assumed a user-created folder | `ensure_folder()` finds or creates an app-owned folder; `drive.file` cannot see a user folder; `list_backups` is read-only (`create=False`) | `test_upload_creates_an_app_owned_folder_when_none_is_configured`, `test_an_existing_app_owned_folder_is_reused`, `test_listing_backups_never_creates_a_folder` | **PASS** | If an operator sets `GOOGLE_DRIVE_FOLDER_ID` to a folder the app never opened, upload still 404s. Documented, not silently handled. |
| 5 | Backup checksum was not stored with the artefact | Manifest includes `tables_sha256`; upload attaches `appProperties.sha256`; restore requires a checksum and recomputes both hashes | 6 tests: checksum stored, corrupted download refused, missing checksum refused, edited rows refused, order-independent table hash | **PASS** | Backups created before this fix carry no checksum and are refused by design. `appProperties` values fit Drive's 124-byte limit. |
| 6 | Evidence probes cached by domain | `_probe_url` keys the cache by canonical URL; 120-probe budget degrades to `INSUFFICIENT` rather than a false CONFIRMED | 11 tests incl. `test_availability_is_checked_per_url_not_per_domain` and `test_probe_budget_degrades_to_unprobed_not_confirmed` | **PASS** | A 302 counts as reachable without following (SSRF safety); a busy day may leave later items unprobed and therefore unalertable. |
| 7 | Logger bound `sys.stderr` at configuration time | `_DynamicStreamLogger` resolves `sys.stderr` per write and swallows `ValueError`/`OSError` | New `test_logging.py`: replaced stream is followed, closed stream does not raise, secrets redacted | **PASS** | `UnicodeEncodeError` is not caught. On a legacy non-UTF-8 console a message with emoji could still raise; CI uses UTF-8. |
| 8 | Duplicate migration recreated stale tables | Only `001_initial_schema.sql` and `002_runtime_state.sql` remain; the legacy file is in `archive/` | `migrations.py` applies in filename order with a checksum guard; migration tests are Postgres-only and skipped here | **PASS** (duplicate eliminated) / **NOT_TESTED** (fresh install) | A fresh database has never been created in this audit. The SQL is idempotent and transactional, but execution is BLOCKED. |

Verdict on the eight: **8/8 addressed; 7 fully verified locally, 1 verified by
inspection only because it needs a live database.**

---

## 3. Test and workflow execution evidence

### 3.1 Commands and results

```
> python -m pytest tests -o addopts="" -q
267 passed, 20 skipped in 3.23s

> python -m compileall -q src
exit 0

> python -m pip uninstall -y ai-radar
Successfully uninstalled ai-radar-1.0.0

> python -c "import ai_radar"
ModuleNotFoundError: No module named 'ai_radar'

> python -m pytest tests -o addopts="" -q
267 passed, 20 skipped in 3.27s

> python -m pip install --no-deps --no-build-isolation -e .
Successfully installed ai-radar-1.0.0
> ai-radar --help            # usage line, 11 subcommands
> python -m ai_radar --help  # identical usage line
```

The uninstalled run is the important one: it proves CI does not depend on an
accidental local install.

After the G0 safety guard was added (the destructive Postgres suite now refuses a
non-disposable database), the current suite is **295 passed, 20 skipped**.
The 267 figures above are the run recorded during the audit itself.

### 3.2 Skipped tests (not counted as passes)

| Reason | Count | Marker |
|---|---|---|
| PostgreSQL not available | 16 | `postgres` |
| Gemini contract | 1 | `live` |
| Groq contract | 1 | `live` |
| Telegram contract | 1 | `live` |
| Google Drive contract | 1 | `live` |

Total 20. None of them ran; none may be reported as passed.

### 3.3 Workflow inspection

| Workflow | Triggers | Jobs | Timeout | Permissions | Concurrency |
|---|---|---|---|---|---|
| `research.yml` | cron `30 2-16 * * *`, dispatch | research | 20 min | `contents: read` | yes, no cancel |
| `digest.yml` | cron `5 17 * * *`, dispatch | digest | 10 min | `contents: read` | yes |
| `backup.yml` | cron `30 17 * * 0`, dispatch | backup, restore-rehearsal | 20 min | `contents: read` | yes |
| `maintenance.yml` | cron `15 1 * * *`, dispatch | maintenance | 10 min | `contents: read` | yes |
| `tests.yml` | push, PR, dispatch | test (3.12/3.13), migrations on `main` | 15/10 min | `contents: read` | n/a |

- Working directory: repository root; no `working-directory` overrides.
- `PYTHONPATH: src` is set on every job that runs `python -m ai_radar`.
- Python 3.12 for production jobs; the test matrix adds 3.13.
- Secrets are referenced only via `${{ secrets.* }}`; no `echo` of a secret expression.
- `continue-on-error` appears once, on `doctor`, and never around the pipeline.
- No workflow declares write permissions.
- Every cron matches `config/settings.yaml`; `doctor` fails if they drift.

Remaining PARTIAL: `pytest` and `pytest-asyncio` are installed by name without a
version in `tests.yml`, so the test toolchain is not reproducible even though the
runtime is pinned.

---

## 4. Security findings

| # | Check | Result | Evidence / residual risk |
|---|---|---|---|
| 1 | Credentials in Git history | **PASS** | Both commits scanned for PAT, `AIza`, `gsk_`, `ya29`, Telegram bot URL, private-key header. One hit only: a deliberately fake `AIzaSyAAAA…` fixture in `test_prompts.py`. |
| 2 | API keys in tracked files | **PASS** | Same scan over `git ls-files`; `.env` untracked and git-ignored. |
| 3 | Telegram token in logs | **PASS** | `_safe_url()` scrubs every FetchError URL; log processor redacts registered literals and token shapes; tests assert `***REDACTED***`. |
| 4 | Telegram token in the database | **FAIL → fixed** | `deliveries.last_error` previously received `str(TelegramError)`, which includes `…/bot<id>:<token>/…`. Dispatcher now scrubs before `mark_delivery_unknown` / `defer_delivery`; 3 parameterised tests. |
| 5 | OAuth refresh token in logs | **PASS** | The token is never logged; token-exchange errors include status and `error_description` only. The helper prints it once to stdout and writes nothing to disk. |
| 6 | Private keys in configuration | **PASS** | None present; service-account JSON is explicitly git-ignored and not used by the design. |
| 7 | Unsafe default production configuration | **PASS** | Missing secrets fail closed with exit 2; no paid provider exists; a 6543 DSN is refused. |
| 8 | Broad GitHub workflow permissions | **PASS** | Every workflow: `permissions: contents: read`; no `pull_request_target`; actions limited to first-party `actions/*`. |
| 9 | AI-generated shell execution | **PASS** | No `subprocess`, `os.system`, `eval` or `exec` in `src/`. |
| 10 | AI-generated SQL | **PASS** | Model output is validated into Pydantic and stored via parameterised SQL; no model text reaches a statement. |
| 11 | SSRF | **PASS** | Scheme allowlist, no userinfo, ports 80/443, DNS-resolved private/loopback/link-local/metadata rejection, revalidation per redirect hop (max 3), 2 MB cap. Collectors use `SafeHttpClient`; `feedparser` parses text only, never fetches. |
| 12 | External content treated as instructions | **PASS** | Fenced `<untrusted_content>`, fence/role-marker neutralisation, 3,000-char bound, advisory-only output, deterministic pricing/priority recomputation. |
| 13 | Unencrypted backup artefact | **PARTIAL** | The archive is gzip JSON: public announcement text, URLs, run reports, scrubbed errors. Drive encrypts in transit and at rest, but there is no application-level encryption. No credential is stored in it. Accepted for V1; add envelope encryption if the archive ever holds private data. |
| 14 | Overly broad Supabase credentials | **PARTIAL** | `DATABASE_URL` uses the project owner role because the migration job needs DDL. Runtime could use a DML-only role, but the same secret is used by both jobs. Documented in `security.md` §3; not enabled. |

Also found and fixed at the HTTP layer: `FetchError` messages included the full
request URL. They now pass through `_safe_url()`, which drops userinfo and masks
secret-shaped path/query segments. This is the root-cause half of finding 4.

---

## 5. Database readiness

### 5.1 Schema

13 tables: the nine required by the brief (`sources`, `discoveries`,
`discovery_sources`, `analysis_results`, `pricing_history`, `deliveries`,
`workflow_runs`, `daily_digests`, `backup_manifests`) plus `analysis_cache`,
`provider_usage`, `system_state` and `schema_migrations`.

| Property | Finding |
|---|---|
| Keys | UUID/text primary keys; `discoveries.canonical_key` UNIQUE; `(discovery_id, channel)` UNIQUE on deliveries; `(discovery_id, prompt_version)` UNIQUE on analysis; evidence PK on `(discovery_id, source_id, evidence_url)` |
| Foreign keys | evidence/analysis/pricing/deliveries → discoveries (CASCADE); discoveries → sources (RESTRICT) |
| Checks | closed enums for tier, method, pricing, verdict, status, delivery state; priority 0–100; confidence 0–1; non-negative counters |
| Indexes | category+priority, discovered_at, title_fingerprint, status+verdict, dispatchable deliveries, run history, backup history, provider usage |
| Timestamps | all `timestamptz`; `updated_at` maintained by trigger on sources/discoveries/deliveries |
| Ordering | `001` creates the memory schema, `002` the runtime tables, `003` widens the provider CHECK to include free.ai; filename order is deterministic |
| Idempotency | `IF NOT EXISTS` throughout; triggers dropped and recreated |
| Checksum guard | editing an applied migration raises `MigrationError` instead of diverging silently |

### 5.2 Runtime behaviour

- **Atomic delivery claim:** `SELECT … FOR UPDATE SKIP LOCKED` in a CTE, then
  `UPDATE … RETURNING`, with the per-run limit applied in the claim.
- **Checkpoints:** `record_source_success` is only called after persistence; a
  failed write leaves the checkpoint untouched, so the next run re-collects.
- **Transactions:** one transaction per discovery upsert (discovery, evidence,
  analysis, pricing history); one per delivery transition.
- **Connection handling:** psycopg3 async pool, min 1 / max 6, 20 s connect
  timeout, one connection held for the session advisory lock.
- **Outage recovery:** connection failure aborts the run before any checkpoint
  moves; the next scheduled run recovers.
- **Concurrency:** workflow `concurrency` group plus a PostgreSQL advisory lock;
  the second runner exits `skipped_locked`.

### 5.3 The connection-mode finding

Supabase's shared pooler in **transaction mode** (port 6543) cannot support this
design: the docs state prepared statements are unsupported and session-level state
(including session advisory locks) is lost between transactions. Both are in use.

- The app now refuses a 6543 DSN at construction with an explanatory
  `DatabaseError`, unless `AI_RADAR_ALLOW_TRANSACTION_POOLER=1` is set.
- The correct string is **shared pooler, session mode, port 5432**, username
  `postgres.<project-ref>`, `sslmode=require`. It is IPv4-reachable, which
  GitHub-hosted runners require.
- `.env.example`, `deployment.md` and `setup-windows.md` were corrected.
- 5 unit tests pin the behaviour.

**Fresh-install execution: PASS (2026-11-24).** A fresh Supabase project was
migrated and verified: 3 migrations applied, second run empty, 13 tables, all
named indexes and unique/foreign-key constraints, provider CHECK accepts
`freeai`, checkpoint update and atomic claim exercised inside a rolled-back
transaction, zero leftover rows. The destructive 16-test suite still requires a
disposable database and remains BLOCKED.

---

## 6. Gemini and Groq readiness

| Check | Gemini | Groq |
|---|---|---|
| Endpoint | `POST /v1beta/models/{model}:generateContent` | `POST /openai/v1/chat/completions` |
| Auth | `x-goog-api-key` header (never in the URL) | `Authorization: Bearer` |
| Model id | resolved from `/models`, preference list, optional pin | same, excludes whisper/guard/tts/orpheus from blind fallback |
| Structured output | `responseMimeType: application/json` + Pydantic validation | `response_format: json_object` + same validation |
| Timeout | 20 s default, no silent retries | same |
| 429 | `rate_limit` with `Retry-After`; provider disabled for the run | same |
| 5xx / transport | `unavailable`; two failures disable the provider for the run | same |
| Invalid JSON | `invalid_response` → fall through to the next provider | same |
| Fallback | Gemini → Groq → deterministic | Groq is last before deterministic |
| Token limits | `maxOutputTokens=800`; prompt bounded to 3,000 chars | `max_tokens=800` |
| Usage | every call recorded to `provider_usage` (provider, model, ok, kind, latency, tokens) | same |
| Redaction | prompt scrubbed with the global redactor before send; no secret in error text | same |

Current model landscape was checked against the providers' documentation on
2026-11-23: Gemini free-tier limits are per model and must be read in AI Studio;
Groq's published table lists `openai/gpt-oss-120b` at 30 RPM / 1,000 RPD and
`qwen/qwen3.8-27b` at the same base limits, both of which appear in the preference
list. Runtime resolution remains the correct strategy because ids change.

**Status: PARTIAL.** The request/response shape and failure handling are unit-tested
with mock transports; no real API call was made. **BLOCKED** on the keys.

Edge cases worth knowing: a model that rejects `json_object` returns 400 and is
treated as `unavailable`; a cached deterministic analysis is reused for the same
prompt fingerprint until the cache is pruned (45 days), even after keys are added.

### free.ai (added 2026-11-24, after the audit snapshot)

| Check | Finding |
|---|---|
| Endpoint | `POST https://api.free.ai/v1/chat/`, OpenAI-compatible |
| Auth | `Authorization: Bearer sk-free-...` |
| Models | self-hosted `qwen7b` (default, documented) plus external ids from `/v1/models` |
| Free allowance | 30,000 tokens/day, 10 requests/minute, `402` when exhausted |
| Structured output | not documented; prompt-constrained JSON + Pydantic validation |
| Budget | rolling-24h `FREEAI_DAILY_TOKEN_BUDGET` (25,000 default) and 2 calls/run |
| Fallback | free.ai → Gemini → Groq → deterministic |
| Status | **PARTIAL** — adapter unit-tested against a mock transport; no live key test (BLOCKED) |

The daily pool cannot carry the full hourly pipeline. It is a budget-capped
preferred provider, not a replacement for Gemini/Groq; this is a deliberate
capability judgement, not a claim that 30K tokens/day is sufficient.

---

## 7. Telegram readiness

| Check | Finding |
|---|---|
| Bot configuration | `getMe` is used by `doctor`; the token never appears in logs (scrubbed URL, registered secret) |
| Chat id | supplied as a string; groups use the negative id |
| Formatting | HTML parse mode; every dynamic value escaped; 3,800-char render cap, 4,096 API cap |
| Message content | title, priority icon/label, category, confidence, summary, what is new, why it matters, practical use case, capabilities, pricing/licence, publication date, evidence link, verdict |
| Rate limits | 1.2 s minimum between sends; 429 honours `retry_after` and defers |
| Queue | `PENDING → SENDING → SENT / RETRY_WAIT / UNKNOWN`; intent persisted before the call; `FOR UPDATE SKIP LOCKED` claims |
| UNKNOWN | ambiguous timeout is never auto-retried; the run degrades and reports it |
| Duplicates | `UNIQUE (discovery_id, channel)` plus a queue-time check; exactly-once is **not** claimed |
| Cap | 5 per run, enforced as the minimum of policy, scoring and environment |
| Priority gate | only CONFIRMED, priority ≥ 40 (3/5), confidence ≥ 0.57, age ≤ 48 h |
| Digest sequencing | 22:35 IST, after the 22:30 run *starts*; a run delayed past 22:35 is not included |

**Status: PARTIAL.** Formatting, state machine and failure paths are unit-tested;
real delivery is **BLOCKED**.

Edge cases: a URL containing a literal `"` could produce malformed HTML because
`esc()` passes `quote=False`; Telegram would reject that message (`bad_request`,
permanent for that item). A crash after `sendMessage` but before `mark_delivery_sent`
leaves `SENDING`, which maintenance reports.

**Test procedure (do not run yet):** private chat → set secrets → `notify --test` →
confirm one message → run `research --dry-run` → confirm silence → wait for a
qualifying discovery or dispatch with `dry_run=false`.

---

## 8. Google Drive readiness

| Stage | Finding |
|---|---|
| Authorization URL | `https://accounts.google.com/o/oauth2/v2/auth`, `access_type=offline`, `prompt=consent`, `include_granted_scopes=true` |
| Redirect URI | `http://127.0.0.1:<port>/oauth2callback` (default 8765), valid for a Desktop client |
| Scope | `drive.file` only |
| Token exchange | form-encoded POST, `grant_type=authorization_code` |
| Refresh handling | token cached until 60 s before expiry; exchange repeated on demand |
| Folder | app-owned `AI Radar Backups`; `drive.file` cannot write into a user-created folder |
| Upload | multipart/related, JSON metadata part, `appProperties` checksum |
| Manifest | `backup_manifests` row before upload; status `PENDING → UPLOADED → VALIDATED` (or `FAILED`) |
| Checksums | SHA-256 of the gzip payload in Drive `appProperties`; `tables_sha256` inside the payload |
| Download | raw bytes, 64 MB cap |
| Verification | after upload and again on restore; a backup with no checksum is refused |
| Restore | separate DSN required; truncate + load in one transaction; per-table count comparison |
| Failure handling | upload/download/checksum failures mark `FAILED`, never `VALIDATED`; retention only prunes after a newer validated backup |

OAuth consent notes:

- An External app left in **Testing** issues refresh tokens that expire after
  **7 days**. Publish to **In production** before the first weekly backup.
- Production publishing does not make a refresh token immortal: the user can revoke
  the grant, a password change or long inactivity can invalidate it, and there is a
  per-client/user token limit. The weekly backup failing is the signal; the fix is
  to re-run the helper.
- Unverified apps show a warning; `drive.file` is a limited scope and acceptable for
  personal use.

**Status: PARTIAL.** The entire lifecycle is unit-tested against a mock transport;
no real upload, download or restore has run. **BLOCKED.**

---

## 9. Source quality

24 sources: 22 Tier A, 2 Tier B, 0 Tier C. Methods: 18 RSS/Atom, 5 GitHub
releases, 1 Hugging Face. Coverage: models 5, coding 4, agents 3, image/video 4,
audio 3, productivity 3, industry 2.

All 24 returned HTTP 200 and 436 entries on a fresh live probe on 2026-11-23
(recorded in `artifacts/source_check_20261124.txt`).

| Aspect | Finding |
|---|---|
| Official status | 22 are primary: company newsrooms, official changelogs, official GitHub releases, publisher model cards. `techcrunch_ai` and `verge_ai` are Tier B reporting, correctly labelled, and cannot alone establish a fact. |
| Category fit | Matches the seven required categories; no source is duplicated across ids. |
| Method fit | RSS where a stable feed exists; GitHub releases for release facts; HF Hub for model cards. No HTML scraping, no JS rendering. |
| URL validity | Probed live; every URL is HTTPS. |
| Tier discipline | Tier C-only discoveries are capped at 0.49 confidence and discarded; no Tier C source is enabled, which is a deliberate quota decision documented in `sources.md`. |
| Checkpoint handling | ETag/Last-Modified stored per source; 304 advances the checkpoint without re-processing; checkpoint advances only after persistence. |
| Duplicate behaviour | canonical URL + normalized title/publisher + source ids; DB uniqueness on `canonical_key`; evidence rows merged per discovery. |
| Last successful collection | **BLOCKED** — no database, so per-source `last_success_at` has never been written. |

Weaknesses:

1. `hf_image_models` returned **0 entries**: the substance gate (3 likes / 200
   downloads) filtered the entire newest cohort. Healthy but quiet, and image-model
   recall is thin.
2. Two Tier B sources cover "industry updates" with only 20 and 10 entries at probe
   time; the category is the least deep.
3. Anthropic, Mistral, Cursor, Windsurf and several others have no usable feed and
   were rejected at probe time (recorded in `sources.md`).

**Status: PASS for validity and tiering; PARTIAL for recall.**

---

## 10. Confidence threshold evaluation

The operating threshold is **0.57**, derived from `tests/evaluation/labels.jsonl`.

| Property | Value |
|---|---|
| Cases | 24 |
| Positives / negatives | 14 / 10 |
| Tier sets | A: 14, B: 6, C: 4 |
| Verdicts | CONFIRMED 11, PARTIAL 5, INSUFFICIENT 6, DISPROVED 2 |
| Domains | 1 domain: 15, 2: 6, 3: 3 |
| Score range | positives 0.61–1.00, negatives 0.00–0.53 |
| Separation | clean gap; threshold = midpoint |
| Reported precision/recall | 1.0 / 1.0 on this set |

Why this is **not** a validated production threshold:

1. **Circular construction.** Each case is defined by the same five facts the
   confidence function consumes (`verdict`, `tiers`, `domains`, `reachable`,
   `date_verified`). Perfect separation is therefore expected, not evidence of
   real-world accuracy.
2. **No held-out set.** The threshold and the reported metrics use the same 24
   cases; there is no test split.
3. **Class and domain imbalance.** 14/10 overall, but only 4 Tier C cases and 3
   three-domain cases. Borderline evidence (Tier B with two domains, gated pages,
   stale-but-real announcements) is under-represented.
4. **No temporal or publisher spread.** Cases are hand-written descriptions, not
   sampled from the 24 live sources across several weeks.

What would justify a production claim:

- ≥ 100 labelled cases per class, sampled from real collected items over ≥ 4 weeks;
- stratification by category, tier, domain count, reachability and date quality;
- a held-out test split, with precision at the alert threshold measured separately
  from recall;
- an explicit target (e.g. "≤ 1 false alert per 100 delivered alerts") and a
  documented failure budget;
- periodic re-labelling as sources and model behaviour change.

Until then `config/confidence_thresholds.json` correctly records `n_samples: 24`
and the caveat. **Status: PARTIAL (seed calibration only).**

---

## 11. Cloud cost and scheduling risks

| Item | Value |
|---|---|
| Research cron | `30 2-16 * * *` = 15 runs/day = 450/month |
| Digest | `5 17 * * *` = 22:35 IST daily |
| Backup | `30 17 * * 0` = Sunday 23:00 IST |
| Maintenance | `15 1 * * *` = 06:45 IST |
| Estimated research duration | 3–6 min/run (install ≈ 40 s + 24 sources + ≤ 40 analyses + ≤ 120 probes) |
| Estimated research minutes/month | 1,350–2,700 |
| Other jobs | digest 30–60 min, maintenance 30–60 min, backup ~10 min, tests on push |
| GitHub Free, private repo | 2,000 min/month → **the 6-minute estimate exceeds it** |
| GitHub Free, public repo | standard runners free |
| Supabase Free | 500 MB, 5 GB egress, pauses after 1 week idle, no automatic backups |
| Gemini Free | per-model RPM/TPM/RPD; worst case 600 requests/day at 40 × 15 |
| Groq Free | ~1,000 RPD for `gpt-oss-120b` |
| Drive | 15 GB shared |

Risks, in order:

1. **Private-repo minutes.** At 6 min/run, 2,700 minutes exceeds the 2,000 quota;
   runs then fail until the month resets or the repo is made public. Mitigation:
   public repo, or 8 runs/day (every two hours) in both config and workflow.
2. **Digest race.** A research run delayed past 22:35 IST is not represented in that
   day's digest. The digest is idempotent per IST date; `--resend` exists.
3. **Gemini RPD.** A busy launch day can exhaust the daily quota; Groq and
   deterministic cover it, at lower quality.
4. **Supabase pause** after a week without activity — hourly runs prevent it, but a
   paused project fails every job until resumed.
5. **60-day inactivity disable** for scheduled workflows in public repos — the
   external heartbeat notices; a monthly keepalive commit is the fix.
6. **Supabase 500 MB** after years of discoveries; no automatic archival.

₹0/month is **not guaranteed**. It holds if the repo is public, the free tiers are
not upgraded, and the cadence stays as configured. No code path can create a paid
resource.

---

## 12. Deployment blockers

| # | Blocker | Owner action | Blocks |
|---|---|---|---|
| 1 | ~~No Supabase project / DSN~~ **resolved 2026-11-24** | session pooler 5432 connected; migrations and verifier passed | destructive DB suite only |
| 2 | No Telegram bot | BotFather, chat id, private chat | delivery, digest |
| 3 | No Gemini key | AI Studio | primary analysis |
| 4 | No Groq key | Groq console | fallback analysis |
| 5 | No Google OAuth client/grant | Cloud console + helper | backup, restore |
| 6 | No healthchecks check | create 1h/11h check | external monitoring |
| 7 | No GitHub repository/secrets | push, set 9 secrets | cloud execution |
| 8 | No PC-off window | shut down for a full day | acceptance |

Additionally **NOT_TESTED** until the above exist: fresh migrations, live provider
contracts, Telegram delivery, Drive upload/download/restore, scheduled runs, and the
digest on a real day.

---

## 13. Exact next actions

1. Read `docs/cloud_integration_checklist.md`; execute sections 0 and A–G.
2. Use the **session pooler on 5432**; the app refuses 6543.
3. Set secrets with `gh secret set`, never by pasting into a file.
4. Run `python -m ai_radar backup`, then `restore --verify-only`, then a real
   restore into a second database.
5. Execute sections H–I: one dry run, then one controlled alert.
6. Enable schedules; observe three consecutive runs and one digest.
7. Perform the PC-off test; record it in §14 and in
   `docs/final-report.md`.
8. Update `README.md` status and `docs/final-report.md` only with evidence from
   those steps.

Do not:
- enable billing on Gemini/Groq, or a Supabase paid plan, to "make it work";
- set `GOOGLE_DRIVE_FOLDER_ID` to a folder you created by hand;
- retry an `UNKNOWN` delivery blindly;
- edit an applied migration;
- claim PC independence before the test.

---

## 14. PC-off acceptance criteria

`PC_INDEPENDENCE_STATUS = NOT_VERIFIED`

Run the whole window with the PC **powered off** (not sleep). Every row must pass.

| # | Criterion | Evidence to record | Result |
|---|---|---|---|
| 1 | GitHub Actions runs at scheduled times | 3 consecutive `workflow_runs` rows with `started_at` in the window | not performed |
| 2 | Research sources are collected | `entries_collected > 0`, `sources_failed = 0` | not performed |
| 3 | AI provider calls work | `analysis_results.provider` in (`gemini`, `groq`); `provider_usage` rows | not performed |
| 4 | Supabase records are created | new `discoveries` with `discovered_at` in the window | not performed |
| 5 | Telegram messages arrive | alert and/or digest on the phone; `deliveries.status = SENT` | not performed |
| 6 | Daily digest is generated | `daily_digests` row for the IST date with a non-null `telegram_message_id` | not performed |
| 7 | External heartbeat updates | healthchecks.io pings aligned with run completion | not performed |
| 8 | Drive backup completes | Sunday `backup_manifests.status = VALIDATED` | not performed |
| 9 | Recovery works after a missed run | `recovered_missed_runs > 0` in a later run, or a deliberately skipped slot recovered | not performed |

If any row is missing, the status stays `NOT_VERIFIED` and the window is repeated.

---

## Recommendation

**READY FOR CLOUD CONFIGURATION.**

Reasons: the codebase is internally consistent and locally proven; the three
additional defects found in this audit are fixed with regression tests; the
deployment path is documented down to the exact connection string; and no code
blocker remains for starting the cloud setup.

Conditions: the database, providers, Telegram, Drive, schedules and PC-off test are
still **unexecuted**. Production readiness is not claimed and must not be claimed
until §14 passes. If the first live `migrate`, provider call, alert or restore
fails, return to `pre_deployment_audit.md` for the matching failure and recovery
procedure rather than changing code blindly.

---

## Addendum — 2026-11-24 (post-audit fixes and fresh evidence)

This section supersedes the test counts and open integrations above. It records
what was re-executed after the audit, and three defects found by executing rather
than reading.

### A1. Fresh verification evidence

| Check | Command | Result |
|---|---|---|
| Full offline suite (Python 3.12.10, clean venv from `requirements.lock`) | `python -m pytest tests -o addopts="" -q` | **321 passed, 20 skipped, 3.47 s** |
| Byte compilation | `python -m compileall -q src` | exit 0 |
| Source registry | `ai-radar sources --validate` | 24 sources, 7 categories, `problems: []` |
| Config + DB + migrations + cron drift | `ai-radar doctor --database-only` | 5/5 PASS; 3 migrations applied |
| Database schema and write paths | `python scripts/verify_database.py` | **14/14 PASS**, 13 tables, row counts/constraints/FKs correct, 0 leftover rows |
| Live collection, all sources | `ai-radar collect` | **24/24 sources, 436 entries** |
| Secret scan over `git ls-files` | pattern scan (token/API-key/PEM/DSN shapes) | 5 hits, all synthetic test fixtures (`FakeTokenForRedactionTestOnly`, `AIzaSyAAAA…`); `.env` untracked |

### A2. Defects found and fixed in this pass

1. **Placeholders were used as credentials (production-impacting).**
   `.env.example` promises that its `<…>` values are not credentials, but
   `Settings` loaded them verbatim. Copying the file to `.env` made the GitHub
   collector send `Authorization: Bearer <github-read-only-pat>`, so **all five
   GitHub release sources returned HTTP 401** (observed live: 19/24 sources). A
   placeholder bot token or provider key would likewise turn a clear
   "not configured" into a misleading auth error.
   *Fix:* `settings.looks_like_placeholder()` blanks documented placeholder shapes
   for every credential field. *Evidence:* live collection went from 19/24 to
   **24/24** after the fix. *Tests:* `tests/unit/test_settings_placeholders.py`.

2. **`FREEAI_API_KEY` was never passed to any workflow (deployment-blocking).**
   The key is documented as required in `deployment.md`, but `research.yml` did
   not set it. Because the router only constructs a provider when its key exists,
   the **preferred provider would silently never run in production** and every
   analysis would start at Gemini.
   *Fix:* `research.yml` now passes `FREEAI_API_KEY` and the optional
   `FREEAI_MODEL` variable; docs and the checklist name the secret.
   *Tests:* `tests/unit/test_workflows.py` asserts every provider secret is wired.

3. **A stored ETag would have been reported as a source failure.** For the JSON
   collectors, a `304 Not Modified` produced an empty body that `fetch_json`
   parsed as invalid JSON, and the GitHub/Hugging Face collectors never persisted
   the validators in the first place — so conditional requests could never work
   even though the registry claims ETag support.
   *Fix:* `fetch_json_document()` returns the payload plus the response, raises
   `kind="not_modified"`, and both collectors now persist `ETag`/`Last-Modified`
   and treat `304` as "unchanged". *Tests:* two new collector tests.

Also fixed: `"` in a URL was not escaped inside `href="…"` (Telegram rejects the
whole message permanently with `bad_request`); the user-facing label conflated
open weights with open source, contradicting the pricing policy; free.ai was
missing from the maintenance quota report; `pytest`/`pytest-asyncio` were
unpinned in CI.

### A3. Telegram — still open

A read-only `getMe` was attempted once: it **timed out after 20 s**, with the bot
token correctly redacted in the error. DNS and TCP 443 to `api.telegram.org`
answered in 0.18 s, so this is an HTTP-layer timeout from this PC, not a blocked
network and not a token verdict. `TELEGRAM_CHAT_ID` is not configured, so no
message was or could be sent. **Telegram authentication: UNVERIFIED.**

### A4. Remaining gates (unchanged, all owner-side)

| Gate | Status | Blocking cause |
|---|---|---|
| Telegram authentication + one approved delivery | BLOCKED | no chat id; `getMe` timeout; approval required to send |
| Gemini / Groq live contracts | NOT TESTED | no keys |
| Drive backup, checksum and restore rehearsal | BLOCKED | no OAuth grant; no disposable database |
| Destructive PostgreSQL suite (16 tests) | BLOCKED | needs a disposable database |
| Cloud deployment and schedules | BLOCKED | no Git remote and no `gh` CLI on this PC |
| PC-independence acceptance | NOT VERIFIED | requires a real cloud run with the PC off |

`PC_INDEPENDENCE_STATUS = NOT_VERIFIED`
