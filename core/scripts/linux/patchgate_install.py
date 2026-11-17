#!/usr/bin/env python3
"""Patchgate install-plan executor for Linux.

Runs inside the VM as root. Standard library only, so it works on any distribution
with Python 3.9+ (Amazon Linux 2023, RHEL 9, Ubuntu 22.04+). It never calls a cloud API:
everything it needs is in the install plan produced by `python -m patchgate compile`.

Exit codes: 0 success, 1 failure, 2 invalid input.
"""

from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any

LOG_DIR = Path("/var/log/patchgate")
STATE_DIR = Path("/var/lib/patchgate")
STATE_FILE = STATE_DIR / "state.json"
PLAN_FILE = STATE_DIR / "plan.json"  # last applied plan, re-used by drift runs
COMPLIANCE_FILE = STATE_DIR / "compliance.json"  # evidence: last result of every control
DRIFT_EVENT_FILE = STATE_DIR / "drift-event.json"  # picked up and published by the provider
DRIFT_INTERVALS = {"30m": 1800, "1h": 3600, "2h": 7200, "6h": 21600, "12h": 43200, "24h": 86400}
DRIFT_SLACK_SECONDS = 120  # schedulers fire slightly early/late
DOWNLOAD_TIMEOUT = 300
VALIDATE_ATTEMPTS = 5
VALIDATE_DELAY_SECONDS = 6

log = logging.getLogger("patchgate")


class InstallError(RuntimeError):
    pass


# ---------------------------------------------------------------- helpers


def run(cmd: list[str], *, env: dict[str, str] | None = None) -> None:
    log.info("$ %s", " ".join(cmd))
    proc = subprocess.run(  # noqa: S603 - commands are built from a reviewed catalog
        cmd, env=env, capture_output=True, text=True, check=False
    )
    for line in (proc.stdout + proc.stderr).splitlines():
        log.info("  %s", line)
    if proc.returncode != 0:
        raise InstallError(f"command failed ({proc.returncode}): {' '.join(cmd)}")


def run_script(script: str, label: str) -> None:
    with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as handle:
        handle.write(script)
        path = handle.name
    try:
        run(["/bin/bash", "-euo", "pipefail", path])
    except InstallError as exc:
        raise InstallError(f"{label} failed") from exc
    finally:
        os.unlink(path)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download(url: str, expected_sha256: str, dest: Path) -> Path:
    if not url.startswith("https://"):
        raise InstallError(f"refusing non-https source: {url}")
    log.info("downloading %s", url)
    with urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT) as response, dest.open("wb") as out:  # noqa: S310
        shutil.copyfileobj(response, out)
    actual = sha256_file(dest)
    if actual != expected_sha256:
        dest.unlink(missing_ok=True)
        raise InstallError(f"checksum mismatch for {url}: expected {expected_sha256}, got {actual}")
    log.info("checksum OK (%s)", actual)
    return dest


def detect_package_manager() -> str:
    for manager in ("dnf", "yum", "apt-get"):
        if shutil.which(manager):
            return manager
    raise InstallError("no supported package manager found (dnf, yum, apt-get)")


def packages_for(spec: dict[str, Any], manager: str) -> list[str]:
    packages: dict[str, list[str]] = spec["packages"]
    key = "apt" if manager == "apt-get" else "dnf"
    if key not in packages:
        raise InstallError(f"agent has no package list for {key}")
    return packages[key]


def install_packages(manager: str, names: list[str]) -> None:
    if manager == "apt-get":
        env = {**os.environ, "DEBIAN_FRONTEND": "noninteractive"}
        run(["apt-get", "update", "-q"], env=env)
        run(["apt-get", "install", "-y", "-q", *names], env=env)
    else:
        run([manager, "install", "-y", "-q", *names])


# ---------------------------------------------------------------- steps


def install_step(step: dict[str, Any], manager: str, workdir: Path) -> None:
    method = step["method"]
    if method == "package":
        install_packages(manager, packages_for(step, manager))
    elif method in ("rpm", "deb"):
        target = download(step["source"], step["sha256"], workdir / f"{step['agent']}.{method}")
        if method == "deb" and manager != "apt-get":
            raise InstallError(".deb packages require apt-get")
        if method == "rpm" and manager == "apt-get":
            raise InstallError(".rpm packages require dnf/yum")
        install_packages(manager, [str(target)])
    elif method == "script":
        run_script(step["script"], f"{step['agent']} script")
    else:
        raise InstallError(f"unsupported method on linux: {method}")
    if step.get("postInstall"):
        run_script(step["postInstall"], f"{step['agent']} postInstall")


def validate_step(step: dict[str, Any]) -> None:
    command = step.get("validate")
    if not command:
        return
    for attempt in range(1, VALIDATE_ATTEMPTS + 1):
        proc = subprocess.run(  # noqa: S603
            ["/bin/bash", "-c", command], capture_output=True, text=True, check=False
        )
        if proc.returncode == 0:
            log.info("validate OK: %s", step["agent"])
            return
        log.info("validate attempt %d/%d failed for %s", attempt, VALIDATE_ATTEMPTS, step["agent"])
        time.sleep(VALIDATE_DELAY_SECONDS)
    raise InstallError(f"validation failed for {step['agent']}: {command}")


def run_quiet(command: str) -> bool:
    proc = subprocess.run(  # noqa: S603
        ["/bin/bash", "-c", command], capture_output=True, text=True, check=False
    )
    return proc.returncode == 0


def apply_controls(hardening: dict[str, Any] | None, mode: str) -> list[dict[str, str]]:
    """Run the 'controls' engine. Returns one result per control.

    Status: compliant | remediated | non-compliant (audit) | failed (remediation did not fix it).
    """
    if not isinstance(hardening, dict):  # absent, or "none" in plans from older catalogs
        return []
    if hardening.get("engine") != "controls":
        raise InstallError(f"unsupported hardening engine: {hardening.get('engine')}")
    results = []
    for control in hardening["controls"]:
        name = control["name"]
        if run_quiet(control["check"]):
            status = "compliant"
        elif mode == "audit":
            status = "non-compliant"
        else:
            try:
                run_script(control["remediate"], f"control {name}")
            except InstallError as exc:
                log.error("%s", exc)
            status = "remediated" if run_quiet(control["check"]) else "failed"
        log.info("control %-32s %s", name, status)
        results.append({"control": name, "cis": control.get("cis", ""), "status": status})
    write_json(
        COMPLIANCE_FILE,
        {
            "level": hardening.get("level"),
            "mode": mode,
            "checkedAt": now_iso(),
            "controls": results,
        },
    )
    return results


def execute(plan: dict[str, Any]) -> dict[str, Any]:
    if plan.get("kind") != "InstallPlan" or plan.get("platform") != "linux":
        raise ValueError("not a linux InstallPlan")
    manager = detect_package_manager()
    results: list[dict[str, str]] = []
    with tempfile.TemporaryDirectory(prefix="patchgate-") as tmp:
        for step in plan["steps"]:
            log.info("== %s (%s)", step["agent"], step["method"])
            install_step(step, manager, Path(tmp))
            validate_step(step)
            results.append({"agent": step["agent"], "status": "ok"})
    controls = apply_controls(plan.get("hardening"), "enforce")
    failed = [c["control"] for c in controls if c["status"] == "failed"]
    if failed:
        raise InstallError(f"hardening controls could not be remediated: {', '.join(failed)}")
    return {
        "profile": plan["profile"],
        "digest": plan["digest"],
        "steps": results,
        "hardening": summarize(controls),
    }


def summarize(controls: list[dict[str, str]]) -> dict[str, int]:
    summary: dict[str, int] = {}
    for control in controls:
        summary[control["status"]] = summary.get(control["status"], 0) + 1
    return summary


# ---------------------------------------------------------------- drift


def drift_due(interval: str, last_run: float | None, now: float) -> bool:
    if interval == "off":
        return False
    if last_run is None:
        return True
    return now - last_run >= DRIFT_INTERVALS[interval] - DRIFT_SLACK_SECONDS


def run_drift(force: bool = False) -> int:
    """Re-validate agents and hardening from the last applied plan. Runs on a schedule."""
    if not PLAN_FILE.exists():
        log.info("no applied plan yet; nothing to check")
        return 0
    plan = json.loads(PLAN_FILE.read_text())
    drift = plan.get("drift", {})
    interval, mode = drift.get("interval", "30m"), drift.get("mode", "enforce")
    state = read_state()
    if not force and not drift_due(interval, state.get("lastDriftAt"), time.time()):
        log.info("drift check not due (interval %s)", interval)
        return 0

    log.info("== drift check (interval %s, mode %s)", interval, mode)
    findings: list[dict[str, str]] = []
    manager = detect_package_manager()
    with tempfile.TemporaryDirectory(prefix="patchgate-") as tmp:
        for step in plan["steps"]:
            if not step.get("validate") or run_quiet(step["validate"]):
                continue
            if mode == "audit":
                findings.append({"item": f"agent:{step['agent']}", "status": "non-compliant"})
                continue
            try:
                install_step(step, manager, Path(tmp))
                validate_step(step)
                findings.append({"item": f"agent:{step['agent']}", "status": "remediated"})
            except InstallError as exc:
                log.error("%s", exc)
                findings.append({"item": f"agent:{step['agent']}", "status": "failed"})
    for result in apply_controls(plan.get("hardening"), mode):
        if result["status"] != "compliant":
            findings.append({"item": f"control:{result['control']}", "status": result["status"]})

    state["lastDriftAt"] = time.time()
    write_json(STATE_FILE, state)
    if findings:
        event = {
            "profile": plan["profile"],
            "digest": plan["digest"],
            "mode": mode,
            "findings": findings,
            "detectedAt": now_iso(),
        }
        write_json(DRIFT_EVENT_FILE, event)
        log.warning("PATCHGATE_DRIFT %s", json.dumps(findings))
    else:
        log.info("no drift detected")
    return 1 if any(f["status"] == "failed" for f in findings) else 0


# ---------------------------------------------------------------- entrypoint


def decode_plan(encoded: str) -> bytes:
    """Accept "gz1:" + base64(gzip(json)) (what Patchgate sends) or plain base64 JSON."""
    if encoded.startswith("gz1:"):
        return gzip.decompress(base64.b64decode(encoded[4:], validate=True))
    return base64.b64decode(encoded, validate=True)


def load_plan(args: argparse.Namespace) -> dict[str, Any]:
    raw = decode_plan(args.plan_b64) if args.plan_b64 else args.plan_file.read_bytes()
    plan = json.loads(raw)
    if not isinstance(plan, dict):
        raise ValueError("plan must be a JSON object")
    return plan


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--plan-b64", help="install plan: gz1:<base64 gzip JSON> or base64 JSON")
    source.add_argument("--plan-file", type=Path, help="install plan JSON file")
    source.add_argument("--drift", action="store_true", help="re-check the last applied plan")
    parser.add_argument("--force", action="store_true", help="ignore digest/interval and run now")
    args = parser.parse_args(argv)

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(LOG_DIR / "install.log")],
    )
    if args.drift:
        return run_drift(force=args.force)
    try:
        plan = load_plan(args)
    except (ValueError, OSError) as exc:
        log.error("invalid plan: %s", exc)
        return 2

    if not args.force:
        state = read_state()
        if state.get("digest") == plan.get("digest") and state.get("status") == "success":
            log.info("plan %s already applied; validating only", plan.get("digest"))
            try:
                for step in plan["steps"]:
                    validate_step(step)
                return 0
            except InstallError:
                log.warning("validation failed; re-applying plan")

    try:
        result = execute(plan)
    except (InstallError, ValueError) as exc:
        log.error("FAILED: %s", exc)
        update_state(digest=plan.get("digest"), status="failed", error=str(exc))
        return 1
    write_json(PLAN_FILE, plan)
    update_state(digest=plan["digest"], status="success", result=result, error=None)
    log.info("SUCCESS: profile %s applied", plan["profile"])
    print(json.dumps({"patchgate": result}))
    return 0


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)


def read_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {}
    state: dict[str, Any] = json.loads(STATE_FILE.read_text())
    return state


def update_state(**fields: Any) -> None:
    state = read_state()
    state.update({k: v for k, v in fields.items() if v is not None})
    for key in [k for k, v in fields.items() if v is None]:
        state.pop(key, None)
    state["updatedAt"] = now_iso()
    write_json(STATE_FILE, state)


if __name__ == "__main__":
    sys.exit(main())
