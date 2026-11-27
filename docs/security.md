# Security

AI Radar reads untrusted pages, holds credentials for four services, and runs
unattended. The threat model follows from that.

## 1. What is worth attacking

| Asset | Where it lives | Consequence if lost |
|---|---|---|
| Supabase DSN | GitHub secret | full read/write of research memory |
| Telegram bot token | GitHub secret | anyone can post to your chat as the bot |
| Google refresh token | GitHub secret | access to files this app created in Drive |
| free.ai / Gemini / Groq keys | GitHub secret | quota theft, billing if a paid tier were enabled |
| GitHub token | automatic, read-only | repository metadata only |
| The repository itself | GitHub | code tampering, workflow changes |

There is no user account system, no personal data store, and no inbound network
service. The attack surface is outbound HTTP plus CI configuration.

## 2. Controls that are implemented

### Secrets

- Secrets are read from the environment only (`settings.py`), never from a file in
  the repository. `.env` is git-ignored; production values are GitHub encrypted
  secrets.
- Every value is registered with a redactor at startup (`utils/redact.py`). The
  redactor also masks secret-shaped strings it has never seen: Postgres DSNs,
  `bot<id>:<token>` URL paths, `Bearer ...`, `key=`/`token=` query parameters,
  GitHub PATs, `AIza...` (Gemini), `gsk_...` (Groq).
- Logs are structlog events with explicit fields. No request or response bodies are
  logged, and exception strings pass through the redactor.
- Request URLs in error messages are sanitised where the message is built
  (`utils/net.py::_safe_url`): userinfo is dropped and secret-shaped path/query
  segments are masked. This covers logs, CI output and anything persisted.
- Error text written to the database is scrubbed at the persistence boundary too:
  `deliveries.last_error` cannot contain the Telegram bot token, which appears in
  the request URL of a failed send.
- Workflows never echo environment variables; `gh secret set` and the UI both avoid
  the clipboard.

### Outbound requests (SSRF and abuse)

`utils/net.py` wraps every call:

- scheme allowlist (`http`, `https`), no `user:pass@`, ports restricted to 80/443;
- hostnames resolved and rejected when any address is private, loopback,
  link-local, multicast, reserved or a cloud metadata address;
- the same check is applied to **every redirect hop**, and redirects are capped at
  three;
- response bodies are capped (2 MB default) while streaming, not after the fact;
- per-request timeouts, bounded retries with exponential backoff and jitter,
  `Retry-After` support;
- `ETag` / `Last-Modified` conditional requests so an unchanged feed is cheap.

The collector cannot be pointed at an internal service even if a feed item links
to one; the link is rejected and the entry is discarded.

### Untrusted content and prompt injection

Feed titles and summaries are attacker-controlled. Defences are structural
(`intelligence/prompts.py`):

1. Untrusted text is length-bounded (3,000 characters) and placed inside
   `<untrusted_content>` tags.
2. Any `</untrusted...>` sequence in the content is rewritten to `[fence]`, so a
   post cannot close the fence and issue instructions in the clear.
3. Leading `system:` / `assistant:` / `developer:` role markers are neutralised.
4. The system instruction states the fenced text is data, not instructions, and
   forbids inventing pricing, licences or dates.
5. Model output is **advisory**. Pricing, licence, verdict, confidence and
   priority are recomputed deterministically from collected evidence and
   configuration after the model answers. A successful injection can distort
   prose; it cannot set `FREE`, raise confidence, or trigger an alert.
6. Model output is validated by Pydantic against a closed schema. Malformed JSON is
   a provider failure, not a partial write.

The model has no tools: no shell, no SQL, no HTTP, no secret access. Nothing it
returns is executed.

### Database

- One repository module owns every statement (`storage/repository.py`);
  parameters are bound, never interpolated.
- Backup export and restore iterate a fixed table allowlist, not a name taken from
  input.
- Restore refuses to run when the target DSN matches the production host, port and
  database, and truncates only the known memory tables.
- The run lock is a session-scoped PostgreSQL advisory lock, so two workflows
  cannot process the same queue.
- All timestamps are UTC; all state transitions are inside transactions.

### Telegram

- HTML parse mode with `html.escape` on every interpolated value, so a title
  containing `<b>` cannot forge formatting.
- 4,096-character hard limit enforced before the request.
- Delivery intent is persisted before the send; an ambiguous timeout becomes
  `UNKNOWN` and is never auto-retried. **Exactly-once is not claimed and cannot be
  provided by the Bot API.**

### Google Drive

- `drive.file` scope: the app can only touch files it created. It cannot enumerate
  or read the rest of your Drive.
- OAuth2 refresh token, not a service account (a service account has no quota and
  cannot own files in a personal Drive).
- The backup checksum is stored in Drive `appProperties` next to the payload, so a
  restore after total database loss can still verify integrity.
- Retention deletes the oldest files only after a newer backup reached
  `VALIDATED`.

### CI configuration

- Every workflow declares `permissions: contents: read`.
- No `pull_request_target`, no workflow that echoes secrets, no third-party action
  beyond `actions/checkout`, `actions/setup-python` and
  `actions/upload-artifact`.
- `continue-on-error` is used once, for `doctor`, and never around the pipeline
  itself.
- Scheduled jobs run from the default branch only; pull requests cannot deploy.
- Dependabot (`.github/dependabot.yml`) proposes pinned version bumps weekly.

#### Dependency review is gated on repository visibility

GitHub serves the dependency-review API to **public** repositories, and to private
repositories only when they carry a GitHub Advanced Security (or GitHub Code
Security) licence. On an unlicensed private repository the action stops at its
first API call with `403 Forbidden`. Enabling the dependency graph does not change
that, and no `permissions:` block can: the 403 is an entitlement, not a token
scope. Buying a licence and changing visibility are both owner decisions, so the
workflow adapts instead.

`.github/workflows/tests.yml`:

```yaml
dependency-review:
  if: >
    github.event_name == 'pull_request' &&
    github.event.repository.visibility == 'public'
  permissions:
    contents: read
    pull-requests: read
```

Consequences:

- **Public repository** - the job runs and is blocking (`fail-on-severity: high`).
- **Private repository** - the job is **skipped**, which GitHub reports as a neutral
  conclusion. The run is not red, no other job `needs` it, and a `workflow_dispatch`
  or `push` run never starts it.
- The job takes `pull-requests: read` only and sets `comment-summary-in-pr: never`,
  because commenting on the PR would require `pull-requests: write`. The verdict is
  still the job result and the run summary.
- Private-phase supply-chain coverage is unchanged and still blocking where it
  matters: `secret-scan` (working tree + full history) fails the build, the test
  matrix runs on Python 3.12 and 3.13, and `dependency-audit` (`pip-audit`) reports
  advisories without blocking an unrelated fix.

### Cost safety as a security control

There is no code path that can enable a paid API. If every configured provider fails,
the deterministic analyser runs. A misconfigured provider cannot create a bill.

## 3. Recommended hardening not enabled by default

| Item | Why it is not the default | How to adopt |
|---|---|---|
| Least-privilege database role | the same `DATABASE_URL` is used by the migration job, which needs DDL | create a role with `SELECT/INSERT/UPDATE/DELETE` on the memory tables, run migrations once with the owner DSN, then point `DATABASE_URL` at the restricted role and disable the `migrations` job |
| Session pooler only | the app refuses a 6543 transaction-pooler DSN by design | use the shared pooler in session mode (5432) or a direct connection |
| Action pinning by commit SHA | major-version tags are readable and get security patches | pin each `uses:` to a full SHA and let Dependabot bump it |
| Hash-pinned requirements | `requirements.lock` pins versions but not hashes | `pip-compile --generate-hashes` and install with `--require-hashes` |
| Repository ruleset | a solo project rarely needs it | require the `tests` check on `main`, block force-push |
| OAuth app verification | `drive.file` is a sensitive-but-limited scope; unverified apps show a warning | keep the app in "In production" with a single user, or submit for verification |

## 4. Residual risks

| Risk | Impact | Mitigation | Status |
|---|---|---|---|
| Prompt injection changes a summary's tone | misleading text, never a false pricing/priority verdict | deterministic recomputation, evidence links in every alert | mitigated |
| A legitimate looking but false announcement | wrong discovery stored | Tier A requirement, availability probe, confidence gate | mitigated |
| Telegram message sent twice | duplicate alert | `UNKNOWN` state, no auto-retry, reconciliation in `maintenance` | accepted, impossible to fully remove |
| Telegram token persisted in an error row | credential leak into the DB and backups | scrubbed before persistence (fixed in the 2026-11-24 audit) | fixed |
| Free-tier provider disappears or changes limits | analysis degrades to deterministic | provider chain, quota tracking, health checks | mitigated |
| GitHub disables scheduled workflows after 60 days of repository inactivity (public repos) | silent stop | external heartbeat pages; monthly keepalive commit | detected, manual action |
| Supabase pauses an idle project | runs fail until resumed | hourly activity, heartbeat alert | detected |
| A paid provider cap is bypassed by concurrency | unbudgeted spend | atomic slot reservation before the HTTP call; concurrency regression test | fixed 2026-11-25 |
| Laptop or phone compromise | repo access, not production secrets | secrets live in GitHub; rotate on suspicion | accepted |
| Project folder inside OneDrive | `.env` synced to cloud, file locks | keep the repo outside OneDrive (see setup-windows.md) | operational |

## 5. Incident response

1. **Contain** — disable `research.yml` and `backup.yml` in the Actions UI.
2. **Rotate** — Telegram bot token (`/revoke` with `@BotFather`), Supabase database
   password, Gemini key, Groq key, and the Google grant at
   <https://myaccount.google.com/permissions>.
3. **Re-issue** — update GitHub secrets; re-run the OAuth helper to mint a new
   refresh token.
4. **Inspect** — `workflow_runs` for unexpected triggers, `deliveries` for messages
   you did not expect, `backup_manifests` for files you did not create.
5. **Restore if needed** — follow [backup-restore.md](backup-restore.md); restore
   into a new database, never over the live one.
6. **Record** — what leaked, what rotated, what was verified afterwards.

## 5b. Publishing the repository publicly

Before the repository is made public, and on every push afterwards:

```bash
python scripts/check_secrets.py --history
```

The scanner covers every tracked and untracked file plus every line added **or
removed** by every commit. It prints the rule, file and line/commit only. CI runs
the same command in `tests.yml` and fails the build on a finding.

Synthetic fixtures that must contain credential shapes are listed by exact path in
[`.github/secret-scan-allowlist.txt`](../.github/secret-scan-allowlist.txt). Do not
add application code, workflows or reports there.

If a real credential is found:

1. **Rotate it immediately.** Assume it is already public.
2. **Remove it from the current tree** and confirm the scanner is clean.
3. **Rewrite history** before publishing (`git filter-repo --replace-text`, then
   force-push a fresh clone), or publish from a new repository that starts at a
   single reviewed commit. A later commit that deletes the value does not remove
   it from the object database GitHub serves.
4. **Note what leaked**: file, commit, credential type. Never reproduce the value
   in a report, an issue or a chat message.

Non-credential private identifiers (hostnames, project refs, account ids) are not
secrets, but they still reveal infrastructure. Treat them the same way before a
public release: they belong in the rewrite set, not in documentation.

## 6. What the system deliberately does not do

- No inbound webhook or API endpoint, so there is no service to exploit.
- No AI-authored SQL, shell command or HTTP request.
- No scraping behind logins, paywalls or JavaScript.
- No storage of personal data, chat history or user identifiers beyond the chat id.
- No automatic paid-tier fallback.
