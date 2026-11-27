# AI Radar - apply STAGING migration 004 only (provider CHECK constraint)
#
# Read-only inspection -> doctor -> existing migration runner -> doctor ->
# read-only verification. No research, no provider call, no Telegram, no data
# change. The staging DSN is loaded silently from Windows Credential Manager by
# the shared helper; it exists only in this process's environment, is never
# printed and is never written to the transcript.
#
# Aborts on any unexpected condition. Migration 004 is the only file the runner
# may apply, and that is asserted before the runner is invoked.

$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)
$root = (Get-Location).Path
$py = Join-Path $root ".venv312\Scripts\python.exe"
$log = Join-Path $root "artifacts\staging_migration_004_output.txt"

Start-Transcript -Path $log -Force | Out-Null
try {
    Write-Host "=== AI Radar: apply staging migration 004 only ===" -ForegroundColor Cyan
    if (-not (Test-Path $py)) { throw "virtualenv interpreter not found at .venv312\Scripts\python.exe" }

    # Silent retrieval from Windows Credential Manager. If this process already
    # holds the staging DSN (for example when driven by staging_orchestrate.ps1),
    # it is reused without reading the store again.
    . (Join-Path $PSScriptRoot "lib\StagingSession.ps1")
    Initialize-StagingCredential
    if (-not $env:AI_RADAR_STAGING_DATABASE_URL) { throw "no staging DSN in this process" }

    # --- this process only ------------------------------------------------------
    $env:AI_RADAR_STAGING = "1"
    $env:AI_RADAR_STAGING_ALLOW_TELEGRAM = "0"
    $env:FREEAI_API_KEY = ""
    $env:GEMINI_API_KEY = ""
    $env:GROQ_API_KEY = ""
    $env:XAI_API_KEY = ""
    $env:SARVAM_API_KEY = ""
    $env:TELEGRAM_BOT_TOKEN = ""
    $env:TELEGRAM_CHAT_ID = ""
    $env:AI_RADAR_LOG_LEVEL = "INFO"
    $env:PYTHONPATH = Join-Path $root "src"

    Write-Host "`n--- 1/6 read-only pre-check (guards + current DB state) ---" -ForegroundColor Cyan
    $preText = & $py scripts\staging_migration_004_check.py pre 2>&1
    $preExit = $LASTEXITCODE
    $preText | ForEach-Object { Write-Host $_ }
    if ($preExit -ne 0) { throw "pre-check failed (exit $preExit); nothing was applied" }

    Write-Host "`n--- 2/6 migration 004 checksum and SQL ---" -ForegroundColor Cyan
    $sha = (Get-FileHash "database\migrations\004_provider_check.sql" -Algorithm SHA256).Hash.ToLower()
    Write-Host "file: database\migrations\004_provider_check.sql"
    Write-Host "sha256: $sha"
    Write-Host "--- SQL ---"
    Get-Content "database\migrations\004_provider_check.sql" | ForEach-Object { Write-Host $_ }

    Write-Host "`n--- 3/6 doctor --database-only (before) ---" -ForegroundColor Cyan
    $doctorPre = & $py -m ai_radar doctor --database-only 2>&1
    $doctorPreExit = $LASTEXITCODE
    $doctorPre | ForEach-Object { Write-Host $_ }
    $doctorPre | Set-Content "artifacts\staging_migration_004_doctor_pre.txt" -Encoding utf8

    $failLines = @($doctorPre | Select-String -Pattern '^\[FAIL\]' | ForEach-Object { $_.Line })
    $onlyPending004 = ($failLines.Count -eq 1) -and ($failLines[0] -match 'migrations') -and ($failLines[0] -match '004_provider_check')
    if (-not $onlyPending004) {
        throw "doctor reported failures other than the expected pending 004: $($failLines -join ' | ')"
    }
    Write-Host "doctor exit $doctorPreExit - expected at this point: 004_provider_check is the only pending migration"

    Write-Host "`n--- 4/6 apply migration 004 via the existing runner ---" -ForegroundColor Cyan
    $migrateText = & $py -m ai_radar migrate 2>&1
    $migrateExit = $LASTEXITCODE
    $migrateText | ForEach-Object { Write-Host $_ }
    $migrateText | Set-Content "artifacts\staging_migration_004_migrate.txt" -Encoding utf8
    if ($migrateExit -ne 0) { throw "migrate exited $migrateExit; see artifacts\staging_migration_004_migrate.txt" }

    Write-Host "`n--- 5/6 doctor --database-only (after) ---" -ForegroundColor Cyan
    & $py -m ai_radar doctor --database-only
    $doctorPostExit = $LASTEXITCODE
    if ($doctorPostExit -ne 0) { throw "doctor still failing after migration (exit $doctorPostExit)" }

    Write-Host "`n--- 6/6 read-only verification (migrations + constraint + data unchanged) ---" -ForegroundColor Cyan
    & $py scripts\staging_migration_004_check.py post
    if ($LASTEXITCODE -ne 0) { throw "post-check failed (exit $LASTEXITCODE)" }

    Write-Host "`n=== MIGRATION 004 APPLIED - exit code 0 ===" -ForegroundColor Green
    Write-Host "evidence: artifacts\staging_migration_004_output.txt"
    Write-Host "          artifacts\staging_migration_004_pre.json"
    Write-Host "          artifacts\staging_migration_004_migrate.txt"
    Write-Host "          artifacts\staging_migration_004_post.json"
    Clear-StagingCredential
    Stop-Transcript | Out-Null
    exit 0
}
catch {
    Write-Host "`n[ABORT] $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "No research was run, no provider was called, no message was sent." -ForegroundColor Red
    try { Clear-StagingCredential } catch { }
    try { Stop-Transcript | Out-Null } catch { }
    exit 1
}
