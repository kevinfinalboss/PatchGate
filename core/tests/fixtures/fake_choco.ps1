# Fake Chocolatey for installer tests: packages live in the JSON file $env:PATCHGATE_FAKE_CHOCO_DB.
$db = $env:PATCHGATE_FAKE_CHOCO_DB
$installed = @{}
if (Test-Path $db) { (Get-Content -Raw $db | ConvertFrom-Json).PSObject.Properties | ForEach-Object { $installed[$_.Name] = $_.Value } }
$command, $name = $args[0], $args[1]
$version = $null
for ($i = 0; $i -lt $args.Count; $i++) { if ($args[$i] -eq '--version') { $version = $args[$i + 1] } }
if ($env:PATCHGATE_FAKE_CHOCO_BROKEN -and $name -eq $env:PATCHGATE_FAKE_CHOCO_BROKEN -and $command -ne 'list') { exit 1 }
switch ($command) {
    'list' { if ($installed.ContainsKey($name)) { Write-Output "$name|$($installed[$name])" }; exit 0 }
    'upgrade' { $installed[$name] = if ($version) { $version } else { '9.9' } }
    'uninstall' { $installed.Remove($name) }
    default { exit 0 }
}
$installed | ConvertTo-Json | Set-Content -Path $db
exit 0
