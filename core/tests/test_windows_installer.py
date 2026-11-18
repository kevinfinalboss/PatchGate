"""Behavioural tests for the Windows installer, executed with PowerShell 7 (pwsh).

Windows-only cmdlets are not exercised here; these tests cover plan handling, idempotency,
failure reporting, reboot signalling and source validation. Skipped when pwsh is absent.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "windows" / "Install-PatchgatePlan.ps1"
PWSH = shutil.which("pwsh")

pytestmark = pytest.mark.skipif(PWSH is None, reason="pwsh not installed")


def plan(*steps: dict[str, Any], digest: str = "sha256:1") -> str:
    doc = {
        "kind": "InstallPlan",
        "platform": "windows",
        "profile": "test",
        "digest": digest,
        "hardening": "none",
        "chocolatey": [],
        "steps": list(steps),
    }
    return base64.b64encode(json.dumps(doc).encode()).decode()


def run(tmp_path: Path, encoded: str | None, *extra: str) -> tuple[int, dict[str, Any]]:
    env = {
        **os.environ,
        "ProgramData": str(tmp_path / "pd"),
        "TEMP": str(tmp_path / "tmp"),
        "DOTNET_SYSTEM_GLOBALIZATION_INVARIANT": "1",
        "PATCHGATE_INSTALLER_RETRY_SECONDS": "0",
    }
    (tmp_path / "tmp").mkdir(exist_ok=True)
    assert PWSH is not None
    proc = subprocess.run(  # noqa: S603
        [PWSH, "-NoProfile", "-File", str(SCRIPT)]
        + (["-PlanBase64", encoded] if encoded is not None else [])
        + list(extra),
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    state_file = tmp_path / "pd" / "Patchgate" / "state.json"
    state = json.loads(state_file.read_text(encoding="utf-8-sig")) if state_file.exists() else {}
    return proc.returncode, state


def test_script_step_succeeds_and_is_idempotent(tmp_path: Path) -> None:
    marker = tmp_path / "count.txt"
    step = {
        "agent": "demo",
        "method": "script",
        "script": f'Add-Content -Path "{marker}" -Value x',
        "validate": f'if (-not (Test-Path "{marker}")) {{ exit 1 }}',
    }
    code, state = run(tmp_path, plan(step))
    assert (code, state["status"]) == (0, "success")
    code, _ = run(tmp_path, plan(step))
    assert code == 0
    code, _ = run(tmp_path, plan(step), "-Force")
    assert code == 0
    assert marker.read_text().count("x") == 1  # validation passing skips reinstall


def test_failing_step_reports_failure(tmp_path: Path) -> None:
    code, state = run(tmp_path, plan({"agent": "broken", "method": "script", "script": "exit 7"}))
    assert code == 1
    assert state["status"] == "failed"
    assert "exited with 7" in state["error"]


@pytest.mark.skipif(os.name != "nt", reason="POSIX truncates exit codes to 8 bits (3010 -> 194)")
def test_reboot_request_is_propagated(tmp_path: Path) -> None:
    step = {"agent": "needs-reboot", "method": "script", "script": "exit 3010"}
    code, state = run(tmp_path, plan(step))
    assert code == 3010
    assert state["status"] == "reboot-pending"


def test_non_https_download_is_refused(tmp_path: Path) -> None:
    step = {"agent": "x", "method": "msi", "source": "http://example.com/a.msi", "sha256": "a" * 64}
    code, state = run(tmp_path, plan(step))
    assert code == 1
    assert "non-https" in state["error"]


def test_invalid_input_returns_2(tmp_path: Path) -> None:
    code, _ = run(tmp_path, "!!not-base64")
    assert code == 2


def test_linux_plan_is_rejected(tmp_path: Path) -> None:
    doc = json.loads(base64.b64decode(plan()))
    doc["platform"] = "linux"
    code, _ = run(tmp_path, base64.b64encode(json.dumps(doc).encode()).decode())
    assert code == 2


def test_accepts_compressed_plans_from_the_catalog(tmp_path: Path) -> None:
    from patchgate.catalog import encode_plan

    marker = tmp_path / "gz.txt"
    doc = json.loads(base64.b64decode(plan()))
    doc["steps"] = [
        {"agent": "demo", "method": "script", "script": f'Set-Content -Path "{marker}" -Value 1'}
    ]
    code, state = run(tmp_path, encode_plan(doc))
    assert (code, state["status"]) == (0, "success")
    assert marker.exists()


def test_locked_log_file_does_not_abort_installation(tmp_path: Path) -> None:
    """A log shipper holding the log file open must not break the install."""
    log_dir = tmp_path / "pd" / "Patchgate" / "logs"
    log_dir.mkdir(parents=True)
    (log_dir / "install.log").mkdir()  # a directory where the file should be: every write fails
    step = {"agent": "demo", "method": "script", "script": "exit 0"}
    code, state = run(tmp_path, plan(step))
    assert (code, state["status"]) == (0, "success")


# ---------------------------------------------------------------- controls engine & drift


def control(name: str, target: Path) -> dict[str, str]:
    return {
        "name": name,
        "cis": "1.2.3",
        "check": f'if (Test-Path "{target}") {{ exit 0 }} else {{ exit 1 }}',
        "remediate": f'New-Item -ItemType File -Force -Path "{target}" | Out-Null',
    }


def hardened(*controls: dict[str, str], mode: str = "enforce") -> str:
    doc = json.loads(base64.b64decode(plan()))
    doc["hardening"] = {"engine": "controls", "level": "baseline", "controls": list(controls)}
    doc["drift"] = {"interval": "30m", "mode": mode}
    return base64.b64encode(json.dumps(doc).encode()).decode()


def read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))  # no BOM tolerated


def test_controls_are_remediated_with_evidence(tmp_path: Path) -> None:
    present, missing = tmp_path / "present", tmp_path / "missing"
    present.touch()
    code, state = run(tmp_path, hardened(control("ok", present), control("fixme", missing)))
    assert (code, state["status"]) == (0, "success")
    assert missing.exists()
    evidence = read(tmp_path / "pd" / "Patchgate" / "compliance.json")
    assert {c["control"]: c["status"] for c in evidence["controls"]} == {
        "ok": "compliant",
        "fixme": "remediated",
    }
    assert (tmp_path / "pd" / "Patchgate" / "plan.json").exists()


def test_unfixable_control_fails(tmp_path: Path) -> None:
    broken = {"name": "broken", "check": "exit 1", "remediate": "exit 0"}
    code, state = run(tmp_path, hardened(broken))
    assert code == 1
    assert "broken" in state["error"]


def test_drift_corrects_and_writes_event_without_bom(tmp_path: Path) -> None:
    setting = tmp_path / "setting"
    assert run(tmp_path, hardened(control("keep", setting)))[0] == 0
    setting.unlink()
    code, state = run(tmp_path, None, "-Drift", "-Force")
    assert code == 0
    assert setting.exists()
    event = read(tmp_path / "pd" / "Patchgate" / "drift-event.json")
    assert event["findings"] == [{"item": "control:keep", "status": "remediated"}]
    assert "lastDriftAt" in state


def test_drift_audit_mode_only_reports(tmp_path: Path) -> None:
    setting = tmp_path / "setting"
    setting.touch()
    assert run(tmp_path, hardened(control("keep", setting), mode="audit"))[0] == 0
    setting.unlink()
    assert run(tmp_path, None, "-Drift", "-Force")[0] == 0
    assert not setting.exists()
    event = read(tmp_path / "pd" / "Patchgate" / "drift-event.json")
    assert event["findings"][0]["status"] == "non-compliant"


def test_drift_respects_interval(tmp_path: Path) -> None:
    setting = tmp_path / "setting"
    assert run(tmp_path, hardened(control("keep", setting)))[0] == 0
    assert run(tmp_path, None, "-Drift")[0] == 0  # first run is due
    setting.unlink()
    assert run(tmp_path, None, "-Drift")[0] == 0  # not due yet
    assert not setting.exists()


def test_drift_without_applied_plan_is_a_no_op(tmp_path: Path) -> None:
    assert run(tmp_path, None, "-Drift")[0] == 0


def utc_hhmm(offset: timedelta) -> str:
    return (datetime.now(UTC) + offset).strftime("%H:%M")


ALL_DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
# Built around the current time so the tests do not depend on when they run.
WINDOW_NOW = {"days": ALL_DAYS, "start": utc_hhmm(timedelta(minutes=-30)), "duration": "2h"}
WINDOW_LATER = {"days": ALL_DAYS, "start": utc_hhmm(timedelta(hours=3)), "duration": "1h"}


def broken_by_hand(tmp_path: Path, **drift: Any) -> tuple[Path, Path]:
    """Applies a plan with one agent and one control, then breaks both."""
    agent, setting = tmp_path / "agent", tmp_path / "setting"
    doc = json.loads(base64.b64decode(hardened(control("keep", setting))))
    doc["steps"] = [
        {
            "agent": "demo",
            "method": "script",
            "script": f'New-Item -ItemType File -Force -Path "{agent}" | Out-Null',
            "validate": f'if (-not (Test-Path "{agent}")) {{ exit 1 }}',
        }
    ]
    doc["drift"] = {"interval": "30m", "mode": "enforce", **drift}
    assert run(tmp_path, base64.b64encode(json.dumps(doc).encode()).decode())[0] == 0
    agent.unlink()
    setting.unlink()
    return agent, setting


def test_drift_mode_per_category(tmp_path: Path) -> None:
    agent, setting = broken_by_hand(tmp_path, hardening="audit")
    assert run(tmp_path, None, "-Drift")[0] == 0
    assert agent.exists() and not setting.exists()
    event = read(tmp_path / "pd" / "Patchgate" / "drift-event.json")
    assert event["mode"] == "agents=enforce, apps=enforce, hardening=audit"
    assert event["modes"] == {"agents": "enforce", "apps": "enforce", "hardening": "audit"}
    assert {f["item"]: f["status"] for f in event["findings"]} == {
        "agent:demo": "remediated",
        "control:keep": "non-compliant",
    }


def test_outside_window_only_reports(tmp_path: Path) -> None:
    agent, setting = broken_by_hand(tmp_path, window=WINDOW_LATER)
    assert run(tmp_path, None, "-Drift")[0] == 0
    assert not agent.exists() and not setting.exists()
    event = read(tmp_path / "pd" / "Patchgate" / "drift-event.json")
    assert (event["window"], event["trigger"]) == ("outside", "schedule")
    assert event["mode"] == "audit (outside maintenance window)"


def test_inside_window_corrects_even_before_the_interval(tmp_path: Path) -> None:
    agent, setting = broken_by_hand(tmp_path, interval="6h", window=WINDOW_NOW)
    state_file = tmp_path / "pd" / "Patchgate" / "state.json"
    state = json.loads(state_file.read_text(encoding="utf-8-sig"))
    state["lastDriftAt"] = datetime.now(UTC).timestamp() - 60  # audit run a minute ago
    state_file.write_text(json.dumps(state), encoding="utf-8")
    code, state = run(tmp_path, None, "-Drift")
    assert code == 0
    assert agent.exists() and setting.exists()
    assert "lastWindowDriftAt" in state
    assert read(tmp_path / "pd" / "Patchgate" / "drift-event.json")["window"] == "inside"


def test_reconcile_ignores_window_and_interval(tmp_path: Path) -> None:
    agent, setting = broken_by_hand(tmp_path, interval="off", window=WINDOW_LATER)
    assert run(tmp_path, None, "-Drift", "-Force")[0] == 0
    assert agent.exists() and setting.exists()
    event = read(tmp_path / "pd" / "Patchgate" / "drift-event.json")
    assert event["trigger"] == "reconcile"
    assert event["mode"] == "enforce (reconcile, maintenance window ignored)"


# ---------------------------------------------------------------- applications

FAKE_CHOCO = Path(__file__).resolve().parent / "fixtures" / "fake_choco.ps1"


def run_apps(
    tmp_path: Path, encoded: str | None, *extra: str, broken: str = ""
) -> tuple[int, dict[str, Any], dict[str, str]]:
    db = tmp_path / "choco.json"
    os.environ["PATCHGATE_CHOCO_EXE"] = str(FAKE_CHOCO)
    os.environ["PATCHGATE_FAKE_CHOCO_DB"] = str(db)
    os.environ["PATCHGATE_FAKE_CHOCO_BROKEN"] = broken
    try:
        code, state = run(tmp_path, encoded, *extra)
    finally:
        for key in (
            "PATCHGATE_CHOCO_EXE",
            "PATCHGATE_FAKE_CHOCO_DB",
            "PATCHGATE_FAKE_CHOCO_BROKEN",
        ):
            os.environ.pop(key, None)
    packages = json.loads(db.read_text()) if db.exists() else {}
    return code, state, packages


def with_apps(*apps: dict[str, Any], digest: str = "sha256:apps") -> str:
    doc = json.loads(base64.b64decode(plan(digest=digest)))
    doc["apps"] = list(apps)
    return base64.b64encode(json.dumps(doc).encode()).decode()


def test_apps_installed_pinned_and_pruned(tmp_path: Path) -> None:
    dotnet = {
        "name": "dotnet-8.0-runtime",
        "version": "8.0.34",
        "source": "appset:dotnet8",
        "prune": True,
    }
    seven = {"name": "7zip", "source": "profile", "prune": False}
    code, state, packages = run_apps(tmp_path, with_apps(dotnet, seven))
    assert code == 0
    assert packages == {"dotnet-8.0-runtime": "8.0.34", "7zip": "9.9"}
    assert set(state["managedApps"]) == {"dotnet-8.0-runtime", "7zip"}

    # The dotnet8 set is removed from the instance: pruned; the profile app stays.
    code, state, packages = run_apps(tmp_path, with_apps(seven, digest="sha256:v2"))
    assert code == 0
    assert packages == {"7zip": "9.9"}
    assert set(state["managedApps"]) == {"7zip"}


def test_app_version_change_and_absent(tmp_path: Path) -> None:
    (tmp_path / "choco.json").write_text(json.dumps({"nodejs-lts": "20.0.0", "putty": "0.80"}))
    code, _, packages = run_apps(
        tmp_path,
        with_apps(
            {
                "name": "nodejs-lts",
                "version": "26.13.1",
                "source": "appset:nodejs-lts",
                "prune": True,
            },
            {"name": "putty", "ensure": "absent", "source": "profile", "prune": False},
        ),
    )
    assert code == 0
    assert packages == {"nodejs-lts": "26.13.1"}


def test_unmanaged_software_is_never_pruned(tmp_path: Path) -> None:
    (tmp_path / "choco.json").write_text(json.dumps({"git": "2.40.0"}))  # installed by someone else
    app = {"name": "git", "source": "appset:devtools", "prune": True}
    run_apps(tmp_path, with_apps(app))
    _, _, packages = run_apps(tmp_path, with_apps(digest="sha256:v2"))
    assert packages == {"git": "2.40.0"}


def test_failed_app_fails_bootstrap(tmp_path: Path) -> None:
    app = {"name": "broken-app", "source": "profile", "prune": False}
    code, state, _ = run_apps(tmp_path, with_apps(app), broken="broken-app")
    assert code == 1
    assert "broken-app" in state["error"]


def test_drift_reinstalls_app_removed_by_hand(tmp_path: Path) -> None:
    app = {
        "name": "dotnet-8.0-runtime",
        "version": "8.0.34",
        "source": "appset:dotnet8",
        "prune": True,
    }
    assert run_apps(tmp_path, with_apps(app))[0] == 0
    (tmp_path / "choco.json").write_text("{}")  # someone uninstalls it
    code, _, packages = run_apps(tmp_path, None, "-Drift", "-Force")
    assert code == 0
    assert packages == {"dotnet-8.0-runtime": "8.0.34"}
    event = read(tmp_path / "pd" / "Patchgate" / "drift-event.json")
    assert {"item": "app:dotnet-8.0-runtime", "status": "remediated"} in event["findings"]


def test_legacy_chocolatey_field_still_works(tmp_path: Path) -> None:
    doc = json.loads(base64.b64decode(plan()))
    doc["chocolatey"] = [{"name": "7zip"}]
    code, _, packages = run_apps(tmp_path, base64.b64encode(json.dumps(doc).encode()).decode())
    assert code == 0 and packages == {"7zip": "9.9"}


def test_busy_windows_installer_is_retried(tmp_path: Path) -> None:
    """Exit 1618 right after boot (seen in the lab) must be retried, not fail the bootstrap."""
    (tmp_path / "busy.txt").write_text("2")
    app = {"name": "7zip", "source": "profile", "prune": False}
    code, _, packages = run_apps(tmp_path, with_apps(app))
    assert code == 0 and packages == {"7zip": "9.9"}
