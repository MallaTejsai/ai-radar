# AI Radar - staging credential management (Windows Credential Manager)
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\staging_credentials.ps1 -Setup
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\staging_credentials.ps1 -Verify
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\staging_credentials.ps1 -Remove
#
# -Setup   asks for the staging DSN ONCE with hidden input, validates the shape and
#          the endpoint identity, stores it in Windows Credential Manager, then
#          reads it back and compares fingerprints. This is the only place the DSN
#          is ever typed.
# -Verify  prints exactly four PASS/FAIL lines. It never reveals the secret.
# -Remove  deletes only the AI Radar staging credential.
#
# No connection is opened to any database by any of these commands. Production is
# parsed only, as the reference signature the staging endpoint must differ from.

[CmdletBinding()]
param(
    [switch]$Setup,
    [switch]$Verify,
    [switch]$Remove
)

$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)
$root = (Get-Location).Path

. (Join-Path $PSScriptRoot "lib\StagingSession.ps1")

$target = Get-StagingCredentialTargetName
$selected = @(@($Setup, $Verify, $Remove) | Where-Object { $_ })
if ($selected.Count -ne 1) {
    Write-Host "Specify exactly one of -Setup, -Verify or -Remove." -ForegroundColor Red
    Write-Host "  $($script:StagingSetupCommand)"
    Write-Host "  $($script:StagingVerifyCommand)"
    Write-Host '  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\staging_credentials.ps1 -Remove'
    exit 2
}

function Read-StagingDsnHidden {
    <# One hidden prompt. The value is returned as a SecureString and never echoed. #>
    $secure = Read-Host -AsSecureString "Paste the STAGING DSN (input is hidden; it is never echoed, logged or saved to a file)"
    if ($null -eq $secure -or $secure.Length -eq 0) {
        throw "no staging DSN was entered"
    }
    return $secure
}

if ($Setup) {
    Write-Host "=== AI Radar: store the staging DSN in Windows Credential Manager ===" -ForegroundColor Cyan
    Write-Host "Storage: Windows Credential Manager, target '$target', current Windows user only."
    Write-Host "Nothing is written to .env, JSON, YAML, Git or any report. No database is contacted."

    $existing = Read-StagingCredentialBlob -Target $target
    if ($null -ne $existing) {
        $existing.Dispose()
        Write-Host "[INFO] a credential already exists for this target and will be replaced." -ForegroundColor DarkGray
    }

    $secure = $null
    $plain = $null
    try {
        $secure = Read-StagingDsnHidden
        $plain = ConvertFrom-SecureStringPlain -Secure $secure

        if (-not (Test-StagingDsnFormat -Dsn $plain)) {
            throw "DSN format validation failed: $($script:StagingDsnFormatReason)"
        }
        if (-not (Test-StagingEndpointIdentity -Dsn $plain)) {
            throw "staging endpoint identity failed: $($script:StagingEndpointIdentityReason)"
        }

        $saved = Save-StagingCredential -Dsn $plain
        Write-Host ""
        Write-Host "[PASS] stored and read back successfully in Windows Credential Manager" -ForegroundColor Green
        Write-Host "       endpoint    : $($saved.Endpoint)   (password never shown)"
        Write-Host "       fingerprint : $($saved.Fingerprint)"
        Write-Host "       target name : $($saved.Target)"
        Write-Host ""
        Write-Host "Staging scripts now load this automatically; you will not be asked again."
        Write-Host "Check any time with: $($script:StagingVerifyCommand)"
        exit 0
    }
    catch {
        Write-Host ""
        Write-Host "[FAIL] $($_.Exception.Message)" -ForegroundColor Red
        Write-Host "Nothing was changed." -ForegroundColor Red
        exit 1
    }
    finally {
        $plain = $null
        if ($null -ne $secure) { $secure.Dispose() }
        [GC]::Collect() | Out-Null
    }
}

if ($Verify) {
    $backendOk = Test-WindowsCredentialManager

    $exists = $false
    $formatOk = $false
    $identityOk = $false
    $secure = $null
    $plain = $null
    try {
        if ($backendOk) {
            $secure = Read-StagingCredentialBlob -Target $target
            if ($null -ne $secure) {
                $exists = $true
                $plain = ConvertFrom-SecureStringPlain -Secure $secure
                $formatOk = Test-StagingDsnFormat -Dsn $plain
                if ($formatOk) {
                    $identityOk = Test-StagingEndpointIdentity -Dsn $plain
                }
            }
        }
    }
    finally {
        $plain = $null
        if ($null -ne $secure) { $secure.Dispose() }
        [GC]::Collect() | Out-Null
    }

    function Format-Result([bool]$ok) { if ($ok) { "PASS" } else { "FAIL" } }

    Write-Host "Credential exists: $(if ($exists) { 'YES' } else { 'NO' })"
    Write-Host "Windows Credential Manager backend: $(Format-Result $backendOk)"
    Write-Host "DSN format validation: $(Format-Result $formatOk)"
    Write-Host "Staging endpoint identity: $(Format-Result $identityOk)"

    if ($exists -and $backendOk -and $formatOk -and $identityOk) { exit 0 }
    exit 1
}

if ($Remove) {
    Write-Host "=== AI Radar: remove the stored staging credential ===" -ForegroundColor Cyan
    try {
        $present = Read-StagingCredentialBlob -Target $target
        if ($null -ne $present) { $present.Dispose() } else { $present = $null }
        Remove-StagingCredential -Target $target | Out-Null
        if ($null -ne $present) {
            Write-Host "[PASS] the AI Radar staging credential was deleted; nothing else was touched" -ForegroundColor Green
            Write-Host "Staging scripts will now refuse to run until you re-run the setup command."
        }
        else {
            Write-Host "[PASS] no AI Radar staging credential was stored; nothing to delete" -ForegroundColor Green
        }
        exit 0
    }
    catch {
        Write-Host "[FAIL] $($_.Exception.Message)" -ForegroundColor Red
        exit 1
    }
}
