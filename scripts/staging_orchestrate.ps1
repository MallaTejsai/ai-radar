# AI Radar - single staging orchestration entry point
#
#   .\scripts\staging_orchestrate.ps1                     # doctor, migration gate, verification
#   .\scripts\staging_orchestrate.ps1 -AuthorizeResearch   # ... then exactly one research cycle
#
# The staging DSN is loaded silently from Windows Credential Manager by the
# shared helper (scripts/lib/StagingSession.ps1). Nothing here prompts for it: if
# it is missing, the run stops and prints the one-time setup command. Every stage
# then executes inside ONE Python process (scripts/staging_orchestrator.py), which
# reads the DSN from this process's environment.
#
# The DSN is never written to .env, a file, a log, a command line, Git or a
# report. Provider keys are blanked unless research was explicitly authorized;
# Telegram is opt-in and only for an authorized run. Everything is cleared in
# the finally block.

[CmdletBinding()]
param(
    [switch]$AuthorizeResearch,
    [string]$LogPath = "artifacts\staging_orchestration_output.txt"
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

# --- authorization comes BEFORE any credential or connection ------------------
if ($AuthorizeResearch) {
    Write-Host "=== AI Radar: staging orchestration (ONE research cycle) ===" -ForegroundColor Cyan
    Write-Host "An authorized run spends paid Sarvam credits and may send one Telegram alert."
    Write-Host "It can run at most once: a marker file blocks a second cycle."
    $confirm = Read-Host "Type RUN to authorize exactly one staging research cycle"
    if ($confirm -cne "RUN") {
        Write-Host "[ABORT] research NOT authorized (expected RUN, got '$confirm'). Nothing was connected, called or sent." -ForegroundColor Red
        exit 2
    }
}
else {
    Write-Host "=== AI Radar: staging orchestration (migrations + read-only verification) ===" -ForegroundColor Cyan
    Write-Host "Research and Telegram delivery are not authorized for this run."
}

$code = 1
try {
    $logFull = Join-Path $root $LogPath
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $logFull) | Out-Null
    Start-Transcript -Path $logFull -Force | Out-Null

    # Silent retrieval from Windows Credential Manager. The only thing that ever
    # asks for the DSN is scripts\staging_credentials.ps1 -Setup.
    Initialize-StagingCredential
    if (-not $env:AI_RADAR_STAGING_DATABASE_URL) { throw "no staging DSN in this process" }

    # --- configuration for this process only ---------------------------------
    $env:AI_RADAR_STAGING = "1"
    $env:AI_RADAR_LOG_LEVEL = "INFO"
    $env:PYTHONPATH = Join-Path $root "src"

    # Keys that must never be usable. Staging mode blanks these in Settings too;
    # this is the second, independent layer.
    $env:FREEAI_API_KEY = ""
    $env:GEMINI_API_KEY = ""
    $env:GROQ_API_KEY = ""
    $env:XAI_API_KEY = ""

    if ($AuthorizeResearch) {
        # Explicit opt-in, set only after the typed authorization above.
        $env:AI_RADAR_STAGING_ALLOW_TELEGRAM = "1"
        $env:MAX_ALERTS_PER_RUN = "1"
        $env:SARVAM_MAX_CALLS_PER_RUN = "1"
        $pyArgs = @("scripts\staging_orchestrator.py", "--authorize-research")
    }
    else {
        $env:AI_RADAR_STAGING_ALLOW_TELEGRAM = "0"
        $env:SARVAM_API_KEY = ""
        $env:TELEGRAM_BOT_TOKEN = ""
        $env:TELEGRAM_CHAT_ID = ""
        $pyArgs = @("scripts\staging_orchestrator.py")
    }

    Write-Host "`n[INFO] launching one Python process for every stage (no per-stage child processes)" -ForegroundColor DarkGray
    & $py @pyArgs
    $code = $LASTEXITCODE
    Write-Host "`n[INFO] orchestrator exit code: $code" -ForegroundColor DarkGray
}
catch {
    Write-Host "`n[ABORT] $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "No research was run, no provider was called, no message was sent." -ForegroundColor Red
    $code = 1
}
finally {
    # Clears the DSN and every staging-mode variable from this process, whatever
    # path was taken above. The child process has already exited.
    Clear-StagingCredential
    try { Stop-Transcript | Out-Null } catch { }
}

Write-Host "`nevidence: artifacts\staging_orchestration_output.txt" -ForegroundColor Cyan
Write-Host "          artifacts\staging_orchestration_report.json" -ForegroundColor Cyan
exit $code
