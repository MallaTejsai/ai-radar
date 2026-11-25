# Staging credential setup (Windows Credential Manager)

AI Radar stores the Supabase **staging** DSN once, in Windows Credential Manager, and
every staging script then loads it silently. You are never asked to paste it again
during normal operation.

- Credential target name: `AI_RADAR_STAGING_DATABASE_URL`
- Backend: Windows Credential Manager, `CRED_TYPE_GENERIC`, persist = local machine
- Protection: DPAPI under your Windows user; another user on the same PC cannot read it
- Never used for production and never a fallback for `DATABASE_URL`

## 1. One-time setup

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/staging_credentials.ps1 -Setup
```

What happens:

1. One hidden prompt (`Read-Host -AsSecureString`) — the DSN is not echoed.
2. Format checks: `postgresql://`, host present, user **and** password present, port not 6543.
3. Isolation check: the staging host/port/user must differ from your `DATABASE_URL`
   (read from the process environment or the `DATABASE_URL` line in `.env`).
   If there is no production DSN to compare against, the database name must look
   disposable (`test`, `ci`, `scratch`, `sandbox`, `stag`).
4. The value is written to Windows Credential Manager, read back, and compared by
   SHA-256 fingerprint. A failed read-back aborts the setup.
5. Only the masked endpoint and the fingerprint are printed. No database is contacted.

Re-running `-Setup` replaces the stored value (use this to rotate).

## 2. Verify

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/staging_credentials.ps1 -Verify
```

Prints exactly four lines and nothing else about the secret:

```
Credential exists: YES|NO
Windows Credential Manager backend: PASS|FAIL
DSN format validation: PASS|FAIL
Staging endpoint identity: PASS|FAIL
```

Exit code `0` only when all four pass. `-Verify` does not open a database connection.

## 3. How scripts retrieve it

Every staging entry point dot-sources the shared helper and calls one function:

```powershell
. (Join-Path $PSScriptRoot "lib\StagingSession.ps1")
Initialize-StagingCredential      # silent; throws with the setup command if unusable
```

`Initialize-StagingCredential`:

1. reuses `AI_RADAR_STAGING_DATABASE_URL` if this process already has it (so a
   multi-step script never re-reads the store, and child processes only inherit a
   controlled process environment);
2. otherwise reads Windows Credential Manager;
3. re-validates the DSN shape and the production isolation rule before exporting it
   to `$env:AI_RADAR_STAGING_DATABASE_URL`;
4. throws — with the exact setup command — if the credential is missing, malformed
   or points at production. It never prompts and never falls back.

Scripts using it: `staging_orchestrate.ps1`, `staging_migration_004.ps1`,
`staging_live_run.ps1`, `staging_audit.ps1`. No staging script implements its own
DSN prompt; `Read-Host` appears only in `staging_credentials.ps1 -Setup`.

Every script clears the DSN and staging variables in a `finally` block:

```powershell
Clear-StagingCredential
```

## 4. Rotating the staging database password

1. Reset the password in Supabase → **staging** project → Settings → Database.
2. Update the session-pooler URI (port `5432`) if the username changed.
3. Store the new value:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/staging_credentials.ps1 -Setup
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/staging_credentials.ps1 -Verify
```

The old password stops working immediately; the stored credential is overwritten,
never appended. If rotation was prompted by exposure, also check any terminal
scrollback or shell history where the old value was typed.

## 5. Removing the stored credential

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/staging_credentials.ps1 -Remove
```

Deletes only the `AI_RADAR_STAGING_DATABASE_URL` entry. It does not touch other
credentials in your vault, `.env`, or the database. Staging scripts then refuse to
run until `-Setup` is run again.

Manual inspection (optional, shows only presence):

```powershell
cmdkey /list | Select-String "AI_RADAR_STAGING_DATABASE_URL"
```

## 6. Security limitations

- **Windows only.** The helper calls `advapi32` (`CredReadW`/`CredWriteW`/`CredDeleteW`)
  via P/Invoke. Linux/macOS CI must inject `AI_RADAR_STAGING_DATABASE_URL` as an
  environment secret; nothing there reads Credential Manager.
- **No new dependency.** `keyring` and `pywin32` are not installed and are not
  required; the native API is used directly.
- **Local machine persistence.** The entry survives logoff (persist = local machine)
  and is decryptable only by your Windows user profile on this PC. It is not
  available to other machines or other users.
- **Managed memory cannot be wiped.** PowerShell/.NET strings are immutable, so a
  transient plaintext copy exists in the process heap until garbage collection.
  The helper minimises the window (BSTR zeroing, array clearing, immediate nulling)
  but cannot guarantee erasure, and a process dump or debugger running as you could
  read it.
- **Anyone with your unlocked Windows session can use the credential.** Credential
  Manager does not add a second factor. Lock the PC and use a strong account
  password; do not share the profile.
- **The value is not encrypted at rest by this repository.** It is protected by
  Windows; a backup of your profile or a memory dump is outside that boundary.
- **Not a secret manager for multiple operators.** Each operator needs their own
  Windows account and their own `-Setup` run.
- The setup and verify commands never contact a database, so they cannot detect a
  wrong *password* — only a wrong shape or a wrong endpoint. The first actual
  connection is the password test.
