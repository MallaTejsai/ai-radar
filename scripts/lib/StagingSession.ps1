# AI Radar - shared staging credential handling (dot-source this file)
#
# One-time setup stores the staging DSN in Windows Credential Manager under the
# target name AI_RADAR_STAGING_DATABASE_URL. Every staging script then reads it
# from there silently; no script prompts for the DSN during normal operation.
#
# Storage backend: Windows Credential Manager (CRED_TYPE_GENERIC, persist=LOCAL_MACHINE)
# through advapi32 CredReadW/CredWriteW/CredDeleteW. No pip dependency and no
# PowerShell module is required. The blob is protected by DPAPI under the current
# user's logon, so another Windows user on the same PC cannot read it.
#
# Guarantees:
#   * the DSN never appears in a file, log, transcript, command line or report;
#   * it only ever exists in this process's environment
#     (AI_RADAR_STAGING_DATABASE_URL) and in managed memory;
#   * retrieval is silent and cached per process - a second call never re-reads
#     and never prompts;
#   * a missing, malformed or production-matching credential fails closed with
#     instructions, and never falls back to DATABASE_URL.
#
# Usage:
#   . .\scripts\lib\StagingSession.ps1
#   Initialize-StagingCredential          # silent retrieval, throws on failure
#   ...
#   Clear-StagingCredential              # always, in a finally block
#
# One-time setup / verification / removal: scripts\staging_credentials.ps1

$script:StagingCredentialTarget = "AI_RADAR_STAGING_DATABASE_URL"
$script:StagingCredentialTypeGeneric = 1
$script:StagingCredentialPersistLocalMachine = 2
$script:StagingCredentialErrorNotFound = 1168
$script:StagingSetupCommand = 'powershell -NoProfile -ExecutionPolicy Bypass -File scripts\staging_credentials.ps1 -Setup'
$script:StagingVerifyCommand = 'powershell -NoProfile -ExecutionPolicy Bypass -File scripts\staging_credentials.ps1 -Verify'

# --------------------------------------------------------------------------- C#
if (-not ("AI.Radar.NativeCredential" -as [type])) {
    Add-Type -Namespace AI.Radar -Name NativeCredential -MemberDefinition @'
[StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
public struct CREDENTIAL {
    public uint Flags;
    public uint Type;
    [MarshalAs(UnmanagedType.LPWStr)] public string TargetName;
    [MarshalAs(UnmanagedType.LPWStr)] public string Comment;
    public System.Runtime.InteropServices.ComTypes.FILETIME LastWritten;
    public uint CredentialBlobSize;
    public IntPtr CredentialBlob;
    public uint Persist;
    public uint AttributeCount;
    public IntPtr Attributes;
    [MarshalAs(UnmanagedType.LPWStr)] public string TargetAlias;
    [MarshalAs(UnmanagedType.LPWStr)] public string UserName;
}

[DllImport("advapi32.dll", EntryPoint = "CredReadW", CharSet = CharSet.Unicode, SetLastError = true)]
public static extern bool CredRead(string target, uint type, uint flags, out IntPtr credential);

[DllImport("advapi32.dll", EntryPoint = "CredWriteW", CharSet = CharSet.Unicode, SetLastError = true)]
public static extern bool CredWrite(ref CREDENTIAL credential, uint flags);

[DllImport("advapi32.dll", EntryPoint = "CredDeleteW", CharSet = CharSet.Unicode, SetLastError = true)]
public static extern bool CredDelete(string target, uint type, uint flags);

[DllImport("advapi32.dll", EntryPoint = "CredFree", SetLastError = false)]
public static extern void CredFree(IntPtr buffer);
'@
}

# ------------------------------------------------------------------ low level
function Get-StagingCredentialTargetName {
    [CmdletBinding()]
    param([string]$Target = $script:StagingCredentialTarget)
    return $Target
}

function Write-StagingCredentialBlob {
    <#
        Store one DSN in Windows Credential Manager. Never echoes the value.
        Returns $true on success; sets $script:StagingCredentialLastError.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$Target,
        [Parameter(Mandatory)][string]$Dsn
    )

    $script:StagingCredentialLastError = $null
    $bytes = [Text.Encoding]::Unicode.GetBytes($Dsn)
    $blob = [IntPtr]::Zero
    try {
        $blob = [Runtime.InteropServices.Marshal]::AllocHGlobal($bytes.Length)
        [Runtime.InteropServices.Marshal]::Copy($bytes, 0, $blob, $bytes.Length)

        $credential = [AI.Radar.NativeCredential+CREDENTIAL]::new()
        $credential.Flags = 0
        $credential.Type = [uint32]$script:StagingCredentialTypeGeneric
        $credential.TargetName = $Target
        $credential.Comment = "AI Radar staging database DSN (read-only setup, do not share)"
        $credential.CredentialBlobSize = [uint32]$bytes.Length
        $credential.CredentialBlob = $blob
        $credential.Persist = [uint32]$script:StagingCredentialPersistLocalMachine
        $credential.AttributeCount = 0
        $credential.Attributes = [IntPtr]::Zero
        $credential.TargetAlias = $null
        $credential.UserName = $env:USERNAME

        $ok = [AI.Radar.NativeCredential]::CredWrite([ref]$credential, 0)
        if (-not $ok) {
            $code = [Runtime.InteropServices.Marshal]::GetLastWin32Error()
            $script:StagingCredentialLastError = "CredWrite failed (Win32 $code)"
            return $false
        }
        return $true
    }
    finally {
        if ($blob -ne [IntPtr]::Zero) {
            # Zero the unmanaged copy before releasing it.
            for ($i = 0; $i -lt $bytes.Length; $i++) {
                [Runtime.InteropServices.Marshal]::WriteByte($blob, $i, 0)
            }
            [Runtime.InteropServices.Marshal]::FreeHGlobal($blob)
        }
        [Array]::Clear($bytes, 0, $bytes.Length)
    }
}

function Read-StagingCredentialBlob {
    <#
        Return the stored DSN as a SecureString, or $null when it is absent.
        The plaintext exists only inside this function and is zeroed on exit.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][string]$Target)

    $script:StagingCredentialLastError = $null
    $handle = [IntPtr]::Zero
    $plain = $null
    $bytes = $null
    try {
        $ok = [AI.Radar.NativeCredential]::CredRead(
            $Target, [uint32]$script:StagingCredentialTypeGeneric, 0, [ref]$handle)
        if (-not $ok) {
            $code = [Runtime.InteropServices.Marshal]::GetLastWin32Error()
            if ($code -ne $script:StagingCredentialErrorNotFound) {
                $script:StagingCredentialLastError = "CredRead failed (Win32 $code)"
            }
            return $null
        }

        $credential = [Runtime.InteropServices.Marshal]::PtrToStructure(
            $handle, [type]"AI.Radar.NativeCredential+CREDENTIAL")
        if ($credential.CredentialBlobSize -eq 0 -or $credential.CredentialBlob -eq [IntPtr]::Zero) {
            $script:StagingCredentialLastError = "stored credential is empty"
            return $null
        }

        $bytes = New-Object byte[] ([int]$credential.CredentialBlobSize)
        [Runtime.InteropServices.Marshal]::Copy($credential.CredentialBlob, $bytes, 0, $bytes.Length)
        $plain = [Text.Encoding]::Unicode.GetString($bytes)

        $secure = New-Object System.Security.SecureString
        foreach ($character in $plain.ToCharArray()) { $secure.AppendChar($character) }
        $secure.MakeReadOnly()
        return $secure
    }
    finally {
        if ($null -ne $bytes) { [Array]::Clear($bytes, 0, $bytes.Length) }
        $plain = $null
        if ($handle -ne [IntPtr]::Zero) { [AI.Radar.NativeCredential]::CredFree($handle) }
        [GC]::Collect() | Out-Null
    }
}

function Remove-StagingCredentialBlob {
    <# Delete only the named target. Returns PASS/FAIL strings, never the secret. #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][string]$Target)

    $script:StagingCredentialLastError = $null
    $ok = [AI.Radar.NativeCredential]::CredDelete(
        $Target, [uint32]$script:StagingCredentialTypeGeneric, 0)
    if ($ok) { return $true }
    $code = [Runtime.InteropServices.Marshal]::GetLastWin32Error()
    if ($code -eq $script:StagingCredentialErrorNotFound) { return $true }
    $script:StagingCredentialLastError = "CredDelete failed (Win32 $code)"
    return $false
}

function Test-WindowsCredentialManager {
    <#
        Backend health: a read that reports "not found" still proves the store is
        reachable. Only a real API/OS error is a FAIL. Never prints the secret.
    #>
    [CmdletBinding()]
    param([string]$ProbeTarget = "AI_RADAR_CREDENTIAL_BACKEND_PROBE")

    $script:StagingCredentialLastError = $null
    $handle = [IntPtr]::Zero
    try {
        $ok = [AI.Radar.NativeCredential]::CredRead(
            $ProbeTarget, [uint32]$script:StagingCredentialTypeGeneric, 0, [ref]$handle)
        $code = [Runtime.InteropServices.Marshal]::GetLastWin32Error()
        if ($ok) { return $true }
        if ($code -eq $script:StagingCredentialErrorNotFound) { return $true }
        $script:StagingCredentialLastError = "Windows Credential Manager unavailable (Win32 $code)"
        return $false
    }
    finally {
        if ($handle -ne [IntPtr]::Zero) { [AI.Radar.NativeCredential]::CredFree($handle) }
    }
}

# ----------------------------------------------------------------- validation
function ConvertFrom-SecureStringPlain {
    <# SecureString -> managed string. Callers must null the result immediately. #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][System.Security.SecureString]$Secure)

    $bstr = [IntPtr]::Zero
    try {
        $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Secure)
        return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
    }
    finally {
        if ($bstr -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
    }
}

function Test-StagingDsnFormat {
    <#
        Shape check only - no connection. Mirrors the load-bearing checks in
        ai_radar.storage.db.validate_dsn: scheme, host, password present, and no
        transaction-pooler port. Returns $true/$false and sets the reason.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][string]$Dsn)

    $script:StagingDsnFormatReason = $null
    $value = $Dsn.Trim()
    if ([string]::IsNullOrWhiteSpace($value)) {
        $script:StagingDsnFormatReason = "empty value"
        return $false
    }
    if ($value -match '<[^>]*>' -or $value -match 'YOUR[-_]' -or $value -match 'PROJECT-REF') {
        $script:StagingDsnFormatReason = "placeholder text was pasted instead of the DSN"
        return $false
    }
    if ($value -notmatch '^postgres(ql)?://') {
        $script:StagingDsnFormatReason = "must start with postgresql://"
        return $false
    }

    $uri = $null
    if (-not [Uri]::TryCreate($value, [UriKind]::Absolute, [ref]$uri)) {
        $script:StagingDsnFormatReason = "not a parseable URI (percent-encode reserved password characters)"
        return $false
    }
    if ([string]::IsNullOrWhiteSpace($uri.Host)) {
        $script:StagingDsnFormatReason = "host is missing"
        return $false
    }
    if ($uri.Port -eq 6543) {
        $script:StagingDsnFormatReason = "port 6543 is a transaction pooler; session mode (5432) is required"
        return $false
    }
    $userInfo = $uri.UserInfo
    if ([string]::IsNullOrWhiteSpace($userInfo) -or -not $userInfo.Contains(":")) {
        $script:StagingDsnFormatReason = "user and password are required in the URI"
        return $false
    }
    $password = $userInfo.Substring($userInfo.IndexOf(":") + 1)
    if ([string]::IsNullOrWhiteSpace($password)) {
        $script:StagingDsnFormatReason = "password is missing"
        return $false
    }
    return $true
}

function Get-EndpointSignature {
    <# (host, port, user) - the Supabase project identity. Never includes a password. #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][string]$Dsn)

    $uri = $null
    if (-not [Uri]::TryCreate($Dsn.Trim(), [UriKind]::Absolute, [ref]$uri)) { return $null }
    $user = $uri.UserInfo
    if ($user.Contains(":")) { $user = $user.Substring(0, $user.IndexOf(":")) }
    return [pscustomobject]@{
        Host = $uri.Host.ToLowerInvariant()
        Port = [string]$uri.Port
        User = $user
    }
}

function Get-EndpointSignatureString {
    [CmdletBinding()]
    param([Parameter(Mandatory)][string]$Dsn)

    $signature = Get-EndpointSignature -Dsn $Dsn
    if ($null -eq $signature) { return $null }
    return ("{0}:{1}/{2}" -f $signature.Host, $signature.Port, $signature.User)
}

function Get-ProductionDsn {
    <#
        The production DSN is used ONLY to prove the staging endpoint is a
        different server. It is parsed and never connected to, and it is never a
        fallback for the staging credential.
        Order: process environment, then the DATABASE_URL line in .env.
    #>
    [CmdletBinding()]
    param([string]$Root)

    if ($env:DATABASE_URL) { return $env:DATABASE_URL }
    if (-not $Root) { $Root = (Get-Location).Path }
    $envFile = Join-Path $Root ".env"
    if (-not (Test-Path $envFile)) { return "" }
    foreach ($line in (Get-Content $envFile -ErrorAction SilentlyContinue)) {
        if ($line -match '^\s*DATABASE_URL\s*=\s*(.+?)\s*$') {
            $value = $Matches[1].Trim()
            if ($value.Length -ge 2 -and (
                    ($value.StartsWith('"') -and $value.EndsWith('"')) -or
                    ($value.StartsWith("'") -and $value.EndsWith("'")))) {
                $value = $value.Substring(1, $value.Length - 2)
            }
            return $value
        }
    }
    return ""
}

function Get-DatabaseNameFromDsn {
    [CmdletBinding()]
    param([Parameter(Mandatory)][string]$Dsn)

    $uri = $null
    if (-not [Uri]::TryCreate($Dsn.Trim(), [UriKind]::Absolute, [ref]$uri)) { return "" }
    return ($uri.AbsolutePath.TrimStart('/').Split('?')[0])
}

function Test-StagingEndpointIdentity {
    <#
        Refuses a staging DSN that reaches the production server, and refuses one
        whose isolation cannot be proven. Same policy as
        ai_radar.storage.db.assert_staging_target.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$Dsn,
        [string]$ProductionDsn
    )

    $script:StagingEndpointIdentityReason = $null
    $staging = Get-EndpointSignatureString -Dsn $Dsn
    if (-not $staging) {
        $script:StagingEndpointIdentityReason = "staging endpoint could not be parsed"
        return $false
    }

    if (-not $ProductionDsn) { $ProductionDsn = Get-ProductionDsn }
    if ($ProductionDsn) {
        $production = Get-EndpointSignatureString -Dsn $ProductionDsn
        if ($production -and $staging -eq $production) {
            $script:StagingEndpointIdentityReason = "this is the production server; refusing to store it as staging"
            return $false
        }
        return $true
    }

    # No production DSN to compare against: require a throwaway-looking database
    # name, exactly as the Python guard does.
    $name = Get-DatabaseNameFromDsn -Dsn $Dsn
    foreach ($tag in @('test', 'ci', 'scratch', 'sandbox', 'stag')) {
        if ($name.ToLowerInvariant().Contains($tag)) { return $true }
    }
    $script:StagingEndpointIdentityReason = "no production DSN to compare against and the database name is not recognisably disposable"
    return $false
}

function Get-StagingDsnFingerprint {
    <# Short non-reversible fingerprint, safe to print. #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][string]$Dsn)

    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [Text.Encoding]::UTF8.GetBytes($Dsn)
        $hash = $sha.ComputeHash($bytes)
        [Array]::Clear($bytes, 0, $bytes.Length)
        return (($hash | ForEach-Object { $_.ToString("x2") }) -join "").Substring(0, 16)
    }
    finally { $sha.Dispose() }
}

# ------------------------------------------------------------------- lifecycle
function Initialize-StagingCredential {
    <#
        Retrieve the staging DSN from Windows Credential Manager and place it in
        this process's environment. Never prompts. Fails closed with instructions
        when the credential is absent, malformed or points at production.
    #>
    [CmdletBinding()]
    param(
        [string]$Target = $script:StagingCredentialTarget,
        [string]$ProductionDsn
    )

    if ($env:AI_RADAR_STAGING_DATABASE_URL) {
        Write-Host "[INFO] staging DSN already held by this process - reusing it, no store read" -ForegroundColor DarkGray
        return
    }

    if (-not (Test-WindowsCredentialManager)) {
        throw "Windows Credential Manager is unavailable. $($script:StagingCredentialLastError)"
    }

    $secure = Read-StagingCredentialBlob -Target $Target
    if ($null -eq $secure) {
        $detail = if ($script:StagingCredentialLastError) { " ($($script:StagingCredentialLastError))" } else { "" }
        throw ("no staging credential is stored under '$Target'$detail.`n" +
            "Store it once (hidden input, Windows Credential Manager):`n    $($script:StagingSetupCommand)")
    }

    $plain = $null
    try {
        $plain = ConvertFrom-SecureStringPlain -Secure $secure
        if (-not (Test-StagingDsnFormat -Dsn $plain)) {
            throw ("the stored staging credential is not a valid DSN: $($script:StagingDsnFormatReason).`n" +
                "Replace it once with:`n    $($script:StagingSetupCommand)")
        }
        if (-not (Test-StagingEndpointIdentity -Dsn $plain -ProductionDsn $ProductionDsn)) {
            throw ("the stored staging credential failed the isolation check: $($script:StagingEndpointIdentityReason).`n" +
                "Nothing will connect. Fix it with:`n    $($script:StagingSetupCommand)")
        }
        $env:AI_RADAR_STAGING_DATABASE_URL = $plain.Trim()
    }
    finally {
        $plain = $null
        if ($null -ne $secure) { $secure.Dispose() }
        [GC]::Collect() | Out-Null
    }

    Write-Host "[PASS] staging credential loaded from Windows Credential Manager (value never printed)" -ForegroundColor Green
}

function Save-StagingCredential {
    <#
        Validate then store. Validation is not bypassable by callers: an invalid or
        production-matching DSN is refused before anything is written.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$Dsn,
        [string]$Target = $script:StagingCredentialTarget,
        [string]$ProductionDsn
    )

    if (-not (Test-WindowsCredentialManager)) {
        throw "Windows Credential Manager is unavailable. $($script:StagingCredentialLastError)"
    }
    if (-not (Test-StagingDsnFormat -Dsn $Dsn)) {
        throw "DSN format validation failed: $($script:StagingDsnFormatReason)"
    }
    if (-not (Test-StagingEndpointIdentity -Dsn $Dsn -ProductionDsn $ProductionDsn)) {
        throw "staging endpoint identity failed: $($script:StagingEndpointIdentityReason)"
    }
    if (-not (Write-StagingCredentialBlob -Target $Target -Dsn $Dsn.Trim())) {
        throw "could not store the credential: $($script:StagingCredentialLastError)"
    }

    # Read back and compare hashes so a silent write failure cannot masquerade as
    # success. The comparison never prints either value.
    $verifySecure = Read-StagingCredentialBlob -Target $Target
    if ($null -eq $verifySecure) {
        throw "the credential could not be read back after saving"
    }
    $verifyPlain = $null
    try {
        $verifyPlain = ConvertFrom-SecureStringPlain -Secure $verifySecure
        $expected = Get-StagingDsnFingerprint -Dsn $Dsn.Trim()
        $actual = Get-StagingDsnFingerprint -Dsn $verifyPlain
        if ($expected -ne $actual) { throw "the credential read back does not match what was saved" }
        return [pscustomobject]@{
            Target = $Target
            Endpoint = Get-MaskedEndpoint -Dsn $verifyPlain
            Fingerprint = $actual
        }
    }
    finally {
        $verifyPlain = $null
        $verifySecure.Dispose()
        [GC]::Collect() | Out-Null
    }
}

function Get-MaskedEndpoint {
    <# Safe to print and to paste into a report: the password is removed. #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][string]$Dsn)

    $uri = $null
    if (-not [Uri]::TryCreate($Dsn.Trim(), [UriKind]::Absolute, [ref]$uri)) {
        return "(unparseable DSN)"
    }
    $user = $uri.UserInfo
    if ($user.Contains(":")) { $user = $user.Substring(0, $user.IndexOf(":")) }
    $database = $uri.AbsolutePath.TrimStart('/').Split('?')[0]
    return ("{0}@{1}:{2}/{3}" -f $user, $uri.Host, $uri.Port, $database)
}

function Remove-StagingCredential {
    [CmdletBinding()]
    param([string]$Target = $script:StagingCredentialTarget)

    if (-not (Test-WindowsCredentialManager)) {
        throw "Windows Credential Manager is unavailable. $($script:StagingCredentialLastError)"
    }
    if (-not (Remove-StagingCredentialBlob -Target $Target)) {
        throw "could not delete the credential: $($script:StagingCredentialLastError)"
    }
    if ($null -ne (Read-StagingCredentialBlob -Target $Target)) {
        throw "the credential still exists after deletion"
    }
    return $true
}

function Clear-StagingCredential {
    # Idempotent: safe from any finally block, including paths where the prompt,
    # the store read or the validation never completed.
    [CmdletBinding()]
    param()

    foreach ($name in @(
            'AI_RADAR_STAGING_DATABASE_URL',
            'AI_RADAR_STAGING',
            'AI_RADAR_STAGING_ALLOW_TELEGRAM',
            'FREEAI_API_KEY',
            'GEMINI_API_KEY',
            'GROQ_API_KEY',
            'XAI_API_KEY',
            'SARVAM_API_KEY',
            'TELEGRAM_BOT_TOKEN',
            'TELEGRAM_CHAT_ID',
            'MAX_ALERTS_PER_RUN',
            'SARVAM_MAX_CALLS_PER_RUN'
        )) {
        [Environment]::SetEnvironmentVariable($name, $null)
    }
    [GC]::Collect() | Out-Null
    Write-Host "[INFO] staging credential and staging-mode variables cleared from this process" -ForegroundColor DarkGray
}
