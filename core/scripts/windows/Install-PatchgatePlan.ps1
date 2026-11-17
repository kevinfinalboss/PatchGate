#Requires -Version 5.1
<#
.SYNOPSIS
    Patchgate install-plan executor for Windows.

.DESCRIPTION
    Runs inside the VM as SYSTEM/Administrator. Applies an install plan produced by
    `python -m patchgate compile`: agents (choco, msi, exe, script), Chocolatey packages and
    their validation commands. It never calls a cloud API.

    Idempotent: steps whose validation already passes are skipped, and a plan whose digest
    was already applied successfully is only re-validated.

    Exit codes: 0 success, 1 failure, 2 invalid input, 3010 reboot required (re-run after reboot).
#>
[CmdletBinding(DefaultParameterSetName = 'Base64')]
param(
    [Parameter(Mandatory, ParameterSetName = 'Base64')]
    [string]$PlanBase64,

    [Parameter(Mandatory, ParameterSetName = 'File')]
    [string]$PlanFile,

    [switch]$Force
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$Root = Join-Path $env:ProgramData 'Patchgate'
$LogFile = Join-Path $Root 'logs\install.log'
$StateFile = Join-Path $Root 'state.json'

# Chocolatey is bootstrapped from its pinned package, never from a remote install script.
$ChocoVersion = '2.7.5'
$ChocoSha256 = 'f86f99a18d236c2f99e6f67a20be7adc65c6c7756bf6cf3b43759e7ec0aa5839'
$ChocoUrl = "https://packages.chocolatey.org/chocolatey.$ChocoVersion.nupkg"

# Child scripts run with the same PowerShell executable as this installer (5.1 or 7).
$PowerShellExe = (Get-Process -Id $PID).Path
$RebootExitCode = 3010
$SuccessCodes = @(0, 1641, 3010)
$ValidateAttempts = 5
$ValidateDelaySeconds = 6

function Write-PatchgateLog {
    # Write-Host on purpose: Write-Output would leak log lines into function return values.
    # SSM Run Command captures the host stream as command output.
    [Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSAvoidUsingWriteHost', '', Justification = 'Logs must not pollute the pipeline')]
    param([string]$Message, [string]$Level = 'INFO')
    $line = '{0} {1} {2}' -f (Get-Date -Format 'yyyy-MM-ddTHH:mm:ssK'), $Level, $Message
    Write-Host $line
    # Share read/write: log shippers (e.g. the CloudWatch agent) keep this file open.
    # A logging failure must never abort an installation.
    try {
        $stream = [IO.File]::Open($LogFile, [IO.FileMode]::Append, [IO.FileAccess]::Write,
            ([IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete))
        try {
            $writer = New-Object IO.StreamWriter($stream, (New-Object Text.UTF8Encoding $false))
            $writer.WriteLine($line)
            $writer.Flush()
        }
        finally {
            $stream.Dispose()
        }
    }
    catch {
        Write-Host "WARN could not write $LogFile ($($_.Exception.Message))"
    }
}

function Save-State {
    param([hashtable]$State)
    $State['updatedAt'] = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
    $State | ConvertTo-Json -Depth 6 | Set-Content -Path $StateFile -Encoding UTF8
}

function Invoke-Native {
    # Windows PowerShell 5.1 turns native stderr into terminating errors under 'Stop'.
    param([string]$FilePath, [string[]]$Arguments)
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $output = & $FilePath @Arguments 2>&1
        $code = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previous
    }
    foreach ($line in $output) { Write-PatchgateLog "  $line" }
    return $code
}

function Invoke-PlanScript {
    # Runs catalog script content in a child process. Returns its exit code.
    param([string]$Script)
    $path = Join-Path $env:TEMP ("patchgate-{0}.ps1" -f [guid]::NewGuid())
    Set-Content -Path $path -Value $Script -Encoding UTF8
    try {
        return Invoke-Native -FilePath $PowerShellExe -Arguments @(
            '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', $path)
    }
    finally {
        Remove-Item -Path $path -Force -ErrorAction SilentlyContinue
    }
}

function Get-VerifiedDownload {
    param([string]$Url, [string]$Sha256, [string]$Destination)
    if (-not $Url.StartsWith('https://')) { throw "refusing non-https source: $Url" }
    Write-PatchgateLog "downloading $Url"
    Invoke-WebRequest -Uri $Url -OutFile $Destination -UseBasicParsing
    $actual = (Get-FileHash -Algorithm SHA256 -Path $Destination).Hash.ToLowerInvariant()
    if ($actual -ne $Sha256.ToLowerInvariant()) {
        Remove-Item -Path $Destination -Force
        throw "checksum mismatch for ${Url}: expected $Sha256, got $actual"
    }
    Write-PatchgateLog "checksum OK ($actual)"
    return $Destination
}

function Get-Chocolatey {
    $choco = Join-Path $env:ProgramData 'chocolatey\bin\choco.exe'
    if (Test-Path $choco) { return $choco }
    Write-PatchgateLog "installing Chocolatey $ChocoVersion from pinned package"
    $work = Join-Path $env:TEMP 'patchgate-chocolatey'
    New-Item -ItemType Directory -Force -Path $work | Out-Null
    $package = Get-VerifiedDownload -Url $ChocoUrl -Sha256 $ChocoSha256 -Destination (Join-Path $work 'chocolatey.zip')
    Expand-Archive -Path $package -DestinationPath (Join-Path $work 'pkg') -Force
    & (Join-Path $work 'pkg\tools\chocolateyInstall.ps1') | Out-Null
    if (-not (Test-Path $choco)) { throw 'Chocolatey installation failed' }
    [void](Invoke-Native -FilePath $choco -Arguments @('feature', 'enable', '-n', 'allowGlobalConfirmation'))
    return $choco
}

function Install-ChocoPackage {
    param([string]$Name, [string]$Version)
    $choco = Get-Chocolatey
    $arguments = @('upgrade', $Name, '-y', '--no-progress', '--limit-output')
    if ($Version) { $arguments += @('--version', $Version) }
    $code = Invoke-Native -FilePath $choco -Arguments $arguments
    if ($SuccessCodes -notcontains $code) { throw "choco failed for $Name (exit $code)" }
    return $code
}

function Test-Step {
    param($Step)
    if (-not ($Step.PSObject.Properties.Name -contains 'validate')) { return $false }
    return (Invoke-PlanScript -Script $Step.validate) -eq 0
}

function Assert-Step {
    param($Step)
    if (-not ($Step.PSObject.Properties.Name -contains 'validate')) { return }
    for ($attempt = 1; $attempt -le $ValidateAttempts; $attempt++) {
        if ((Invoke-PlanScript -Script $Step.validate) -eq 0) {
            Write-PatchgateLog "validate OK: $($Step.agent)"
            return
        }
        Write-PatchgateLog "validate attempt $attempt/$ValidateAttempts failed for $($Step.agent)"
        Start-Sleep -Seconds $ValidateDelaySeconds
    }
    throw "validation failed for $($Step.agent)"
}

function Install-Step {
    # Returns $true when the step requested a reboot.
    param($Step, [string]$WorkDir)
    $props = $Step.PSObject.Properties.Name
    $code = 0
    switch ($Step.method) {
        'choco' {
            $version = if ($props -contains 'version') { $Step.version } else { $null }
            $code = Install-ChocoPackage -Name $Step.package -Version $version
        }
        { $_ -in 'msi', 'exe' } {
            $file = Get-VerifiedDownload -Url $Step.source -Sha256 $Step.sha256 `
                -Destination (Join-Path $WorkDir ("{0}.{1}" -f $Step.agent, $Step.method))
            $extra = if ($props -contains 'args') { $Step.args } else { '' }
            if ($Step.method -eq 'msi') {
                $process = Start-Process -FilePath 'msiexec.exe' -Wait -PassThru `
                    -ArgumentList ('/i "{0}" /qn /norestart {1}' -f $file, $extra)
            }
            else {
                $process = Start-Process -FilePath $file -ArgumentList $extra -Wait -PassThru
            }
            $code = $process.ExitCode
            if ($SuccessCodes -notcontains $code) { throw "$($Step.agent) installer exited with $code" }
        }
        'script' {
            $code = Invoke-PlanScript -Script $Step.script
            if ($code -ne 0 -and $code -ne $RebootExitCode) { throw "$($Step.agent) script exited with $code" }
        }
        default { throw "unsupported method on windows: $($Step.method)" }
    }
    if ($code -eq $RebootExitCode) { return $true }
    if ($props -contains 'postInstall') {
        $post = Invoke-PlanScript -Script $Step.postInstall
        if ($post -eq $RebootExitCode) { return $true }
        if ($post -ne 0) { throw "$($Step.agent) postInstall exited with $post" }
    }
    return $false
}

# ------------------------------------------------------------------ main
New-Item -ItemType Directory -Force -Path (Split-Path $LogFile) | Out-Null

try {
    $json = if ($PSCmdlet.ParameterSetName -eq 'Base64') {
        # "gz1:" + base64(gzip(json)) is what Patchgate sends; plain base64 JSON is also accepted.
        if ($PlanBase64.StartsWith('gz1:')) {
            $bytes = [Convert]::FromBase64String($PlanBase64.Substring(4))
            $gzip = New-Object IO.Compression.GZipStream((New-Object IO.MemoryStream(, $bytes)), [IO.Compression.CompressionMode]::Decompress)
            $reader = New-Object IO.StreamReader($gzip, [Text.Encoding]::UTF8)
            try { $reader.ReadToEnd() } finally { $reader.Dispose() }
        }
        else {
            [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($PlanBase64))
        }
    }
    else {
        Get-Content -Raw -Path $PlanFile
    }
    $plan = $json | ConvertFrom-Json
    if ($plan.kind -ne 'InstallPlan' -or $plan.platform -ne 'windows') { throw 'not a windows InstallPlan' }
}
catch {
    Write-PatchgateLog "invalid plan: $_" 'ERROR'
    exit 2
}

if (-not $Force -and (Test-Path $StateFile)) {
    $state = Get-Content -Raw -Path $StateFile | ConvertFrom-Json
    if ($state.digest -eq $plan.digest -and $state.status -eq 'success') {
        Write-PatchgateLog "plan $($plan.digest) already applied; validating only"
        try {
            foreach ($step in $plan.steps) { Assert-Step -Step $step }
            exit 0
        }
        catch {
            Write-PatchgateLog "validation failed ($_); re-applying plan" 'WARN'
        }
    }
}

$work = Join-Path $env:TEMP ("patchgate-{0}" -f [guid]::NewGuid())
New-Item -ItemType Directory -Force -Path $work | Out-Null
$results = @()
try {
    foreach ($step in $plan.steps) {
        Write-PatchgateLog "== $($step.agent) ($($step.method))"
        if (Test-Step -Step $step) {
            Write-PatchgateLog "already installed and valid; skipping"
            $results += @{ agent = $step.agent; status = 'skipped' }
            continue
        }
        if (Install-Step -Step $step -WorkDir $work) {
            Write-PatchgateLog "$($step.agent) requested a reboot; re-run after restart" 'WARN'
            Save-State @{ digest = $plan.digest; status = 'reboot-pending'; agent = $step.agent }
            exit $RebootExitCode
        }
        Assert-Step -Step $step
        $results += @{ agent = $step.agent; status = 'ok' }
    }

    $rebootNeeded = $false
    foreach ($package in $plan.chocolatey) {
        $version = if ($package.PSObject.Properties.Name -contains 'version') { $package.version } else { $null }
        Write-PatchgateLog "== chocolatey $($package.name)"
        if ((Install-ChocoPackage -Name $package.name -Version $version) -eq $RebootExitCode) { $rebootNeeded = $true }
        $results += @{ agent = "choco:$($package.name)"; status = 'ok' }
    }

    if ($plan.hardening -ne 'none') { Write-PatchgateLog "hardening level $($plan.hardening) not implemented yet; skipped" 'WARN' }
}
catch {
    Write-PatchgateLog "FAILED: $_" 'ERROR'
    Save-State @{ digest = $plan.digest; status = 'failed'; error = "$_" }
    exit 1
}
finally {
    Remove-Item -Path $work -Recurse -Force -ErrorAction SilentlyContinue
}

Save-State @{ digest = $plan.digest; status = 'success'; result = @{ profile = $plan.profile; steps = $results } }
Write-PatchgateLog "SUCCESS: profile $($plan.profile) applied"
Write-Output (@{ patchgate = @{ profile = $plan.profile; digest = $plan.digest; steps = $results } } | ConvertTo-Json -Depth 5 -Compress)
if ($rebootNeeded) { exit $RebootExitCode }
exit 0
