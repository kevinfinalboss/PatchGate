"""Tests for the dsc hardening engine module, run under PowerShell 7 with DSC cmdlets stubbed."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import zipfile
from pathlib import Path
from typing import Any

import pytest

MODULE = Path(__file__).resolve().parents[1] / "scripts" / "windows" / "PatchgateDsc.psm1"
PWSH = shutil.which("pwsh")
pytestmark = pytest.mark.skipif(PWSH is None, reason="pwsh not installed")


def ps(script: str) -> Any:
    """Run PowerShell with the module imported; the script's last output is parsed as JSON."""
    assert PWSH is not None
    proc = subprocess.run(  # noqa: S603
        [
            PWSH,
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            f"$ErrorActionPreference='Stop'; Import-Module '{MODULE}' -Force; {script}",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
        env={**os.environ, "DOTNET_SYSTEM_GLOBALIZATION_INVARIANT": "1"},
    )
    if proc.returncode != 0:
        raise AssertionError(proc.stderr or proc.stdout)
    return json.loads(proc.stdout) if proc.stdout.strip() else None


def test_zip_extraction_rejects_traversal(tmp_path: Path) -> None:
    archive = tmp_path / "evil.nupkg"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("../outside.txt", "x")
    with pytest.raises(AssertionError, match="unsafe path"):
        ps(f"Expand-PatchgateZip -Path '{archive}' -Destination '{tmp_path / 'out'}'")
    assert not (tmp_path / "outside.txt").exists()


def test_module_install_strips_nuget_metadata_and_is_idempotent(tmp_path: Path) -> None:
    package = tmp_path / "Demo.nupkg"
    with zipfile.ZipFile(package, "w") as z:
        z.writestr("Demo.psd1", "@{ ModuleVersion = '1.2.0' }")
        z.writestr("Demo.nuspec", "<package/>")
        z.writestr("_rels/.rels", "<x/>")
        z.writestr("[Content_Types].xml", "<x/>")
        z.writestr("package/services/metadata/core-properties/x.psmdcp", "<x/>")
    root = tmp_path / "Modules"
    target = ps(
        f"Install-PatchgateModule -Name Demo -Version 1.2.0 -Package '{package}' "
        f"-ModulesRoot '{root}' | ConvertTo-Json"
    )
    installed = Path(target)
    assert installed == root / "Demo" / "1.2.0"
    assert sorted(p.name for p in installed.iterdir()) == [".patchgate", "Demo.psd1"]
    package.unlink()  # second install must not need the package again
    again = f"Install-PatchgateModule -Name Demo -Version 1.2.0 -Package x -ModulesRoot '{root}'"
    ps(again + " | Out-Null")


def baseline_json(**overrides: Any) -> str:
    baseline = {
        "resource": {"module": "CISDSC", "name": "CIS_Server"},
        "parameters": {"LevelOne": True, "LevelTwo": False, "cis2374LegalNoticeText": "It's ours"},
        "exclude": ["2.2.21", "18.1.1"],
        **overrides,
    }
    return json.dumps(baseline).replace("'", "''")


def test_configuration_script_renders_literals_safely() -> None:
    script = ps(
        f"$b = '{baseline_json()}' | ConvertFrom-Json; "
        "Get-PatchgateDscScript -Baseline $b -OutputPath 'C:\\out' | ConvertTo-Json"
    )
    assert "Import-DscResource -ModuleName 'CISDSC' -Name 'CIS_Server'" in script
    assert "ExcludeList = @('2.2.21', '18.1.1')" in script
    assert "LevelOne = $true" in script and "LevelTwo = $false" in script
    assert "cis2374LegalNoticeText = 'It''s ours'" in script  # quotes escaped, no injection


def test_configuration_rejects_invalid_parameter_names() -> None:
    with pytest.raises(AssertionError, match="invalid DSC parameter name"):
        ps(
            f"$b = '{baseline_json(parameters={'x; Remove-Item C:/': 1})}' | ConvertFrom-Json; "
            "Get-PatchgateDscScript -Baseline $b -OutputPath 'C:\\out'"
        )


STUBS = Path(__file__).resolve().parent / "fixtures" / "dsc_stubs.ps1"


@pytest.mark.parametrize(
    ("mode", "expected", "applied"),
    [
        ("enforce", {"1.1.4": "remediated", "1.1.6": "compliant", "18.9.1": "failed"}, True),
        (
            "audit",
            {"1.1.4": "non-compliant", "1.1.6": "compliant", "18.9.1": "non-compliant"},
            False,
        ),
    ],
)
def test_baseline_evidence_per_cis_rule(
    tmp_path: Path, mode: str, expected: dict[str, str], applied: bool
) -> None:
    out = ps(
        f". '{STUBS}'; $b = '{baseline_json()}' | ConvertFrom-Json; "
        f"$r = Invoke-PatchgateDscBaseline -Baseline $b -Mode {mode} -WorkDir '{tmp_path}' "
        "-Compiler $global:compiler -LcmInitializer $global:lcm -Prepare $global:prepare; "
        "@{ results = $r.results; started = [bool]$global:started; lcm = [bool]$global:lcmCalled; "
        "prepared = [bool]$global:prepared } "
        "| ConvertTo-Json -Depth 5"
    )
    assert {r["cis"]: r["status"] for r in out["results"]} == expected
    assert [r["cis"] for r in out["results"]] == ["1.1.4", "1.1.6", "18.9.1"]  # numeric order
    assert out["started"] is applied and out["lcm"] is applied
    assert out["prepared"] is True  # WinRM envelope is raised before any DSC call
    assert out["results"][0]["title"].startswith("1.1.4 - (L1) Ensure min length")
