# AI Radar - offline self-test for the staging credential helper
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\test_staging_credentials.ps1
#
# Uses FAKE DSNs and a dedicated test target name, so the real
# AI_RADAR_STAGING_DATABASE_URL entry is never read, written or deleted.
# No database is contacted: these tests exercise the store, the validators and
# the fail-closed paths only.

$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)
$root = (Get-Location).Path

. (Join-Path $PSScriptRoot "lib\StagingSession.ps1")

$testTarget = "AI_RADAR_STAGING_DATABASE_URL__SELFTEST"
$invalidTarget = "AI_RADAR_STAGING_DATABASE_URL__SELFTEST_BAD"

# Clearly fake values, assembled at run time so no complete fake secret is ever
# stored as a literal in this file. Never a real endpoint credential.
$fakeSecret = "fake" + "-password-" + "123"
$fakeProdSecret = "fake" + "-prod-" + "password"
$fakeStaging = "postgresql://postgres.fakestagingref:" + $fakeSecret + "@aws-1-ap-southeast-2.pooler.supabase.com:5432/postgres?sslmode=require"
$fakeProduction = "postgresql://postgres.fakeprodref:" + $fakeProdSecret + "@aws-1-ap-northeast-1.pooler.supabase.com:5432/postgres"
$fakeProductionEndpoint = "postgresql://postgres.fakeprodref:" + $fakeProdSecret + "@aws-1-ap-northeast-1.pooler.supabase.com:5432/postgres?sslmode=require"
$fakeTransactionPooler = "postgresql://postgres.fakestagingref:" + $fakeSecret + "@aws-1-ap-southeast-2.pooler.supabase.com:6543/postgres"
$fakeMissingPassword = "postgresql://postgres.fakestagingref@aws-1-ap-southeast-2.pooler.supabase.com:5432/postgres"
$fakePlaceholder = "postgresql://postgres.fakestagingref:[YOUR-PASSWORD]@aws-1-ap-southeast-2.pooler.supabase.com:5432/postgres"

$script:results = @()
function Record([string]$name, [bool]$ok, [string]$detail = "") {
    $script:results += [pscustomobject]@{ Name = $name; Ok = $ok; Detail = $detail }
    $mark = if ($ok) { "PASS" } else { "FAIL" }
    Write-Host ("[{0}] {1}{2}" -f $mark, $name, $(if ($detail) { ": $detail" } else { "" }))
}

function Invoke-ExpectFailure([string]$name, [scriptblock]$action, [string]$matchText = "") {
    try {
        & $action | Out-Null
        Record $name $false "no error was raised"
    }
    catch {
        $ok = $true
        $detail = $_.Exception.Message
        if ($matchText -and $detail -notmatch [regex]::Escape($matchText)) {
            $ok = $false
            $detail = "unexpected error: $detail"
        }
        Record $name $ok $detail
    }
}

try {
    Write-Host "=== staging credential helper self-test (fake DSNs, no database) ===" -ForegroundColor Cyan

    # 0. backend ------------------------------------------------------------
    Record "Windows Credential Manager reachable" (Test-WindowsCredentialManager) "advapi32 CredRead/CredWrite"

    # 1. first-time setup ---------------------------------------------------
    $saved = Save-StagingCredential -Dsn $fakeStaging -Target $testTarget -ProductionDsn $fakeProduction
    Record "first-time setup stores the credential" ($saved.Fingerprint -eq (Get-StagingDsnFingerprint -Dsn $fakeStaging)) `
        "target $testTarget, endpoint $($saved.Endpoint)"

    # 2. successful retrieval ----------------------------------------------
    $secure = Read-StagingCredentialBlob -Target $testTarget
    $retrieved = if ($null -ne $secure) { ConvertFrom-SecureStringPlain -Secure $secure } else { $null }
    if ($null -ne $secure) { $secure.Dispose() }
    Record "retrieval returns the stored value" ($null -ne $retrieved -and (Get-StagingDsnFingerprint -Dsn $retrieved) -eq $saved.Fingerprint)
    $retrieved = $null

    # 3. repeated retrieval without prompts --------------------------------
    [Environment]::SetEnvironmentVariable("AI_RADAR_STAGING_DATABASE_URL", $null)
    Initialize-StagingCredential -Target $testTarget -ProductionDsn $fakeProduction
    $first = $env:AI_RADAR_STAGING_DATABASE_URL
    Initialize-StagingCredential -Target $testTarget -ProductionDsn $fakeProduction
    $second = $env:AI_RADAR_STAGING_DATABASE_URL
    Record "repeated retrieval is silent and cached" ($first -eq $second -and (Get-StagingDsnFingerprint -Dsn $second) -eq $saved.Fingerprint)

    $libText = Get-Content (Join-Path $PSScriptRoot "lib\StagingSession.ps1") -Raw
    $promptCount = ([regex]::Matches($libText, 'Read-Host')).Count
    Record "shared helper contains no DSN prompt" ($promptCount -eq 0) "Read-Host occurrences in helper: $promptCount"
    $first = $null; $second = $null

    # 4. missing credential -------------------------------------------------
    [Environment]::SetEnvironmentVariable("AI_RADAR_STAGING_DATABASE_URL", $null)
    Invoke-ExpectFailure "missing credential fails closed with setup instructions" {
        Initialize-StagingCredential -Target "AI_RADAR_STAGING_DATABASE_URL__ABSENT" -ProductionDsn $fakeProduction
    } "staging_credentials.ps1 -Setup"

    # 5. invalid credential -------------------------------------------------
    if (-not (Write-StagingCredentialBlob -Target $invalidTarget -Dsn "not-a-dsn")) {
        Record "invalid credential can be planted for the test" $false $script:StagingCredentialLastError
    }
    [Environment]::SetEnvironmentVariable("AI_RADAR_STAGING_DATABASE_URL", $null)
    Invoke-ExpectFailure "invalid stored credential fails closed" {
        Initialize-StagingCredential -Target $invalidTarget -ProductionDsn $fakeProduction
    } "not a valid DSN"

    # 6. format validation cases -------------------------------------------
    Record "transaction pooler port is rejected" (-not (Test-StagingDsnFormat -Dsn $fakeTransactionPooler))
    Record "missing password is rejected" (-not (Test-StagingDsnFormat -Dsn $fakeMissingPassword))
    Record "placeholder is rejected" (-not (Test-StagingDsnFormat -Dsn $fakePlaceholder))

    # 7. wrong endpoint / production endpoint -------------------------------
    Record "production endpoint fails the identity check" (-not (Test-StagingEndpointIdentity -Dsn $fakeProductionEndpoint -ProductionDsn $fakeProduction))
    Invoke-ExpectFailure "setup refuses to store the production endpoint" {
        Save-StagingCredential -Dsn $fakeProductionEndpoint -Target $testTarget -ProductionDsn $fakeProduction
    } "production"
    Record "staging endpoint passes the identity check" (Test-StagingEndpointIdentity -Dsn $fakeStaging -ProductionDsn $fakeProduction)

    # 8. removal ------------------------------------------------------------
    Remove-StagingCredential -Target $testTarget | Out-Null
    $afterRemove = Read-StagingCredentialBlob -Target $testTarget
    Record "removal deletes only the test credential" ($null -eq $afterRemove)
    Remove-StagingCredential -Target $testTarget | Out-Null
    Record "removal is idempotent" $true

    # 9. no plaintext secret on disk ---------------------------------------
    $hits = @(Get-ChildItem -Path $root -Recurse -File -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -notmatch '\\\.git\\' -and $_.Extension -notin @('.pyc') } |
        Select-String -SimpleMatch $fakeSecret -ErrorAction SilentlyContinue)
    Record "no plaintext fake DSN in files or logs" ($hits.Count -eq 0) "matches: $($hits.Count)"
    $fakeSecret = $null; $fakeProdSecret = $null
}
finally {
    [Environment]::SetEnvironmentVariable("AI_RADAR_STAGING_DATABASE_URL", $null)
    try { Remove-StagingCredentialBlob -Target $testTarget | Out-Null } catch { }
    try { Remove-StagingCredentialBlob -Target $invalidTarget | Out-Null } catch { }
    Clear-StagingCredential | Out-Null
}

$failed = @($script:results | Where-Object { -not $_.Ok })
Write-Host ""
Write-Host ("{0} passed, {1} failed" -f ($script:results.Count - $failed.Count), $failed.Count)
if ($failed.Count -gt 0) {
    $failed | ForEach-Object { Write-Host "  FAILED: $($_.Name)" -ForegroundColor Red }
    exit 1
}
exit 0
