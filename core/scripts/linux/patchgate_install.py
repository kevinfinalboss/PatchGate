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
STATE_FILE = Path("/var/lib/patchgate/state.json")
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
    if plan.get("hardening", "none") != "none":
        log.warning("hardening level %s not implemented yet; skipped", plan["hardening"])
    return {"profile": plan["profile"], "digest": plan["digest"], "steps": results}


# ---------------------------------------------------------------- entrypoint


def load_plan(args: argparse.Namespace) -> dict[str, Any]:
    raw = (
        base64.b64decode(args.plan_b64, validate=True)
        if args.plan_b64
        else args.plan_file.read_bytes()
    )
    plan = json.loads(raw)
    if not isinstance(plan, dict):
        raise ValueError("plan must be a JSON object")
    return plan


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--plan-b64", help="install plan as base64-encoded JSON")
    source.add_argument("--plan-file", type=Path, help="install plan JSON file")
    parser.add_argument("--force", action="store_true", help="re-run even if digest matches")
    args = parser.parse_args(argv)

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(LOG_DIR / "install.log")],
    )
    try:
        plan = load_plan(args)
    except (ValueError, OSError) as exc:
        log.error("invalid plan: %s", exc)
        return 2

    if not args.force and STATE_FILE.exists():
        state = json.loads(STATE_FILE.read_text())
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
        _write_state({"digest": plan.get("digest"), "status": "failed", "error": str(exc)})
        return 1
    _write_state({"digest": plan["digest"], "status": "success", "result": result})
    log.info("SUCCESS: profile %s applied", plan["profile"])
    print(json.dumps({"patchgate": result}))
    return 0


def _write_state(state: dict[str, Any]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    state["updatedAt"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    STATE_FILE.write_text(json.dumps(state, indent=2))


if __name__ == "__main__":
    sys.exit(main())
