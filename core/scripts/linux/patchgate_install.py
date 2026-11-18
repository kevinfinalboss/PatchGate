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
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from datetime import datetime, timedelta, timezone
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
DRIFT_CATEGORIES = ("agents", "apps", "hardening")
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")  # datetime.weekday() order

# "ansible" hardening engine (ADR 0007): a pinned runtime in its own venv; modules run on the
# system Python, which has the distribution's package-manager bindings.
PATCHGATE_HOME = Path("/opt/patchgate")
ANSIBLE_VENV = PATCHGATE_HOME / "ansible"
ANSIBLE_COLLECTIONS = PATCHGATE_HOME / "ansible-collections"
BASELINES_DIR = PATCHGATE_HOME / "baselines"
ANSIBLE_LOCK = Path(__file__).resolve().with_name("ansible-requirements.txt")
ANSIBLE_PYTHONS = ("python3.12", "python3.13", "python3.14", "python3")
# Task names look like "<role path> : 1.1.1.1 | PATCH | Ensure ..." in the JSON report.
CIS_ID = re.compile(r"(?:^|:\s+)\"?(\d+(?:\.\d+)+)\s*\|")
STATUS_RANK = {"compliant": 0, "remediated": 1, "non-compliant": 2, "failed": 3}
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


def apply_hardening(hardening: dict[str, Any] | None, mode: str) -> list[dict[str, str]]:
    """Run the profile's hardening engine and record evidence. Returns one result per control.

    Status: compliant | remediated | non-compliant (audit) | failed (remediation did not fix it).
    """
    if not isinstance(hardening, dict):  # absent, or "none" in plans from older catalogs
        return []
    engine = hardening.get("engine")
    if engine == "controls":
        results = run_controls(hardening, mode)
    elif engine == "ansible":
        results = run_ansible(hardening, mode)
    else:
        raise InstallError(f"unsupported hardening engine: {engine}")
    write_json(
        COMPLIANCE_FILE,
        {
            "engine": engine,
            "level": hardening.get("level") or hardening.get("baseline"),
            "mode": mode,
            "checkedAt": now_iso(),
            "controls": results,
        },
    )
    return results


def run_controls(hardening: dict[str, Any], mode: str) -> list[dict[str, str]]:
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
    return results


# ---------------------------------------------------------------- ansible engine


def python_for_ansible() -> str:
    for candidate in ANSIBLE_PYTHONS:
        path = shutil.which(candidate)
        if not path:
            continue
        version = subprocess.run(  # noqa: S603
            [path, "-c", "import sys; print(sys.version_info >= (3, 12))"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
        if version == "True":
            return path
    install_packages(detect_package_manager(), ["python3.12"])
    path = shutil.which("python3.12")
    if not path:
        raise InstallError("python3.12 is required for the ansible engine")
    return path


def ensure_ansible_runtime() -> Path:
    """Create (once per lock version) a venv with the hash-pinned Ansible runtime."""
    lock_digest = sha256_file(ANSIBLE_LOCK)
    marker = ANSIBLE_VENV / ".patchgate-lock"
    if marker.exists() and marker.read_text().strip() == lock_digest:
        return ANSIBLE_VENV / "bin"
    if ANSIBLE_VENV.exists():
        shutil.rmtree(ANSIBLE_VENV)
    run([python_for_ansible(), "-m", "venv", str(ANSIBLE_VENV)])
    pip = str(ANSIBLE_VENV / "bin" / "pip")
    run([pip, "install", "--quiet", "--require-hashes", "--only-binary", ":all:", "--no-deps",
         "-r", str(ANSIBLE_LOCK)])  # fmt: skip
    marker.write_text(lock_digest)
    return ANSIBLE_VENV / "bin"


def safe_extract(archive: Path, dest: Path) -> None:
    """Extract a tar.gz, rejecting absolute paths, '..', devices and links leaving dest."""
    dest = dest.resolve()
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar.getmembers():
            target = (dest / member.name).resolve()
            if not str(target).startswith(str(dest) + os.sep) and target != dest:
                raise InstallError(f"unsafe path in archive: {member.name}")
            if member.isdev():
                raise InstallError(f"device file in archive: {member.name}")
            if member.issym() or member.islnk():
                base = dest if member.islnk() else target.parent
                link = (base / member.linkname).resolve()
                if not str(link).startswith(str(dest) + os.sep):
                    raise InstallError(f"link escaping archive: {member.name}")
        if hasattr(tarfile, "data_filter"):  # Python 3.12+ and recent 3.9-3.11 patch releases
            tar.extractall(dest, filter="data")
        else:
            tar.extractall(dest)  # noqa: S202 - members validated above


def ensure_baseline(hardening: dict[str, Any], bin_dir: Path, workdir: Path) -> Path:
    """Download and verify the baseline and its collections; returns the playbook path."""
    target = BASELINES_DIR / f"{hardening['baseline']}-{hardening['sha256'][:12]}"
    if not (target / ".patchgate-complete").exists():
        archive = download(hardening["source"], hardening["sha256"], workdir / "baseline.tar.gz")
        unpacked = workdir / "baseline"
        unpacked.mkdir()
        safe_extract(archive, unpacked)
        entries = list(unpacked.iterdir())
        root = entries[0] if len(entries) == 1 and entries[0].is_dir() else unpacked
        if target.exists():
            shutil.rmtree(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(root), str(target))
        for collection in hardening.get("collections", []):
            tarball = download(
                collection["source"], collection["sha256"], workdir / f"{collection['name']}.tar.gz"
            )
            run([str(bin_dir / "ansible-galaxy"), "collection", "install", "--no-deps", "--offline",
                 "--force", "-p", str(ANSIBLE_COLLECTIONS), str(tarball)])  # fmt: skip
        (target / ".patchgate-complete").write_text(now_iso())
    playbook: Path = (target / str(hardening["playbook"])).resolve()
    if not str(playbook).startswith(str(target.resolve()) + os.sep) or not playbook.exists():
        raise InstallError(f"playbook not found in baseline: {hardening['playbook']}")
    return playbook


def parse_ansible_results(report: dict[str, Any], mode: str) -> list[dict[str, str]]:
    """Map ansible.posix.json output to per-CIS-rule results (worst status per rule wins)."""
    rules: dict[str, dict[str, str]] = {}
    for play in report.get("plays", []):
        for task in play.get("tasks", []):
            name = str(task.get("task", {}).get("name", ""))
            match = CIS_ID.search(name)
            if not match:
                continue
            for host in task.get("hosts", {}).values():
                if host.get("skipped"):
                    continue
                if host.get("failed") or host.get("unreachable"):
                    status = "failed"
                elif host.get("changed"):
                    status = "non-compliant" if mode == "audit" else "remediated"
                else:
                    status = "compliant"
                cis = match.group(1)
                current = rules.get(cis)
                if current is None or STATUS_RANK[status] > STATUS_RANK[current["status"]]:
                    rules[cis] = {
                        "control": f"cis-{cis}",
                        "cis": cis,
                        "status": status,
                        "title": name[:160],
                    }
    return [rules[k] for k in sorted(rules, key=lambda c: [int(p) for p in c.split(".")])]


def failed_tasks(report: dict[str, Any]) -> list[str]:
    """Names and messages of failed tasks, including non-rule tasks such as prechecks."""
    failures = []
    for play in report.get("plays", []):
        for task in play.get("tasks", []):
            for host in task.get("hosts", {}).values():
                if host.get("failed") or host.get("unreachable"):
                    message = str(host.get("msg", ""))[:300]
                    failures.append(f"{task.get('task', {}).get('name', '?')}: {message}")
    return failures[:5]


def run_ansible(hardening: dict[str, Any], mode: str) -> list[dict[str, str]]:
    bin_dir = ensure_ansible_runtime()
    with tempfile.TemporaryDirectory(prefix="patchgate-ansible-") as tmp:
        playbook = ensure_baseline(hardening, bin_dir, Path(tmp))
        variables = Path(tmp) / "vars.json"
        variables.write_text(json.dumps(hardening.get("variables", {})))
        command = [str(bin_dir / "ansible-playbook"), "-i", "localhost,", "-c", "local",
                   str(playbook), "-e", f"@{variables}"]  # fmt: skip
        if mode == "audit":
            command.append("--check")
        env = {
            **os.environ,
            "ANSIBLE_COLLECTIONS_PATH": str(ANSIBLE_COLLECTIONS),
            "ANSIBLE_STDOUT_CALLBACK": "ansible.posix.json",
            "ANSIBLE_PYTHON_INTERPRETER": "/usr/bin/python3",
            "ANSIBLE_NOCOLOR": "1",
            "ANSIBLE_RETRY_FILES_ENABLED": "False",
        }
        log.info("$ %s", " ".join(command))
        proc = subprocess.run(  # noqa: S603
            command, env=env, capture_output=True, text=True, check=False, cwd=playbook.parent
        )
    try:
        report = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        tail = (proc.stdout + proc.stderr)[-2000:]
        raise InstallError(
            f"ansible-playbook gave no JSON report (rc={proc.returncode}): {tail}"
        ) from exc
    results = parse_ansible_results(report, mode)
    counts = summarize(results)
    log.info("ansible baseline %s: %s (rc=%d)", hardening["baseline"], counts, proc.returncode)
    for result in results:
        if result["status"] in ("failed", "non-compliant"):
            log.info("rule %-12s %-13s %s", result["cis"], result["status"], result["title"])
    if proc.returncode != 0 and not counts.get("failed"):
        detail = "; ".join(failed_tasks(report)) or proc.stderr[-1500:]
        raise InstallError(f"ansible-playbook failed (rc={proc.returncode}): {detail}")
    return results


# ---------------------------------------------------------------- applications


def installed_version(manager: str, name: str) -> str | None:
    """Installed version of a package ("<version>-<release>" on rpm systems), or None."""
    if manager == "apt-get":
        command = ["dpkg-query", "-W", "-f=${Status}|${Version}", name]
    else:
        command = ["rpm", "-q", "--qf", "installed|%{VERSION}-%{RELEASE}", name]
    proc = subprocess.run(command, capture_output=True, text=True, check=False)  # noqa: S603
    if proc.returncode != 0:
        return None
    status, _, version = proc.stdout.strip().rpartition("|")
    if manager == "apt-get" and status != "install ok installed":
        return None
    return version or None


def version_matches(installed: str, wanted: str) -> bool:
    """'2.40.1' matches an installed '2.40.1-1.amzn2023'; a full version must match exactly."""
    return installed == wanted or installed.split("-")[0] == wanted


def package_spec(manager: str, name: str, version: str | None) -> str:
    if not version:
        return name
    return f"{name}={version}" if manager == "apt-get" else f"{name}-{version}"


def change_app(manager: str, action: str, name: str, version: str | None = None) -> None:
    env = {**os.environ, "DEBIAN_FRONTEND": "noninteractive"}
    spec = package_spec(manager, name, version)
    if manager == "apt-get":
        commands = {
            "install": ["apt-get", "install", "-y", "-q", "--allow-downgrades", spec],
            "upgrade": ["apt-get", "install", "-y", "-q", "--only-upgrade", name],
            "remove": ["apt-get", "remove", "-y", "-q", name],
        }
        if action != "remove":
            run(["apt-get", "update", "-q"], env=env)
    else:
        commands = {
            "install": [manager, "install", "-y", "-q", spec],
            "upgrade": [manager, "upgrade", "-y", "-q", name],
            "remove": [manager, "remove", "-y", "-q", name],
        }
    try:
        run(commands[action], env=env)
    except InstallError:
        if action == "install" and version and manager != "apt-get":
            run([manager, "downgrade", "-y", "-q", spec], env=env)  # pinned to an older version
        else:
            raise


def reconcile_apps(plan: dict[str, Any], mode: str) -> list[dict[str, str]]:
    """Bring applications to the plan's desired state; prune what Patchgate installed and no
    longer wants. Software Patchgate did not install is never removed by pruning."""
    desired: list[dict[str, Any]] = plan.get("apps") or []
    if not desired and not read_state().get("managedApps"):
        return []
    manager = detect_package_manager()
    state = read_state()
    managed: dict[str, dict[str, Any]] = state.get("managedApps", {})
    results: list[dict[str, str]] = []

    def record(name: str, status: str, detail: str) -> None:
        log.info("app %-32s %-13s %s", name, status, detail)
        results.append({"item": f"app:{name}", "status": status, "detail": detail})

    wanted_names = {app["name"] for app in desired}
    for app in desired:
        name, ensure, version = app["name"], app.get("ensure", "present"), app.get("version")
        current = installed_version(manager, name)
        if ensure == "absent":
            action = "remove" if current else None
        elif current is None:
            action = "install"
        elif ensure == "latest":
            action = "upgrade"
        elif version and not version_matches(current, version):
            action = "install"
        else:
            action = None

        if action is None:
            record(name, "compliant", current or "absent")
        elif mode == "audit":
            record(name, "non-compliant", f"would {action} (installed: {current or 'none'})")
        elif action == "upgrade":
            try:
                change_app(manager, "upgrade", name)
                after = installed_version(manager, name)
                record(name, "remediated" if after != current else "compliant", after or "?")
            except InstallError as exc:
                record(name, "failed", str(exc))
        else:
            try:
                change_app(manager, action, name, version if action == "install" else None)
                after = installed_version(manager, name)
                ok = (
                    after is None
                    if action == "remove"
                    else after is not None and (not version or version_matches(after, version))
                )
                record(name, "remediated" if ok else "failed", f"{action}: {current} -> {after}")
            except InstallError as exc:
                record(name, "failed", str(exc))

        if ensure == "absent":
            managed.pop(name, None)
        elif mode != "audit" and (action == "install" or name in managed):
            managed[name] = {"source": app.get("source", ""), "prune": app.get("prune", False)}

    for name, info in sorted(managed.items()):
        if name in wanted_names or not info.get("prune"):
            continue
        if mode == "audit":
            record(name, "non-compliant", f"would prune (from {info.get('source')})")
            continue
        try:
            if installed_version(manager, name):
                change_app(manager, "remove", name)
            record(name, "remediated", f"pruned (was from {info.get('source')})")
            managed.pop(name)
        except InstallError as exc:
            record(name, "failed", f"prune: {exc}")

    if mode != "audit":
        update_state(managedApps=managed)
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
    apps = reconcile_apps(plan, "enforce")
    failed_apps = [a["item"] for a in apps if a["status"] == "failed"]
    if failed_apps:
        raise InstallError(f"applications could not be reconciled: {', '.join(failed_apps)}")
    controls = apply_hardening(plan.get("hardening"), "enforce")
    failed = [c["control"] for c in controls if c["status"] == "failed"]
    if failed:
        raise InstallError(f"hardening controls could not be remediated: {', '.join(failed)}")
    return {
        "profile": plan["profile"],
        "digest": plan["digest"],
        "steps": results,
        "apps": summarize([{"status": a["status"]} for a in apps]),
        "hardening": summarize(controls),
    }


def summarize(controls: list[dict[str, str]]) -> dict[str, int]:
    summary: dict[str, int] = {}
    for control in controls:
        summary[control["status"]] = summary.get(control["status"], 0) + 1
    return summary


# ---------------------------------------------------------------- drift


def window_start(window: dict[str, Any], now: float) -> float | None:
    """Start (epoch seconds) of the maintenance window occurrence that contains `now`, or None."""
    offset = window.get("utcOffset", "+00:00")
    sign = -1 if offset.startswith("-") else 1
    hours, minutes = (int(part) for part in offset[1:].split(":"))
    local = datetime.fromtimestamp(now, timezone(sign * timedelta(hours=hours, minutes=minutes)))
    start_hour, start_minute = (int(part) for part in window["start"].split(":"))
    length = timedelta(hours=int(window["duration"].rstrip("h")))
    for days_back in (0, 1):  # an occurrence that started yesterday may cross midnight
        day = local - timedelta(days=days_back)
        if WEEKDAYS[day.weekday()] not in window["days"]:
            continue
        start = day.replace(hour=start_hour, minute=start_minute, second=0, microsecond=0)
        if start <= local < start + length:
            return start.timestamp()
    return None


def drift_due(
    interval: str,
    last_run: float | None,
    now: float,
    window_opened: float | None = None,
    last_in_window: float | None = None,
) -> bool:
    if interval == "off":
        return False
    if window_opened is not None and (last_in_window is None or last_in_window < window_opened):
        return True  # first run of each window occurrence corrects what audit runs reported
    if last_run is None:
        return True
    return now - last_run >= DRIFT_INTERVALS[interval] - DRIFT_SLACK_SECONDS


def drift_modes(drift: dict[str, Any], outside_window: bool) -> dict[str, str]:
    """Effective mode per category: the profile's choice, or audit outside the window."""
    default = drift.get("mode", "enforce")
    return {c: "audit" if outside_window else drift.get(c, default) for c in DRIFT_CATEGORIES}


def describe_modes(modes: dict[str, str], note: str = "") -> str:
    """Human-readable summary for logs and emails, e.g. 'agents=enforce, apps=enforce, ...'."""
    values = set(modes.values())
    text = values.pop() if len(values) == 1 else ", ".join(f"{c}={m}" for c, m in modes.items())
    return f"{text} ({note})" if note else text


def run_drift(force: bool = False) -> int:
    """Re-validate agents, apps and hardening from the last applied plan. Runs on a schedule.

    `force` (on-demand reconcile) ignores both the interval and the maintenance window.
    """
    if not PLAN_FILE.exists():
        log.info("no applied plan yet; nothing to check")
        return 0
    plan = json.loads(PLAN_FILE.read_text())
    drift = plan.get("drift", {})
    interval = drift.get("interval", "30m")
    window = drift.get("window")
    now = time.time()
    opened = window_start(window, now) if window else None
    outside = bool(window) and opened is None
    state = read_state()
    due = drift_due(interval, state.get("lastDriftAt"), now, opened, state.get("lastWindowDriftAt"))
    if not force and not due:
        log.info("drift check not due (interval %s)", interval)
        return 0
    modes = drift_modes(drift, outside and not force)
    note = ""
    if outside:
        note = "reconcile, maintenance window ignored" if force else "outside maintenance window"
    mode = describe_modes(modes, note)

    log.info("== drift check (interval %s, mode %s)", interval, mode)
    findings: list[dict[str, str]] = []
    manager = detect_package_manager()
    with tempfile.TemporaryDirectory(prefix="patchgate-") as tmp:
        for step in plan["steps"]:
            if not step.get("validate") or run_quiet(step["validate"]):
                continue
            if modes["agents"] == "audit":
                findings.append({"item": f"agent:{step['agent']}", "status": "non-compliant"})
                continue
            try:
                install_step(step, manager, Path(tmp))
                validate_step(step)
                findings.append({"item": f"agent:{step['agent']}", "status": "remediated"})
            except InstallError as exc:
                log.error("%s", exc)
                findings.append({"item": f"agent:{step['agent']}", "status": "failed"})
    for app in reconcile_apps(plan, modes["apps"]):
        if app["status"] != "compliant":
            findings.append({"item": app["item"], "status": app["status"]})
    for result in apply_hardening(plan.get("hardening"), modes["hardening"]):
        if result["status"] != "compliant":
            findings.append({"item": f"control:{result['control']}", "status": result["status"]})

    state["lastDriftAt"] = now
    if opened is not None:
        state["lastWindowDriftAt"] = now
    write_json(STATE_FILE, state)
    if findings:
        event = {
            "profile": plan["profile"],
            "digest": plan["digest"],
            "mode": mode,
            "modes": modes,
            "trigger": "reconcile" if force else "schedule",
            "findings": findings,
            "detectedAt": now_iso(),
        }
        if window:
            event["window"] = "outside" if outside else "inside"
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
