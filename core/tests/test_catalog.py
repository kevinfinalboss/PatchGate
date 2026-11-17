from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from patchgate.catalog import (
    Catalog,
    CatalogError,
    decode_plan,
    encode_plan,
    validate_document,
)

REAL_CATALOG = Path(__file__).resolve().parents[1] / "catalog"


def agent(name: str = "demo", **install: Any) -> dict[str, Any]:
    return {
        "apiVersion": "patchgate.io/v1alpha1",
        "kind": "Agent",
        "metadata": {"name": name},
        "spec": {"install": install or {"linux": {"method": "script", "script": "true"}}},
    }


def profile(name: str = "p", **spec: Any) -> dict[str, Any]:
    return {
        "apiVersion": "patchgate.io/v1alpha1",
        "kind": "Profile",
        "metadata": {"name": name},
        "spec": {"platform": "linux", **spec},
    }


def write_catalog(root: Path, *docs: dict[str, Any]) -> Path:
    for doc in docs:
        folder = root / ("agents" if doc["kind"] == "Agent" else "profiles")
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{doc['metadata']['name']}.yaml").write_text(yaml.safe_dump(doc))
    return root


def test_real_catalog_is_valid_and_every_profile_compiles() -> None:
    catalog = Catalog.load(REAL_CATALOG)
    plans = catalog.compile_all()
    assert set(plans) == set(catalog.profiles)
    for plan in plans.values():
        assert decode_plan(encode_plan(plan)) == plan


def test_download_without_checksum_is_rejected() -> None:
    doc = agent(windows={"method": "msi", "source": "https://example.com/a.msi"})
    assert any("sha256" in e for e in validate_document(doc))


@pytest.mark.parametrize("args", ["/qn & calc.exe", "/qn; rm -rf /", "$(whoami)", "a\nb"])
def test_installer_args_reject_shell_metacharacters(args: str) -> None:
    doc = agent(
        windows={
            "method": "msi",
            "source": "https://example.com/a.msi",
            "sha256": "a" * 64,
            "args": args,
        }
    )
    assert validate_document(doc)


@pytest.mark.parametrize("source", ["http://example.com/a.msi", "file:///c:/a.msi"])
def test_only_https_sources_are_allowed(source: str) -> None:
    doc = agent(windows={"method": "msi", "source": source, "sha256": "a" * 64})
    assert validate_document(doc)


def test_unknown_agent_in_profile_is_reported(tmp_path: Path) -> None:
    write_catalog(tmp_path, agent("demo"), profile("p", agents=["missing"]))
    with pytest.raises(CatalogError, match="unknown agent 'missing'"):
        Catalog.load(tmp_path)


def test_agent_must_support_profile_platform(tmp_path: Path) -> None:
    write_catalog(
        tmp_path,
        agent("winonly", windows={"method": "script", "script": "exit 0"}),
        profile("p", agents=["winonly"]),
    )
    with pytest.raises(CatalogError, match="does not support linux"):
        Catalog.load(tmp_path)


def test_chocolatey_only_on_windows(tmp_path: Path) -> None:
    write_catalog(tmp_path, agent("demo"), profile("p", chocolatey=[{"name": "7zip"}]))
    with pytest.raises(CatalogError, match="chocolatey is only supported on windows"):
        Catalog.load(tmp_path)


def test_file_name_must_match_metadata_name(tmp_path: Path) -> None:
    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "other.yaml").write_text(yaml.safe_dump(agent("demo")))
    with pytest.raises(CatalogError, match="file name must match"):
        Catalog.load(tmp_path)


def test_compile_keeps_agent_order_and_validation(tmp_path: Path) -> None:
    first = agent("first")
    first["spec"]["validate"] = {"linux": "true"}
    write_catalog(tmp_path, first, agent("second"), profile("p", agents=["second", "first"]))
    plan = Catalog.load(tmp_path).compile("p")
    assert [s["agent"] for s in plan["steps"]] == ["second", "first"]
    assert plan["steps"][1]["validate"] == "true"
    assert "validate" not in plan["steps"][0]
    assert plan["digest"].startswith("sha256:")


def test_digest_changes_when_catalog_changes(tmp_path: Path) -> None:
    write_catalog(tmp_path, agent("demo"), profile("p", agents=["demo"]))
    before = Catalog.load(tmp_path).compile("p")["digest"]
    write_catalog(tmp_path, profile("p", agents=["demo"], ring=2))
    assert Catalog.load(tmp_path).compile("p")["digest"] != before


def test_oversized_plan_is_rejected() -> None:
    import secrets

    plan = {"kind": "InstallPlan", "profile": "big", "padding": secrets.token_hex(4000)}
    with pytest.raises(CatalogError, match="max"):
        encode_plan(plan)


def test_encoded_plan_is_compressed_and_prefixed() -> None:
    plan = Catalog.load(REAL_CATALOG).compile("web-windows")
    encoded = encode_plan(plan)
    assert encoded.startswith("gz1:")
    assert len(encoded) < 3_800


def test_decode_rejects_non_plans() -> None:
    with pytest.raises(CatalogError):
        decode_plan(encode_plan({"kind": "Something"}))


def test_windows_scripts_do_not_write_utf8_with_bom() -> None:
    """Windows PowerShell 5.1 `-Encoding UTF8` adds a BOM that breaks JSON/YAML consumers."""
    catalog = Catalog.load(REAL_CATALOG)
    offenders = []
    for name, agent_doc in catalog.agents.items():
        windows = agent_doc["spec"]["install"].get("windows", {})
        for field in ("script", "postInstall"):
            if "-Encoding UTF8" in windows.get(field, ""):
                offenders.append(f"{name}.{field}")
    assert offenders == [], "use [IO.File]::WriteAllText(path, text, UTF8Encoding($false))"


# ---------------------------------------------------------------- controls


def ctrl(name: str, platform: str = "linux", level: str = "baseline") -> dict[str, Any]:
    return {
        "apiVersion": "patchgate.io/v1alpha1",
        "kind": "Control",
        "metadata": {"name": name, "description": "d"},
        "spec": {"platform": platform, "level": level, "check": "true", "remediate": "true"},
    }


def write_controls(root: Path, *docs: dict[str, Any]) -> None:
    folder = root / "controls"
    folder.mkdir(parents=True, exist_ok=True)
    for doc in docs:
        (folder / f"{doc['metadata']['name']}.yaml").write_text(yaml.safe_dump(doc))


def test_levels_are_cumulative_and_platform_scoped(tmp_path: Path) -> None:
    write_catalog(
        tmp_path,
        profile("base", hardening={"engine": "controls", "level": "baseline"}),
        profile("strict", hardening={"engine": "controls", "level": "cis-level1"}),
    )
    write_controls(
        tmp_path,
        ctrl("b1"),
        ctrl("c1", level="cis-level1"),
        ctrl("w1", platform="windows"),
    )
    catalog = Catalog.load(tmp_path)

    def names(p: str) -> list[str]:
        return [c["name"] for c in catalog.compile(p)["hardening"]["controls"]]

    assert names("base") == ["b1"]
    assert names("strict") == ["b1", "c1"]


def test_excluded_controls_are_skipped_and_validated(tmp_path: Path) -> None:
    write_controls(tmp_path, ctrl("b1"), ctrl("b2"))
    write_catalog(tmp_path, profile("p", hardening={"engine": "controls", "exclude": ["b2"]}))
    plan = Catalog.load(tmp_path).compile("p")
    assert [c["name"] for c in plan["hardening"]["controls"]] == ["b1"]

    write_catalog(tmp_path, profile("p", hardening={"engine": "controls", "exclude": ["nope"]}))
    with pytest.raises(CatalogError, match="unknown control 'nope'"):
        Catalog.load(tmp_path)


def test_exclude_must_match_platform(tmp_path: Path) -> None:
    write_controls(tmp_path, ctrl("w1", platform="windows"))
    write_catalog(tmp_path, profile("p", hardening={"engine": "controls", "exclude": ["w1"]}))
    with pytest.raises(CatalogError, match="not for linux"):
        Catalog.load(tmp_path)


def test_drift_defaults_and_no_hardening(tmp_path: Path) -> None:
    write_catalog(tmp_path, profile("p"))
    plan = Catalog.load(tmp_path).compile("p")
    assert plan["hardening"] is None
    assert plan["drift"] == {"interval": "30m", "mode": "enforce"}


def test_drift_interval_must_be_supported() -> None:
    doc = profile("p", drift={"interval": "45m"})
    assert validate_document(doc)


def test_every_real_control_has_cis_reference_and_fits_in_plans() -> None:
    catalog = Catalog.load(REAL_CATALOG)
    assert len(catalog.controls) >= 30
    assert all(c["metadata"].get("references", {}).get("cis") for c in catalog.controls.values())
    for plan in catalog.compile_all().values():
        encode_plan(plan)  # raises if a profile outgrows the transport limit


@pytest.mark.parametrize(
    ("profile_name", "engine"), [("cis-linux", "ansible"), ("cis-windows", "dsc")]
)
def test_baseline_profiles_compile_the_baseline_not_controls(
    profile_name: str, engine: str
) -> None:
    hardening = Catalog.load(REAL_CATALOG).compile(profile_name)["hardening"]
    assert hardening["engine"] == engine
    assert "controls" not in hardening
    assert hardening["baseline"]


def test_baseline_engine_must_match_platform() -> None:
    doc = {
        "apiVersion": "patchgate.io/v1alpha1",
        "kind": "Baseline",
        "metadata": {"name": "x", "description": "d"},
        "spec": {
            "engine": "dsc",
            "platform": "linux",
            "modules": [
                {"name": "M", "version": "1.0", "source": "https://e.com/m", "sha256": "a" * 64}
            ],
            "resource": {"module": "M", "name": "R"},
        },
    }
    assert validate_document(doc)
