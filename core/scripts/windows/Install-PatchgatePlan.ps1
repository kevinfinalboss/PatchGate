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

    -Drift re-checks the last applied plan (agents and hardening controls) when its interval is
    due, correcting or reporting drift and leaving drift-event.json for the provider to publish.

    Exit codes: 0 success, 1 failure, 2 invalid input, 3010 reboot required (re-run after reboot).
#>
[CmdletBinding(DefaultParameterSetName = 'Base64')]
param(
    [Parameter(Mandatory, ParameterSetName = 'Base64')]
    [string]$PlanBase64,

    [Parameter(Mandatory, ParameterSetName = 'File')]
    [string]$PlanFile,

    [Parameter(Mandatory, ParameterSetName = 'Drift')]
    [switch]$Drift,

    [switch]$Force
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$Root = Join-Path $env:ProgramData 'Patchgate'
$LogFile = Join-Path $Root 'logs\install.log'
$StateFile = Join-Path $Root 'state.json'
$AppliedPlanFile = Join-Path $Root 'plan.json'          # last applied plan, re-used by drift runs
$ComplianceFile = Join-Path $Root 'compliance.json'     # evidence: last result of every control
$DriftEventFile = Join-Path $Root 'drift-event.json'    # picked up and published by the provider
$DriftIntervals = @{ '30m' = 1800; '1h' = 3600; '2h' = 7200; '6h' = 21600; '12h' = 43200; '24h' = 86400 }
$DriftSlackSeconds = 120
$script:HardeningRebootRequired = $false
$script:AppsRebootRequired = $false

# Chocolatey is bootstrapped from its pinned package, never from a remote install script.
$ChocoVersion = '2.7.5'
$ChocoSha256 = 'f86f99a18d236c2f99e6f67a20be7adc65c6c7756bf6cf3b43759e7ec0aa5839'
$ChocoUrl = "https://packages.chocolatey.org/chocolatey.$ChocoVersion.nupkg"

# Child scripts run with the same PowerShell executable as this installer (5.1 or 7).
$PowerShellExe = (Get-Process -Id $PID).Path
$RebootExitCode = 3010
$InstallerBusyExitCode = 1618
$InstallerBusyDelaySeconds = if ($env:PATCHGATE_INSTALLER_RETRY_SECONDS) { [int]$env:PATCHGATE_INSTALLER_RETRY_SECONDS } else { 30 }
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

function Write-JsonFile {
    # UTF-8 without BOM: consumers such as the AWS CLI and JSON parsers reject a BOM.
    param([string]$Path, $Data)
    $json = $Data | ConvertTo-Json -Depth 8
    $tmp = "$Path.tmp"
    [IO.File]::WriteAllText($tmp, $json, (New-Object Text.UTF8Encoding $false))
    Move-Item -Path $tmp -Destination $Path -Force
}

function Get-Prop {
    # StrictMode-safe property access with a default (older plans may lack newer fields).
    param($Object, [string]$Name, $Default = $null)
    if ($null -ne $Object -and $Object.PSObject.Properties.Name -contains $Name) { return $Object.$Name }
    return $Default
}

function Read-State {
    $state = @{}
    if (Test-Path $StateFile) {
        $raw = Get-Content -Raw -Path $StateFile | ConvertFrom-Json
        foreach ($p in $raw.PSObject.Properties) { $state[$p.Name] = $p.Value }
    }
    return $state
}

function Save-State {
    # Merge into the existing state so fields such as lastDriftAt survive.
    param([hashtable]$Fields)
    $state = Read-State
    foreach ($key in $Fields.Keys) {
        if ($null -eq $Fields[$key]) { $state.Remove($key) } else { $state[$key] = $Fields[$key] }
    }
    $state['updatedAt'] = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
    Write-JsonFile -Path $StateFile -Data $state
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
    # PATCHGATE_CHOCO_EXE lets tests point at a fake Chocolatey; it is never set in production.
    if ($env:PATCHGATE_CHOCO_EXE) { return $env:PATCHGATE_CHOCO_EXE }
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

function Invoke-WithInstallerRetry {
    # Windows Installer allows one installation at a time; right after boot EC2Launch, the SSM
    # agent or Windows Update may hold it (exit 1618). Wait and retry instead of failing.
    param([string]$Description, [scriptblock]$Action, [int]$Attempts = 10, [int]$DelaySeconds = $InstallerBusyDelaySeconds)
    for ($attempt = 1; $attempt -le $Attempts; $attempt++) {
        $code = & $Action
        if ($code -ne $InstallerBusyExitCode) { return $code }
        Write-PatchgateLog "$Description`: Windows Installer busy (1618), retry $attempt/$Attempts in ${DelaySeconds}s" 'WARN'
        Start-Sleep -Seconds $DelaySeconds
    }
    return $code
}

function Get-ChocoVersion {
    # Installed version of a Chocolatey package, or $null.
    param([string]$Name)
    $choco = Get-Chocolatey
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { $output = & $choco list $Name --exact --limit-output 2>$null }
    finally { $ErrorActionPreference = $previous }
    foreach ($line in @($output)) {
        $parts = "$line".Split('|')
        if ($parts.Count -ge 2 -and $parts[0] -ieq $Name) { return $parts[1].Trim() }
    }
    return $null
}

function Invoke-ChocoChange {
    # install (optionally pinned, allowing downgrades) | upgrade | remove. Returns the exit code.
    param([string]$Action, [string]$Name, [string]$Version)
    $choco = Get-Chocolatey
    switch ($Action) {
        'install' {
            $arguments = @('upgrade', $Name, '-y', '--no-progress', '--limit-output')
            if ($Version) { $arguments += @('--version', $Version, '--allow-downgrade') }
        }
        'upgrade' { $arguments = @('upgrade', $Name, '-y', '--no-progress', '--limit-output') }
        'remove' { $arguments = @('uninstall', $Name, '-y', '--no-progress', '--limit-output') }
        default { throw "unknown app action $Action" }
    }
    $code = Invoke-WithInstallerRetry -Description "choco $Action $Name" -Action {
        Invoke-Native -FilePath $choco -Arguments $arguments
    }
    if ($SuccessCodes -notcontains $code) { throw "choco $Action $Name failed (exit $code)" }
    return $code
}

function Get-DesiredApp {
    # Plan apps, plus the legacy 'chocolatey' list from older plans (present, profile, no prune).
    param($Plan)
    $apps = @(Get-Prop $Plan 'apps' @())
    foreach ($legacy in @(Get-Prop $Plan 'chocolatey' @())) {
        if ($legacy) {
            $apps += [pscustomobject]@{ name = $legacy.name; version = (Get-Prop $legacy 'version'); ensure = 'present'; source = 'profile'; prune = $false }
        }
    }
    return $apps | Where-Object { $_ }
}

function Invoke-AppReconcile {
    # Bring applications to the plan's desired state and prune what Patchgate installed and no
    # longer wants. Software Patchgate did not install is never removed by pruning.
    param($Plan, [string]$Mode)
    $desired = @(Get-DesiredApp $Plan)
    $state = Read-State
    $managed = @{}
    if ($state.ContainsKey('managedApps') -and $state['managedApps']) {
        foreach ($p in $state['managedApps'].PSObject.Properties) { $managed[$p.Name] = $p.Value }
    }
    $results = @()
    if ($desired.Count -eq 0 -and $managed.Count -eq 0) { return $results }

    $wanted = @{}
    foreach ($app in $desired) {
        $name = $app.name
        $wanted[$name] = $true
        $ensure = Get-Prop $app 'ensure' 'present'
        $version = Get-Prop $app 'version'
        $current = Get-ChocoVersion $name
        $action = $null
        if ($ensure -eq 'absent') { if ($current) { $action = 'remove' } }
        elseif (-not $current) { $action = 'install' }
        elseif ($ensure -eq 'latest') { $action = 'upgrade' }
        elseif ($version -and $current -ne $version) { $action = 'install' }

        if (-not $action) { $status = 'compliant'; $detail = if ($current) { $current } else { 'absent' } }
        elseif ($Mode -eq 'audit') { $status = 'non-compliant'; $detail = "would $action (installed: $current)" }
        else {
            try {
                if ((Invoke-ChocoChange -Action $action -Name $name -Version $version) -eq $RebootExitCode) {
                    $script:AppsRebootRequired = $true
                }
                $after = Get-ChocoVersion $name
                if ($action -eq 'remove') { $ok = -not $after }
                elseif ($action -eq 'upgrade') { $ok = [bool]$after }
                else { $ok = $after -and (-not $version -or $after -eq $version) }
                $status = if (-not $ok) { 'failed' } elseif ($action -eq 'upgrade' -and $after -eq $current) { 'compliant' } else { 'remediated' }
                $detail = "${action}: $current -> $after"
            }
            catch { $status = 'failed'; $detail = "$_" }
        }
        Write-PatchgateLog ("app {0,-32} {1,-13} {2}" -f $name, $status, $detail)
        $results += [ordered]@{ item = "app:$name"; status = $status; detail = $detail }

        if ($ensure -eq 'absent') { $managed.Remove($name) }
        elseif ($Mode -ne 'audit' -and ($action -eq 'install' -or $managed.ContainsKey($name))) {
            $managed[$name] = [ordered]@{ source = (Get-Prop $app 'source' ''); prune = [bool](Get-Prop $app 'prune' $false) }
        }
    }

    foreach ($name in @($managed.Keys | Sort-Object)) {
        $info = $managed[$name]
        $prune = [bool](Get-Prop ([pscustomobject]$info) 'prune' $false)
        if ($wanted.ContainsKey($name) -or -not $prune) { continue }
        $source = Get-Prop ([pscustomobject]$info) 'source' ''
        if ($Mode -eq 'audit') {
            $results += [ordered]@{ item = "app:$name"; status = 'non-compliant'; detail = "would prune (from $source)" }
            continue
        }
        try {
            if (Get-ChocoVersion $name) { [void](Invoke-ChocoChange -Action 'remove' -Name $name) }
            $results += [ordered]@{ item = "app:$name"; status = 'remediated'; detail = "pruned (was from $source)" }
            Write-PatchgateLog ("app {0,-32} {1,-13} pruned (was from {2})" -f $name, 'remediated', $source)
            $managed.Remove($name)
        }
        catch {
            $results += [ordered]@{ item = "app:$name"; status = 'failed'; detail = "prune: $_" }
        }
    }
    if ($Mode -ne 'audit') { Save-State @{ managedApps = $managed } }
    return $results
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
            $code = Invoke-ChocoChange -Action install -Name $Step.package -Version $version
        }
        { $_ -in 'msi', 'exe' } {
            $file = Get-VerifiedDownload -Url $Step.source -Sha256 $Step.sha256 `
                -Destination (Join-Path $WorkDir ("{0}.{1}" -f $Step.agent, $Step.method))
            $extra = if ($props -contains 'args') { $Step.args } else { '' }
            $code = Invoke-WithInstallerRetry -Description $Step.agent -Action {
                if ($Step.method -eq 'msi') {
                    (Start-Process -FilePath 'msiexec.exe' -Wait -PassThru `
                        -ArgumentList ('/i "{0}" /qn /norestart {1}' -f $file, $extra)).ExitCode
                }
                else {
                    (Start-Process -FilePath $file -ArgumentList $extra -Wait -PassThru).ExitCode
                }
            }
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

function Invoke-Hardening {
    # Run the profile's hardening engine and record evidence. Returns one result per control:
    # compliant | remediated | non-compliant (audit) | failed (remediation did not fix it).
    param($Hardening, [string]$Mode)
    $results = @()
    if ($null -eq $Hardening -or $Hardening -is [string]) { return $results }
    switch ($Hardening.engine) {
        'controls' { $results = @(Invoke-ControlsEngine -Hardening $Hardening -Mode $Mode) }
        'dsc' { $results = @(Invoke-DscEngine -Hardening $Hardening -Mode $Mode) }
        default { throw "unsupported hardening engine: $($Hardening.engine)" }
    }
    Write-JsonFile -Path $ComplianceFile -Data ([ordered]@{
            engine    = $Hardening.engine
            level     = (Get-Prop $Hardening 'level' (Get-Prop $Hardening 'baseline' ''))
            mode      = $Mode
            checkedAt = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
            controls  = $results
        })
    return $results
}

function Invoke-ControlsEngine {
    param($Hardening, [string]$Mode)
    $results = @()
    foreach ($control in $Hardening.controls) {
        if ((Invoke-PlanScript -Script $control.check) -eq 0) {
            $status = 'compliant'
        }
        elseif ($Mode -eq 'audit') {
            $status = 'non-compliant'
        }
        else {
            $code = Invoke-PlanScript -Script $control.remediate
            if ($code -ne 0) { Write-PatchgateLog "control $($control.name) remediation exited with $code" 'ERROR' }
            $status = if ((Invoke-PlanScript -Script $control.check) -eq 0) { 'remediated' } else { 'failed' }
        }
        Write-PatchgateLog ("control {0,-32} {1}" -f $control.name, $status)
        $results += [ordered]@{ control = $control.name; cis = (Get-Prop $control 'cis' ''); status = $status }
    }
    return $results
}

function Invoke-DscEngine {
    # Pinned DSC modules + baseline configuration applied by the LCM (see PatchgateDsc.psm1).
    param($Hardening, [string]$Mode)
    Import-Module (Join-Path $PSScriptRoot 'PatchgateDsc.psm1') -Force
    $modulesRoot = Join-Path $env:ProgramFiles 'WindowsPowerShell\Modules'
    $work = Join-Path $env:TEMP ("patchgate-dsc-{0}" -f [guid]::NewGuid())
    New-Item -ItemType Directory -Force -Path $work | Out-Null
    try {
        foreach ($module in $Hardening.modules) {
            $package = Get-VerifiedDownload -Url $module.source -Sha256 $module.sha256 `
                -Destination (Join-Path $work "$($module.name).nupkg")
            [void](Install-PatchgateModule -Name $module.name -Version $module.version -Package $package -ModulesRoot $modulesRoot)
        }
        Write-PatchgateLog "applying DSC baseline $($Hardening.baseline) (mode $Mode)"
        $outcome = Invoke-PatchgateDscBaseline -Baseline $Hardening -Mode $Mode -WorkDir $work
    }
    finally {
        Remove-Item -Path $work -Recurse -Force -ErrorAction SilentlyContinue
    }
    if ($outcome.rebootRequired) {
        Write-PatchgateLog 'DSC baseline requires a reboot to complete' 'WARN'
        $script:HardeningRebootRequired = $true
    }
    $results = @($outcome.results)
    $summary = ((Get-Summary $results).GetEnumerator() | Sort-Object Key | ForEach-Object { "$($_.Key)=$($_.Value)" }) -join ' '
    Write-PatchgateLog "dsc baseline $($Hardening.baseline): $summary ($($results.Count) rules)"
    foreach ($r in $results) {
        if ($r.status -in 'failed', 'non-compliant') { Write-PatchgateLog ("rule {0,-10} {1,-13} {2}" -f $r.cis, $r.status, $r.title) }
    }
    return $results
}

function Get-Summary {
    param($Results)
    $summary = @{}
    foreach ($r in $Results) {
        if ($summary.ContainsKey($r.status)) { $summary[$r.status] += 1 } else { $summary[$r.status] = 1 }
    }
    return $summary
}

function Invoke-Drift {
    # Returns the exit code for a drift run.
    if (-not (Test-Path $AppliedPlanFile)) {
        Write-PatchgateLog 'no applied plan yet; nothing to check'
        return 0
    }
    $plan = Get-Content -Raw -Path $AppliedPlanFile | ConvertFrom-Json
    $driftCfg = Get-Prop $plan 'drift'
    $interval = Get-Prop $driftCfg 'interval' '30m'
    $mode = Get-Prop $driftCfg 'mode' 'enforce'
    $state = Read-State
    $now = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    if ($interval -eq 'off') { Write-PatchgateLog 'drift checks are off for this profile'; return 0 }
    if (-not $Force -and $state.ContainsKey('lastDriftAt') -and
        ($now - [double]$state['lastDriftAt']) -lt ($DriftIntervals[$interval] - $DriftSlackSeconds)) {
        Write-PatchgateLog "drift check not due (interval $interval)"
        return 0
    }

    Write-PatchgateLog "== drift check (interval $interval, mode $mode)"
    $findings = @()
    $work = Join-Path $env:TEMP ("patchgate-{0}" -f [guid]::NewGuid())
    New-Item -ItemType Directory -Force -Path $work | Out-Null
    try {
        foreach ($step in $plan.steps) {
            if (-not ($step.PSObject.Properties.Name -contains 'validate') -or (Test-Step -Step $step)) { continue }
            if ($mode -eq 'audit') { $findings += [ordered]@{ item = "agent:$($step.agent)"; status = 'non-compliant' }; continue }
            try {
                [void](Install-Step -Step $step -WorkDir $work)
                Assert-Step -Step $step
                $findings += [ordered]@{ item = "agent:$($step.agent)"; status = 'remediated' }
            }
            catch {
                Write-PatchgateLog "agent $($step.agent) could not be restored: $_" 'ERROR'
                $findings += [ordered]@{ item = "agent:$($step.agent)"; status = 'failed' }
            }
        }
    }
    finally {
        Remove-Item -Path $work -Recurse -Force -ErrorAction SilentlyContinue
    }
    foreach ($a in @(Invoke-AppReconcile -Plan $plan -Mode $mode)) {
        if ($a.status -ne 'compliant') { $findings += [ordered]@{ item = $a.item; status = $a.status } }
    }
    foreach ($r in @(Invoke-Hardening -Hardening (Get-Prop $plan 'hardening') -Mode $mode)) {
        if ($r.status -ne 'compliant') { $findings += [ordered]@{ item = "control:$($r.control)"; status = $r.status } }
    }

    Save-State @{ lastDriftAt = $now }
    if ($findings.Count -gt 0) {
        Write-JsonFile -Path $DriftEventFile -Data ([ordered]@{
                profile    = $plan.profile
                digest     = $plan.digest
                mode       = $mode
                findings   = $findings
                detectedAt = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
            })
        Write-PatchgateLog ("PATCHGATE_DRIFT " + ($findings | ConvertTo-Json -Compress -Depth 4)) 'WARN'
    }
    else {
        Write-PatchgateLog 'no drift detected'
    }
    if ($findings | Where-Object { $_.status -eq 'failed' }) { return 1 }
    return 0
}

# ------------------------------------------------------------------ main
New-Item -ItemType Directory -Force -Path (Split-Path $LogFile) | Out-Null

if ($Drift -and $PSCmdlet.ParameterSetName -eq 'Drift') {
    try { exit (Invoke-Drift) }
    catch { Write-PatchgateLog "drift check FAILED: $_" 'ERROR'; exit 1 }
}

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

if (-not $Force) {
    $state = Read-State
    if ($state['digest'] -eq $plan.digest -and $state['status'] -eq 'success') {
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

    $appResults = @(Invoke-AppReconcile -Plan $plan -Mode 'enforce')
    $failedApps = @($appResults | Where-Object { $_.status -eq 'failed' } | ForEach-Object { $_.item })
    if ($failedApps.Count -gt 0) { throw "applications could not be reconciled: $($failedApps -join ', ')" }

    $controls = @(Invoke-Hardening -Hardening (Get-Prop $plan 'hardening') -Mode 'enforce')
    $failed = @($controls | Where-Object { $_.status -eq 'failed' } | ForEach-Object { $_.control })
    if ($failed.Count -gt 0) { throw "hardening controls could not be remediated: $($failed -join ', ')" }
}
catch {
    Write-PatchgateLog "FAILED: $_" 'ERROR'
    Save-State @{ digest = $plan.digest; status = 'failed'; error = "$_" }
    exit 1
}
finally {
    Remove-Item -Path $work -Recurse -Force -ErrorAction SilentlyContinue
}

$hardeningSummary = Get-Summary $controls
Write-JsonFile -Path $AppliedPlanFile -Data $plan
Save-State @{
    digest = $plan.digest; status = 'success'; error = $null
    result = @{ profile = $plan.profile; steps = $results; hardening = $hardeningSummary }
}
Write-PatchgateLog "SUCCESS: profile $($plan.profile) applied"
Write-Output (@{ patchgate = @{ profile = $plan.profile; digest = $plan.digest; steps = $results; hardening = $hardeningSummary } } | ConvertTo-Json -Depth 5 -Compress)
if ($script:AppsRebootRequired -or $script:HardeningRebootRequired) { exit $RebootExitCode }
exit 0
