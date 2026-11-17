from __future__ import annotations

import base64
import hashlib
import importlib.util
import io
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "linux" / "patchgate_install.py"


@pytest.fixture
def installer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    spec = importlib.util.spec_from_file_location("patchgate_install", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["patchgate_install"] = module
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "LOG_DIR", tmp_path / "log")
    state_dir = tmp_path / "state"
    monkeypatch.setattr(module, "STATE_DIR", state_dir)
    monkeypatch.setattr(module, "STATE_FILE", state_dir / "state.json")
    monkeypatch.setattr(module, "PLAN_FILE", state_dir / "plan.json")
    monkeypatch.setattr(module, "COMPLIANCE_FILE", state_dir / "compliance.json")
    monkeypatch.setattr(module, "DRIFT_EVENT_FILE", state_dir / "drift-event.json")
    monkeypatch.setattr(module, "VALIDATE_ATTEMPTS", 2)
    monkeypatch.setattr(module, "VALIDATE_DELAY_SECONDS", 0)
    monkeypatch.setattr(module, "detect_package_manager", lambda: "dnf")
    return module


def plan(*steps: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": "InstallPlan",
        "platform": "linux",
        "profile": "test",
        "digest": "sha256:" + "0" * 64,
        "hardening": None,
        "drift": {"interval": "30m", "mode": "enforce"},
        "steps": list(steps),
    }


def b64(doc: dict[str, Any]) -> str:
    return base64.b64encode(json.dumps(doc).encode()).decode()


def test_script_step_runs_and_validates(installer: ModuleType, tmp_path: Path) -> None:
    marker = tmp_path / "ran"
    step = {
        "agent": "demo",
        "method": "script",
        "script": f"touch {marker}",
        "validate": f"test -f {marker}",
    }
    assert installer.main(["--plan-b64", b64(plan(step))]) == 0
    state = json.loads((tmp_path / "state" / "state.json").read_text())
    assert state["status"] == "success"
    assert marker.exists()


def test_failing_script_fails_plan_and_records_state(installer: ModuleType, tmp_path: Path) -> None:
    step = {"agent": "broken", "method": "script", "script": "exit 7"}
    assert installer.main(["--plan-b64", b64(plan(step))]) == 1
    assert json.loads((tmp_path / "state" / "state.json").read_text())["status"] == "failed"


def test_validation_failure_fails_plan(installer: ModuleType) -> None:
    step = {"agent": "demo", "method": "script", "script": "true", "validate": "false"}
    assert installer.main(["--plan-b64", b64(plan(step))]) == 1


def test_already_applied_plan_only_validates(installer: ModuleType, tmp_path: Path) -> None:
    counter = tmp_path / "count"
    step = {"agent": "demo", "method": "script", "script": f"echo x >> {counter}"}
    doc = plan(step)
    assert installer.main(["--plan-b64", b64(doc)]) == 0
    assert installer.main(["--plan-b64", b64(doc)]) == 0
    assert counter.read_text().count("x") == 1
    assert installer.main(["--plan-b64", b64(doc), "--force"]) == 0
    assert counter.read_text().count("x") == 2


def test_invalid_plan_returns_2(installer: ModuleType) -> None:
    assert installer.main(["--plan-b64", "not-base64!!"]) == 2


def test_windows_plan_is_rejected(installer: ModuleType) -> None:
    doc = plan()
    doc["platform"] = "windows"
    assert installer.main(["--plan-b64", b64(doc)]) == 1


def test_download_verifies_checksum(
    installer: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = b"agent-binary"
    monkeypatch.setattr(installer.urllib.request, "urlopen", lambda *a, **k: io.BytesIO(payload))
    good = hashlib.sha256(payload).hexdigest()
    dest = installer.download("https://example.com/a.rpm", good, tmp_path / "a.rpm")
    assert dest.read_bytes() == payload
    with pytest.raises(installer.InstallError, match="checksum mismatch"):
        installer.download("https://example.com/a.rpm", "f" * 64, tmp_path / "b.rpm")
    assert not (tmp_path / "b.rpm").exists()


def test_download_refuses_plain_http(installer: ModuleType, tmp_path: Path) -> None:
    with pytest.raises(installer.InstallError, match="non-https"):
        installer.download("http://example.com/a.rpm", "f" * 64, tmp_path / "a.rpm")


@pytest.mark.parametrize(
    ("manager", "expected"), [("dnf", ["a"]), ("yum", ["a"]), ("apt-get", ["b"])]
)
def test_package_list_per_manager(installer: ModuleType, manager: str, expected: list[str]) -> None:
    spec = {"packages": {"dnf": ["a"], "apt": ["b"]}}
    assert installer.packages_for(spec, manager) == expected


def test_missing_package_list_for_manager(installer: ModuleType) -> None:
    with pytest.raises(installer.InstallError, match="no package list for apt"):
        installer.packages_for({"packages": {"dnf": ["a"]}}, "apt-get")


def test_real_catalog_linux_plans_are_accepted_by_installer(installer: ModuleType) -> None:
    from patchgate.catalog import Catalog

    catalog = Catalog.load(Path(__file__).resolve().parents[1] / "catalog")
    for name, compiled in catalog.compile_all().items():
        if compiled["platform"] == "linux":
            for step in compiled["steps"]:
                assert step["method"] in ("package", "rpm", "deb", "script"), name
                if step["method"] == "package":
                    installer.packages_for(step, "dnf")


def test_accepts_compressed_plans_from_the_catalog(installer: ModuleType, tmp_path: Path) -> None:
    from patchgate.catalog import encode_plan

    marker = tmp_path / "gz"
    encoded = encode_plan(plan({"agent": "demo", "method": "script", "script": f"touch {marker}"}))
    assert encoded.startswith("gz1:")
    assert installer.main(["--plan-b64", encoded]) == 0
    assert marker.exists()


# ---------------------------------------------------------------- controls engine & drift


def control(name: str, target: Path) -> dict[str, str]:
    """A control whose 'setting' is the presence of a file."""
    return {
        "name": name,
        "cis": "1.2.3",
        "check": f"test -f {target}",
        "remediate": f"touch {target}",
    }


def hardened_plan(*controls: dict[str, str], mode: str = "enforce", interval: str = "30m") -> Any:
    doc = plan()
    doc["hardening"] = {"engine": "controls", "level": "baseline", "controls": list(controls)}
    doc["drift"] = {"interval": interval, "mode": mode}
    return doc


def state_dir(tmp_path: Path) -> Path:
    return tmp_path / "state"


def test_controls_are_remediated_and_evidence_is_written(
    installer: ModuleType, tmp_path: Path
) -> None:
    already = tmp_path / "already"
    already.touch()
    missing = tmp_path / "missing"
    doc = hardened_plan(control("ok", already), control("fixme", missing))
    assert installer.main(["--plan-b64", b64(doc)]) == 0
    assert missing.exists()
    evidence = json.loads((state_dir(tmp_path) / "compliance.json").read_text())
    assert {c["control"]: c["status"] for c in evidence["controls"]} == {
        "ok": "compliant",
        "fixme": "remediated",
    }
    assert json.loads((state_dir(tmp_path) / "plan.json").read_text())["digest"] == doc["digest"]


def test_unfixable_control_fails_bootstrap(installer: ModuleType, tmp_path: Path) -> None:
    broken = {"name": "broken", "check": "false", "remediate": "true"}
    assert installer.main(["--plan-b64", b64(hardened_plan(broken))]) == 1
    state = json.loads((state_dir(tmp_path) / "state.json").read_text())
    assert "broken" in state["error"]


def test_drift_corrects_manual_changes_and_emits_event(
    installer: ModuleType, tmp_path: Path
) -> None:
    setting = tmp_path / "setting"
    assert installer.main(["--plan-b64", b64(hardened_plan(control("keep", setting)))]) == 0
    setting.unlink()  # someone reverts the hardening by hand
    assert installer.main(["--drift", "--force"]) == 0
    assert setting.exists()
    event = json.loads((state_dir(tmp_path) / "drift-event.json").read_text())
    assert event["findings"] == [{"item": "control:keep", "status": "remediated"}]


def test_drift_audit_mode_only_reports(installer: ModuleType, tmp_path: Path) -> None:
    setting = tmp_path / "setting"
    doc = hardened_plan(control("keep", setting), mode="audit")
    setting.touch()
    assert installer.main(["--plan-b64", b64(doc)]) == 0
    setting.unlink()
    assert installer.main(["--drift", "--force"]) == 0
    assert not setting.exists()
    event = json.loads((state_dir(tmp_path) / "drift-event.json").read_text())
    assert event["findings"][0]["status"] == "non-compliant"


def test_drift_reinstalls_agent_that_was_removed(installer: ModuleType, tmp_path: Path) -> None:
    marker = tmp_path / "agent"
    step = {
        "agent": "demo",
        "method": "script",
        "script": f"touch {marker}",
        "validate": f"test -f {marker}",
    }
    assert installer.main(["--plan-b64", b64(plan(step))]) == 0
    marker.unlink()
    assert installer.main(["--drift", "--force"]) == 0
    assert marker.exists()


def test_no_drift_writes_no_event(installer: ModuleType, tmp_path: Path) -> None:
    setting = tmp_path / "setting"
    assert installer.main(["--plan-b64", b64(hardened_plan(control("keep", setting)))]) == 0
    assert installer.main(["--drift", "--force"]) == 0
    assert not (state_dir(tmp_path) / "drift-event.json").exists()


def test_drift_without_applied_plan_is_a_no_op(installer: ModuleType) -> None:
    assert installer.main(["--drift"]) == 0


@pytest.mark.parametrize(
    ("interval", "elapsed", "due"),
    [
        ("30m", 0, False),
        ("30m", 1700, True),  # within the slack window
        ("2h", 3600, False),
        ("2h", 7200, True),
        ("off", 999_999, False),
    ],
)
def test_drift_interval(installer: ModuleType, interval: str, elapsed: int, due: bool) -> None:
    assert installer.drift_due(interval, 1_000_000.0, 1_000_000.0 + elapsed) is due


def test_first_drift_run_is_always_due(installer: ModuleType) -> None:
    assert installer.drift_due("24h", None, 0.0) is True


def test_drift_respects_interval_between_runs(installer: ModuleType, tmp_path: Path) -> None:
    setting = tmp_path / "setting"
    assert installer.main(["--plan-b64", b64(hardened_plan(control("keep", setting)))]) == 0
    assert installer.main(["--drift"]) == 0  # first run: due
    setting.unlink()
    assert installer.main(["--drift"]) == 0  # 30m not elapsed: skipped
    assert not setting.exists()


# ---------------------------------------------------------------- ansible engine


def ansible_report(*tasks: tuple[str, dict[str, bool]]) -> dict[str, Any]:
    return {
        "plays": [
            {
                "tasks": [
                    {"task": {"name": name}, "hosts": {"localhost": result}}
                    for name, result in tasks
                ]
            }
        ]
    }


def test_ansible_results_map_to_cis_rules(installer: ModuleType) -> None:
    report = ansible_report(
        ("Gathering Facts", {"changed": False}),
        ("1.1.1.1 | PATCH | Ensure cramfs is disabled", {"changed": True}),
        ("1.1.1.1 | AUDIT | Ensure cramfs is disabled", {"changed": False}),
        ("5.2.10 | PATCH | Ensure root login is disabled", {"changed": False}),
        ("4.1.1 | PATCH | Ensure auditd", {"skipped": True}),
        ("6.1.3 | PATCH | shadow perms", {"failed": True}),
    )
    results = installer.parse_ansible_results(report, "enforce")
    assert [(r["cis"], r["status"]) for r in results] == [
        ("1.1.1.1", "remediated"),  # worst status of the rule's tasks wins
        ("5.2.10", "compliant"),
        ("6.1.3", "failed"),
    ]
    audit = installer.parse_ansible_results(report, "audit")
    assert audit[0]["status"] == "non-compliant"


def make_tar(path: Path, members: dict[str, bytes], links: dict[str, str] | None = None) -> Path:
    import io as _io
    import tarfile

    with tarfile.open(path, "w:gz") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, _io.BytesIO(data))
        for name, target in (links or {}).items():
            info = tarfile.TarInfo(name)
            info.type = tarfile.SYMTYPE
            info.linkname = target
            tar.addfile(info)
    return path


@pytest.mark.parametrize(
    ("members", "links"),
    [
        ({"../evil": b"x"}, None),
        ({"/etc/evil": b"x"}, None),
        ({"ok/file": b"x"}, {"ok/link": "../../etc/passwd"}),
    ],
)
def test_safe_extract_rejects_escapes(
    installer: ModuleType,
    tmp_path: Path,
    members: dict[str, bytes],
    links: dict[str, str] | None,
) -> None:
    archive = make_tar(tmp_path / "a.tar.gz", members, links)
    dest = tmp_path / "out"
    dest.mkdir()
    with pytest.raises(installer.InstallError):
        installer.safe_extract(archive, dest)


def test_ansible_engine_end_to_end_with_fake_playbook(
    installer: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    report = ansible_report(
        ("1.1.1.1 | PATCH | Ensure cramfs is disabled", {"changed": True}),
        ("5.2.10 | PATCH | Ensure root login is disabled", {"changed": False}),
    )
    fake = bin_dir / "ansible-playbook"
    fake.write_text(
        f"#!/bin/sh\necho \"$@\" > {tmp_path}/args\ncat <<'JSON'\n{json.dumps(report)}\nJSON\n"
    )
    fake.chmod(0o755)
    playbook = tmp_path / "role" / "site.yml"
    playbook.parent.mkdir()
    playbook.write_text("---\n")
    monkeypatch.setattr(installer, "ensure_ansible_runtime", lambda: bin_dir)
    monkeypatch.setattr(installer, "ensure_baseline", lambda h, b, w: playbook)

    doc = plan()
    doc["hardening"] = {
        "engine": "ansible",
        "baseline": "demo",
        "source": "https://example.com/role.tar.gz",
        "sha256": "a" * 64,
        "playbook": "site.yml",
        "variables": {"level_2": False},
    }
    assert installer.main(["--plan-b64", b64(doc)]) == 0
    evidence = json.loads((state_dir(tmp_path) / "compliance.json").read_text())
    assert evidence["engine"] == "ansible"
    assert [c["status"] for c in evidence["controls"]] == ["remediated", "compliant"]
    assert "--check" not in (tmp_path / "args").read_text()


def test_ensure_baseline_verifies_and_installs_collections(
    installer: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    role = make_tar(tmp_path / "role.tar.gz", {"ROLE-1.0/site.yml": b"---\n"})
    downloads: list[str] = []

    def fake_download(url: str, sha: str, dest: Path) -> Path:
        downloads.append(url)
        dest.write_bytes(role.read_bytes())
        return dest

    commands: list[list[str]] = []
    monkeypatch.setattr(installer, "download", fake_download)
    monkeypatch.setattr(installer, "run", lambda cmd, **kw: commands.append(cmd))
    monkeypatch.setattr(installer, "BASELINES_DIR", tmp_path / "baselines")
    hardening = {
        "baseline": "demo",
        "source": "https://example.com/role.tar.gz",
        "sha256": "b" * 64,
        "playbook": "site.yml",
        "collections": [
            {"name": "a.b", "version": "1.0.0", "source": "https://x/a.tgz", "sha256": "c" * 64}
        ],
    }
    work = tmp_path / "work"
    work.mkdir()
    playbook = installer.ensure_baseline(hardening, tmp_path / "bin", work)
    assert playbook.name == "site.yml" and playbook.exists()
    assert downloads == ["https://example.com/role.tar.gz", "https://x/a.tgz"]
    assert "--no-deps" in commands[0] and "--offline" in commands[0]

    # Second call is a no-op: content is cached per baseline digest.
    downloads.clear()
    installer.ensure_baseline(hardening, tmp_path / "bin", work)
    assert downloads == []
