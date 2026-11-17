# Stubs replacing Windows-only DSC cmdlets for tests of PatchgateDsc.psm1 under PowerShell 7.
$global:calls = 0

function global:New-Resource([string]$Id) { [pscustomobject]@{ ResourceId = $Id } }

function global:Test-DscConfiguration {
    param($ReferenceConfiguration)
    $global:calls++
    $ok = @(New-Resource '[Registry]1.1.6 - (L1) Ensure relax::[CIS_Server]Patchgate')
    $fixable = New-Resource '[AccountPolicy]1.1.4 - (L1) Ensure min length::[CIS_Server]Patchgate'
    $broken = New-Resource '[Registry]18.9.1 - (L1) Ensure thing::[CIS_Server]Patchgate'
    if ($global:calls -eq 1) {
        return [pscustomobject]@{ ResourcesInDesiredState = $ok; ResourcesNotInDesiredState = @($fixable, $broken) }
    }
    # After applying: 1.1.4 is fixed, 18.9.1 still fails.
    return [pscustomobject]@{ ResourcesInDesiredState = @($ok + $fixable); ResourcesNotInDesiredState = @($broken) }
}

function global:Start-DscConfiguration {
    [CmdletBinding()]
    param($Path, [switch]$Wait, [switch]$Force)
    $global:started = $true
}

$global:compiler = {
    param($ScriptPath)
    $dir = Join-Path (Split-Path $ScriptPath) 'mof'
    New-Item -ItemType Directory -Force $dir | Out-Null
    Set-Content (Join-Path $dir 'localhost.mof') 'mof'
}

$global:lcm = { param($Dir) $global:lcmCalled = $true }
