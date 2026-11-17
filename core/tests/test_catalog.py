from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from patchgate.catalog import (
    MAX_PLAN_BYTES,
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
    plan = {"kind": "InstallPlan", "profile": "big", "padding": "x" * MAX_PLAN_BYTES}
    with pytest.raises(CatalogError, match="max"):
        encode_plan(plan)


def test_decode_rejects_non_plans() -> None:
    with pytest.raises(CatalogError):
        decode_plan(encode_plan({"kind": "Something"}))
