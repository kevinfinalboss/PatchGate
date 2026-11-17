#Requires -Version 5.1
<#
.SYNOPSIS
    "dsc" hardening engine for Patchgate (ADR 0007).

.DESCRIPTION
    Installs pinned DSC modules, compiles the baseline's configuration, applies it with the Local
    Configuration Manager (ApplyOnly: Patchgate drives drift) and reports one result per CIS rule.
    DSC cmdlets are resolved at run time, so tests can replace them with stubs.
#>

Set-StrictMode -Version Latest

$script:CisId = '\](?<id>\d+(?:\.\d+)+)\s+-\s'

function Expand-PatchgateZip {
    # Extract a zip (e.g. a .nupkg), refusing entries that would land outside Destination.
    param([Parameter(Mandatory)][string]$Path, [Parameter(Mandatory)][string]$Destination)
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    New-Item -ItemType Directory -Force -Path $Destination | Out-Null
    $root = [IO.Path]::GetFullPath($Destination).TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar
    $zip = [IO.Compression.ZipFile]::OpenRead($Path)
    try {
        foreach ($entry in $zip.Entries) {
            $target = [IO.Path]::GetFullPath((Join-Path $Destination $entry.FullName))
            if (-not $target.StartsWith($root, [StringComparison]::OrdinalIgnoreCase)) {
                throw "unsafe path in archive: $($entry.FullName)"
            }
        }
        foreach ($entry in $zip.Entries) {
            $target = [IO.Path]::GetFullPath((Join-Path $Destination $entry.FullName))
            if ($entry.FullName.EndsWith('/') -or $entry.FullName.EndsWith('\')) {
                New-Item -ItemType Directory -Force -Path $target | Out-Null
                continue
            }
            New-Item -ItemType Directory -Force -Path (Split-Path $target) | Out-Null
            [IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $target, $true)
        }
    }
    finally {
        $zip.Dispose()
    }
}

function Install-PatchgateModule {
    # Install a verified .nupkg as ModulesRoot\<Name>\<Version>; NuGet metadata is removed.
    param(
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][string]$Version,
        [Parameter(Mandatory)][string]$Package,
        [Parameter(Mandatory)][string]$ModulesRoot
    )
    $target = Join-Path (Join-Path $ModulesRoot $Name) $Version
    $marker = Join-Path $target '.patchgate'
    if (Test-Path $marker) { return $target }
    if (Test-Path $target) { Remove-Item -Recurse -Force -Path $target }
    Expand-PatchgateZip -Path $Package -Destination $target
    foreach ($meta in @('_rels', 'package', '[Content_Types].xml')) {
        Remove-Item -LiteralPath (Join-Path $target $meta) -Recurse -Force -ErrorAction SilentlyContinue
    }
    Get-ChildItem -Path $target -Filter '*.nuspec' | Remove-Item -Force
    Set-Content -Path $marker -Value $Version
    return $target
}

function ConvertTo-PowerShellLiteral {
    param($Value)
    if ($Value -is [bool]) { if ($Value) { return '$true' } else { return '$false' } }
    if ($Value -is [int] -or $Value -is [long] -or $Value -is [double]) { return [string]$Value }
    return "'" + ([string]$Value).Replace("'", "''") + "'"
}

function Get-PatchgateDscScript {
    # PowerShell source of the configuration for a baseline; compiled into a MOF in a child process.
    param([Parameter(Mandatory)]$Baseline, [Parameter(Mandatory)][string]$OutputPath)
    $resource = $Baseline.resource
    $lines = @(
        'Configuration PatchgateBaseline {'
        "    Import-DscResource -ModuleName $(ConvertTo-PowerShellLiteral $resource.module) -Name $(ConvertTo-PowerShellLiteral $resource.name)"
        "    Node 'localhost' {"
        "        $($resource.name) 'Patchgate' {"
    )
    $exclude = @()
    if ($Baseline.PSObject.Properties.Name -contains 'exclude') { $exclude = @($Baseline.exclude) }
    $lines += '            ExcludeList = @(' + (($exclude | ForEach-Object { ConvertTo-PowerShellLiteral $_ }) -join ', ') + ')'
    if ($Baseline.PSObject.Properties.Name -contains 'parameters' -and $null -ne $Baseline.parameters) {
        foreach ($p in $Baseline.parameters.PSObject.Properties) {
            if ($p.Name -notmatch '^[A-Za-z][A-Za-z0-9]*$') { throw "invalid DSC parameter name: $($p.Name)" }
            $lines += "            $($p.Name) = $(ConvertTo-PowerShellLiteral $p.Value)"
        }
    }
    $lines += @(
        '        }'
        '    }'
        '}'
        "PatchgateBaseline -OutputPath $(ConvertTo-PowerShellLiteral $OutputPath) | Out-Null"
    )
    return ($lines -join [Environment]::NewLine)
}

function Get-CisId {
    param([string]$ResourceId)
    if ($ResourceId -match $script:CisId) { return $Matches['id'] }
    return $null
}

function ConvertTo-PatchgateDscEvidence {
    # Before/after Test-DscConfiguration results -> one result per CIS rule (worst status wins).
    param([Parameter(Mandatory)]$Before, $After, [Parameter(Mandatory)][string]$Mode)
    $rank = @{ 'compliant' = 0; 'remediated' = 1; 'non-compliant' = 2; 'failed' = 3 }
    $okBefore = @{}
    foreach ($r in @($Before.ResourcesInDesiredState)) { if ($r) { $okBefore[$r.ResourceId] = $true } }
    $okAfter = @{}
    if ($After) { foreach ($r in @($After.ResourcesInDesiredState)) { if ($r) { $okAfter[$r.ResourceId] = $true } } }

    $rules = @{}
    $all = @($Before.ResourcesInDesiredState) + @($Before.ResourcesNotInDesiredState) | Where-Object { $_ }
    foreach ($resource in $all) {
        $id = Get-CisId $resource.ResourceId
        if (-not $id) { continue }
        if ($okBefore.ContainsKey($resource.ResourceId)) { $status = 'compliant' }
        elseif ($Mode -eq 'audit') { $status = 'non-compliant' }
        elseif ($okAfter.ContainsKey($resource.ResourceId)) { $status = 'remediated' }
        else { $status = 'failed' }
        if (-not $rules.ContainsKey($id) -or $rank[$status] -gt $rank[$rules[$id].status]) {
            $title = ($resource.ResourceId -replace '^\[[^\]]+\]', '' -replace '::\[.*$', '')
            $rules[$id] = [ordered]@{ control = "cis-$id"; cis = $id; status = $status; title = $title.Substring(0, [Math]::Min(160, $title.Length)) }
        }
    }
    $ordered = $rules.Keys | Sort-Object { [version](($_ + '.0.0.0').Split('.')[0..3] -join '.') }, { $_ }
    return @($ordered | ForEach-Object { $rules[$_] })
}

function Initialize-PatchgateLcm {
    # Patchgate drives drift itself: the LCM only applies when asked and never reboots on its own.
    param([Parameter(Mandatory)][string]$WorkDir)
    $meta = Join-Path $WorkDir 'meta'
    $script = @"
[DSCLocalConfigurationManager()]
Configuration PatchgateLcm {
    Node 'localhost' {
        Settings {
            ConfigurationMode  = 'ApplyOnly'
            RefreshMode        = 'Push'
            RebootNodeIfNeeded = `$false
        }
    }
}
PatchgateLcm -OutputPath '$($meta.Replace("'", "''"))' | Out-Null
"@
    $file = Join-Path $WorkDir 'lcm.ps1'
    Set-Content -Path $file -Value $script -Encoding UTF8
    & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $file
    if ($LASTEXITCODE -ne 0) { throw "LCM meta-configuration failed ($LASTEXITCODE)" }
    Set-DscLocalConfigurationManager -Path $meta -Force
}

function Invoke-PatchgateDscBaseline {
    <#
        Apply (enforce) or only evaluate (audit) a DSC baseline. Returns @{ results; rebootRequired }.
        -Compiler and -LcmInitializer wrap Windows-only steps so tests can replace them.
    #>
    param(
        [Parameter(Mandatory)]$Baseline,
        [Parameter(Mandatory)][string]$Mode,
        [Parameter(Mandatory)][string]$WorkDir,
        [scriptblock]$Compiler = {
            param([string]$ScriptPath)
            & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $ScriptPath
            if ($LASTEXITCODE -ne 0) { throw "DSC compilation failed ($LASTEXITCODE)" }
        },
        [scriptblock]$LcmInitializer = { param([string]$Dir) Initialize-PatchgateLcm -WorkDir $Dir }
    )
    $mofDir = Join-Path $WorkDir 'mof'
    $scriptPath = Join-Path $WorkDir 'baseline.ps1'
    Set-Content -Path $scriptPath -Value (Get-PatchgateDscScript -Baseline $Baseline -OutputPath $mofDir) -Encoding UTF8
    & $Compiler $scriptPath
    $mof = Join-Path $mofDir 'localhost.mof'
    if (-not (Test-Path $mof)) { throw 'DSC compilation produced no localhost.mof' }

    $before = Test-DscConfiguration -ReferenceConfiguration $mof
    $after = $null
    $reboot = $false
    if ($Mode -ne 'audit') {
        & $LcmInitializer $WorkDir
        $dscErrors = @()
        Start-DscConfiguration -Path $mofDir -Wait -Force -ErrorAction Continue -ErrorVariable dscErrors | Out-Null
        foreach ($e in @($dscErrors)) { if ($e) { Write-Warning "DSC: $e" } }
        $reboot = [bool](Get-Variable -Name DSCMachineStatus -Scope Global -ValueOnly -ErrorAction SilentlyContinue)
        $after = Test-DscConfiguration -ReferenceConfiguration $mof
    }
    return @{ results = (ConvertTo-PatchgateDscEvidence -Before $before -After $after -Mode $Mode); rebootRequired = $reboot }
}

Export-ModuleMember -Function Expand-PatchgateZip, Install-PatchgateModule, Get-PatchgateDscScript, ConvertTo-PatchgateDscEvidence, Invoke-PatchgateDscBaseline, Initialize-PatchgateLcm
