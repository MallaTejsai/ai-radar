# Backup and restore

Supabase's free plan has **no automatic backups**, so the system makes its own and
verifies them. A backup that has only been uploaded is not a backup; it becomes one
after it has been downloaded again and its checksum matched.

## 1. What a backup contains

One gzip-compressed JSON document, produced inside a repeatable-read transaction so
every table comes from the same snapshot:

```json
{
  "manifest": {
    "backup_id": "backup-20261122T173001Z",
    "schema_version": "1.0.0",
    "code_version": "1.0.0",
    "created_at": "2026-11-22T17:30:01+00:00",
    "record_counts": {"discoveries": 1284, "...": 0},
    "tables_sha256": "…",
    "excluded_tables": {"analysis_cache": "derived cache - rebuilt on demand", "...": "…"}
  },
  "tables": {"sources": [...], "discoveries": [...], "...": []}
}
```

| Backed up | Why |
|---|---|
| `sources` | registry state, checkpoints, health |
| `discoveries` | the research memory itself |
| `discovery_sources` | evidence rows behind each discovery |
| `analysis_results` | model output and provider metadata |
| `pricing_history` | how a pricing claim changed over time |
| `deliveries` | Telegram delivery state machine |
| `workflow_runs` | run reports, recovery history |
| `daily_digests` | what was sent, and when |
| `backup_manifests` | the archive's own audit trail |
| `system_state` | last successful run, heartbeat bookkeeping |

| Excluded | Why |
|---|---|
| `analysis_cache` | derived; rebuilt on demand |
| `provider_usage` | metering, not memory |
| `schema_migrations` | recreated by the migration runner |

## 2. One-time Google Drive setup

Use OAuth2 with a refresh token. **A service account will not work**: it has its own
storage quota, so it cannot own files in your Drive.

1. <https://console.cloud.google.com> → create or select a project.
2. **APIs & Services → Library → Google Drive API → Enable.**
3. **OAuth consent screen** → External → fill app name and support email →
   add scope `https://www.googleapis.com/auth/drive.file` → add your own account as
   a test user.
4. **Publish the app** (Publishing status → *In production*). This matters: an app
   left in *Testing* issues refresh tokens that expire after **7 days**, which would
   break the weekly backup a week after setup with `invalid_grant`.
   An unverified app is fine for personal use; Google shows a warning you can
   dismiss via *Advanced*. Publishing does not make the token permanent: a user
   revocation, a password change or long inactivity can still invalidate it, and
   the weekly failure is the signal.
5. **Credentials → Create credentials → OAuth client ID → Desktop app.** Copy the
   client id and secret.
6. Run the helper on the PC:

```powershell
$env:GOOGLE_OAUTH_CLIENT_ID = "....apps.googleusercontent.com"
$env:GOOGLE_OAUTH_CLIENT_SECRET = "...."
python scripts/google_oauth_authorize.py
```

It opens the consent page, catches the callback on `127.0.0.1:8765`, prints the
refresh token once and does not write it to disk. If the browser cannot reach the
callback, use `--manual`.

7. Add the secrets to GitHub:

```powershell
gh secret set GOOGLE_OAUTH_CLIENT_ID
gh secret set GOOGLE_OAUTH_CLIENT_SECRET
gh secret set GOOGLE_OAUTH_REFRESH_TOKEN
```

### The folder

Leave `GOOGLE_DRIVE_FOLDER_ID` **empty**. The `drive.file` scope only grants access
to files the app created (or files you explicitly opened with it); it cannot write
into a folder you created in Drive, and doing so fails with `404 File not found`.
When the variable is empty the app finds or creates a folder called
**AI Radar Backups** on first use and reuses it.

Only set `GOOGLE_DRIVE_FOLDER_ID` if you have granted the app access to that folder
another way (for example by opening a file in it through the app).

## 3. Schedule and retention

| Item | Value |
|---|---|
| Schedule | `backup.yml`, Sunday 23:00 IST (`30 17 * * 0`) |
| Retention | 12 archives (~3 months) |
| Deletion rule | oldest files deleted only after a newer backup is `VALIDATED` |
| Manual run | Actions → `backup` → Run workflow (`verify_only` to just check) |

## 4. The integrity chain

Four independent checks, in order:

1. **Before upload** — local SHA-256 of the gzip payload matches the manifest, the
   payload decompresses, and the uncompressed size matches.
2. **After upload** — Drive's reported size equals the local size, and the
   `appProperties.sha256` the app attached equals the manifest checksum.
3. **After re-download** — the downloaded bytes hash to the same value. Only then
   does `backup_manifests.status` become `VALIDATED`.
4. **On restore** — the download is hashed again and compared with the checksum
   stored on the Drive file; the inner `tables_sha256` is recomputed and compared.
   A backup with no recorded checksum is refused, not accepted.

`npm`-style "HTTP 200 means success" reasoning is deliberately not used anywhere in
this path.

## 5. Restore procedure

Restoring into production is refused by design: `restore` compares host, port and
database name of `RESTORE_DATABASE_URL` against `DATABASE_URL` and aborts when they
match.

1. Create a second Supabase project (or an empty database) — this is the drill
   target, not the live one.
2. Put its DSN in `RESTORE_DATABASE_URL`.
3. Verify without writing:

```powershell
ai-radar restore --verify-only
```

Expected shape:

```json
{
  "backup_id": "backup-20261122T173001Z",
  "payload_bytes": 184320,
  "tables": 10,
  "rows": 1284,
  "verified": true
}
```

4. Restore into the isolated database:

```powershell
ai-radar restore
# or a specific archive:
ai-radar restore --file-id <drive-file-id>
```

The service applies migrations to the target, truncates only the known memory
tables, loads every row from the archive, and compares per-table counts with the
manifest. Any mismatch is an error.

5. Spot-check the result:

```sql
select count(*) from discoveries;
select max(discovered_at) from discoveries;
select status, count(*) from deliveries group by status;
```

6. Only after that should you consider repointing `DATABASE_URL` at the restored
   database. Keep the old database until the new one has served a full day.

### Restore drill cadence

- After any schema migration.
- Quarterly otherwise.
- Automatically after each weekly backup when the repository variable
  `RESTORE_REHEARSAL=enabled` is set; the rehearsal job uses
  `RESTORE_DATABASE_URL` and never the production DSN.

## 6. Failure modes

| Symptom | Meaning | Action |
|---|---|---|
| `invalid_grant` on token exchange | refresh token expired (app in Testing) or revoked | publish the OAuth app, re-run the helper, update the secret |
| `insufficientPermissions` / 403 | Drive API not enabled, or scope not granted | enable the API, re-consent |
| `storageQuotaExceeded` | Drive full (15 GB shared with Gmail/Photos) | free space or move to a Workspace account |
| `upload rejected (404)` with a folder id | user-created folder is outside `drive.file` | clear `GOOGLE_DRIVE_FOLDER_ID` and let the app create its folder |
| `checksum mismatch` on restore | payload changed or truncated in transit | try an older archive; investigate Drive |
| `backup carries no recorded SHA-256` | archive made by a build before the checksum chain existed | verify manually with `sha256sum`, then treat it as untrusted |
| `row counts do not match the manifest` | partial load | do not use the restored database; re-run restore |
| Backup row stays `UPLOADED` | process died between upload and re-download | run `ai-radar restore --verify-only`; it validates the newest file |

## 7. Recovery objectives

| Metric | Value | Why |
|---|---|---|
| RPO (data loss window) | up to 7 days | weekly schedule; run `backup` manually before risky changes |
| RTO (time to working database) | ~15 minutes | create project, set DSN, `ai-radar restore` |
| Backup size | tens to hundreds of KB | gzip JSON; 12 archives fit comfortably in 15 GB |

## 8. What is not covered

- Telegram message history: `daily_digests` stores what was sent, not the rendered
  Telegram state.
- Provider credentials: secrets are not in the backup, by design. Losing them means
  re-issuing them.
- Files outside this app's Drive scope.
