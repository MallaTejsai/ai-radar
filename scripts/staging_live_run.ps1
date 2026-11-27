# AI Radar - FIRST LIVE STAGING RESEARCH CYCLE (research -> Telegram)
#
# One cycle only. The staging DSN is loaded silently from Windows Credential
# Manager by the shared helper and exists only in this process's environment for
# the duration of the script.
#
# Order: credential retrieval -> env -> doctor -> read-only preflight -> migration
# 004 (if and only if it is the only pending migration) -> ONE research run ->
# read-only verification. Every gate aborts the script; nothing is retried.

$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)
$root = (Get-Location).Path
$py = Join-Path $root ".venv312\Scripts\python.exe"

function Abort([string]$message) {
    Write-Host "`n[ABORT] $message" -ForegroundColor Red
    Write-Host "No further step was executed." -ForegroundColor Red
    exit 1
}

Write-Host "=== AI Radar: first live staging research cycle ===" -ForegroundColor Cyan
if (-not (Test-Path $py)) { Abort "virtualenv interpreter not found at .venv312\Scripts\python.exe" }
if (Test-Path "artifacts\staging_run_report.json") {
    Abort "artifacts\staging_run_report.json already exists: a cycle has already been attempted. Refusing to run a second one."
}

# Silent retrieval from Windows Credential Manager; reused if this process
# already holds the staging DSN. Prefer staging_orchestrate.ps1, which owns the
# full workflow.
. (Join-Path $PSScriptRoot "lib\StagingSession.ps1")
Initialize-StagingCredential
if (-not $env:AI_RADAR_STAGING_DATABASE_URL) { Abort "no staging DSN in this process" }

# --- provider / runtime configuration for this process only -----------------
$env:AI_RADAR_STAGING = "1"
$env:AI_RADAR_STAGING_ALLOW_TELEGRAM = "1"
$env:FREEAI_API_KEY = ""
$env:MAX_ALERTS_PER_RUN = "1"
$env:SARVAM_MODEL = "sarvam-105b"
$env:SARVAM_MAX_CALLS_PER_RUN = "1"
$env:AI_RADAR_LOG_LEVEL = "INFO"
$env:PYTHONPATH = Join-Path $root "src"

Write-Host "`n--- 1/5 doctor (database only) ---" -ForegroundColor Cyan
& $py -m ai_radar doctor --database-only
if ($LASTEXITCODE -ne 0) { Abort "doctor --database-only failed (exit $LASTEXITCODE)" }

Write-Host "`n--- 2/5 read-only preflight ---" -ForegroundColor Cyan
& $py scripts\staging_preflight.py
if ($LASTEXITCODE -ne 0) { Abort "preflight failed (exit $LASTEXITCODE); no research was run" }

$pre = Get-Content "artifacts\staging_preflight.json" -Raw | ConvertFrom-Json
$applied = @($pre.checks.migrations)
$all = Get-ChildItem "database\migrations\*.sql" | ForEach-Object { $_.BaseName }
$pending = @($all | Where-Object { $applied -notcontains $_ })

Write-Host "`nApplied migrations: $($applied -join ', ')"
Write-Host "Pending migrations: $(if ($pending.Count) { $pending -join ', ' } else { 'none' })"

Write-Host "`n--- 3/5 migration gate ---" -ForegroundColor Cyan
if ($pending.Count -gt 0) {
    if (-not ($pending.Count -eq 1 -and $pending[0] -eq "004_provider_check")) {
        Abort "unexpected pending migrations ($($pending -join ', ')); only 004_provider_check may be applied"
    }
    $sha = (Get-FileHash "database\migrations\004_provider_check.sql" -Algorithm SHA256).Hash.ToLower()
    Write-Host "Applying 004_provider_check.sql (sha256 $sha)"
    & $py -m ai_radar migrate
    if ($LASTEXITCODE -ne 0) { Abort "migrate failed (exit $LASTEXITCODE)" }

    & $py -m ai_radar doctor --database-only
    if ($LASTEXITCODE -ne 0) { Abort "doctor failed after migration" }
    & $py scripts\staging_preflight.py
    if ($LASTEXITCODE -ne 0) { Abort "preflight failed after migration; no research was run" }
    $pre = Get-Content "artifacts\staging_preflight.json" -Raw | ConvertFrom-Json
    if (-not (@($pre.checks.migrations) -contains "004_provider_check")) {
        Abort "migration 004 is still not recorded as applied"
    }
} else {
    Write-Host "004_provider_check already applied"
}

# --- 4. the single research cycle -------------------------------------------
$startedAt = (Get-Date).ToUniversalTime().ToString("o")
Write-Host "`n--- 4/5 ONE research cycle (started $startedAt) ---" -ForegroundColor Cyan
& $py -m ai_radar research --trigger manual 2>&1 | Tee-Object -FilePath "artifacts\staging_research_output.txt"
$researchExit = $LASTEXITCODE
$finishedAt = (Get-Date).ToUniversalTime().ToString("o")

@{ started_at = $startedAt; finished_at = $finishedAt; exit_code = $researchExit } |
    ConvertTo-Json | Set-Content "artifacts\staging_run_timestamps.json" -Encoding utf8

if ($researchExit -ne 0) {
    Write-Host "`n[FAIL] research exited $researchExit. Running read-only verification only; nothing will be retried." -ForegroundColor Red
} else {
    Write-Host "`n--- 5/5 read-only verification ---" -ForegroundColor Cyan
}

& $py scripts\staging_verify.py
if ($LASTEXITCODE -ne 0) { Write-Host "[FAIL] verification could not complete" -ForegroundColor Red; exit 1 }

Write-Host "`n=== done: result files under artifacts\ ===" -ForegroundColor Cyan
Write-Host "  staging_preflight.json"
Write-Host "  staging_research_output.txt"
Write-Host "  staging_verify.json"
Write-Host "  staging_run_timestamps.json"
Clear-StagingCredential
if ($researchExit -ne 0) { exit $researchExit }
exit 0
