# AI Radar V1.0 - read-only staging audit entry point
#
#   .\scripts\staging_audit.ps1
#
# The staging DSN is loaded silently from Windows Credential Manager by the shared
# helper (scripts/lib/StagingSession.ps1); nothing here prompts for it. The audit
# performs SELECT-only queries inside a READ ONLY session. It never runs research,
# calls a provider, sends Telegram, writes to the database, deploys or commits.
#
# The DSN is never written to .env, a file, a log, a command line, Git or a
# report; it is cleared from this process in the finally block.

[CmdletBinding()]
param(
    [string]$LogPath = "artifacts\staging_audit_output.txt"
)

$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)
$root = (Get-Location).Path
$py = Join-Path $root ".venv312\Scripts\python.exe"

. (Join-Path $PSScriptRoot "lib\StagingSession.ps1")

if (-not (Test-Path $py)) {
    Write-Host "[ABORT] virtualenv interpreter not found at .venv312\Scripts\python.exe" -ForegroundColor Red
    exit 2
}

Write-Host "=== AI Radar: read-only staging audit ===" -ForegroundColor Cyan
Write-Host "SELECT-only. No research, no provider call, no Telegram, no writes."

$code = 1
try {
    $logFull = Join-Path $root $LogPath
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $logFull) | Out-Null
    Start-Transcript -Path $logFull -Force | Out-Null

    # Silent retrieval; no prompt exists in the audit path.
    Initialize-StagingCredential
    if (-not $env:AI_RADAR_STAGING_DATABASE_URL) { throw "no staging DSN in this process" }

    $env:AI_RADAR_STAGING = "1"
    $env:AI_RADAR_LOG_LEVEL = "WARNING"
    $env:PYTHONPATH = Join-Path $root "src"
    $env:AI_RADAR_STAGING_ALLOW_TELEGRAM = "0"

    # No paid provider can be reached from this process, even by accident.
    $env:FREEAI_API_KEY = ""
    $env:GEMINI_API_KEY = ""
    $env:GROQ_API_KEY = ""
    $env:XAI_API_KEY = ""
    $env:SARVAM_API_KEY = ""
    $env:TELEGRAM_BOT_TOKEN = ""
    $env:TELEGRAM_CHAT_ID = ""

    & $py "scripts\staging_audit.py"
    $code = $LASTEXITCODE
    Write-Host "`n[INFO] audit exit code: $code" -ForegroundColor DarkGray
}
catch {
    Write-Host "`n[ABORT] $($_.Exception.Message)" -ForegroundColor Red
    $code = 1
}
finally {
    Clear-StagingCredential
    try { Stop-Transcript | Out-Null } catch { }
}

Write-Host "`nevidence: artifacts\staging_audit_report.json" -ForegroundColor Cyan
Write-Host "          artifacts\staging_audit_output.txt" -ForegroundColor Cyan
exit $code
