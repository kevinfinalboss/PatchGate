"""Compose install plans from catalog documents (standard library only).

Used by the CLI/CI (documents loaded from YAML) and by the cloud resolver (documents loaded from
the configuration store at run time), so both produce byte-identical plans for the same inputs.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

API_VERSION = "patchgate.io/v1alpha1"
# Plans travel as a single command parameter: gzip + base64 with a format prefix.
# Keep the encoded size well under common provider parameter limits (4 KB).
PLAN_PREFIX = "gz1:"
MAX_ENCODED_PLAN_CHARS = 3_800

KINDS = ("Agent", "Profile", "Control", "Baseline", "AppSet")
# Hardening levels are cumulative: a profile at level N gets every control with level <= N.
LEVELS = {"baseline": 0, "cis-level1": 1}
DEFAULT_DRIFT = {"interval": "30m", "mode": "enforce"}
DRIFT_CATEGORIES = ("agents", "apps", "hardening")
DEFAULT_UTC_OFFSET = "+00:00"
BASELINE_ENGINES = ("ansible", "dsc")  # engines that run a Baseline instead of Control documents

Document = dict[str, Any]


class CatalogError(ValueError):
    """Raised when documents are inconsistent or a plan cannot be compiled."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("\n".join(errors))


@dataclass
class Library:
    """Catalog documents indexed by kind and name."""

    agents: dict[str, Document] = field(default_factory=dict)
    profiles: dict[str, Document] = field(default_factory=dict)
    controls: dict[str, Document] = field(default_factory=dict)
    baselines: dict[str, Document] = field(default_factory=dict)
    appsets: dict[str, Document] = field(default_factory=dict)

    def bucket(self, kind: str) -> dict[str, Document]:
        return {
            "Agent": self.agents,
            "Profile": self.profiles,
            "Control": self.controls,
            "Baseline": self.baselines,
            "AppSet": self.appsets,
        }[kind]

    def add(self, doc: Document) -> None:
        self.bucket(doc["kind"])[doc["metadata"]["name"]] = doc

    # ------------------------------------------------------------ consistency

    def reference_errors(self) -> list[str]:
        """Cross-document problems (unknown references, platform/engine mismatches)."""
        errors: list[str] = []
        for name, profile in self.profiles.items():
            spec = profile["spec"]
            platform = spec["platform"]
            for agent_name in spec.get("agents", []):
                agent = self.agents.get(agent_name)
                if agent is None:
                    errors.append(f"profile '{name}': unknown agent '{agent_name}'")
                elif platform not in agent["spec"]["install"]:
                    errors.append(
                        f"profile '{name}': agent '{agent_name}' does not support {platform}"
                    )
            hardening = spec.get("hardening", {})
            baseline_name = hardening.get("baseline")
            if baseline_name:
                baseline = self.baselines.get(baseline_name)
                if baseline is None:
                    errors.append(f"profile '{name}': unknown baseline '{baseline_name}'")
                elif baseline["spec"]["platform"] != platform:
                    errors.append(
                        f"profile '{name}': baseline '{baseline_name}' is not for {platform}"
                    )
                elif baseline["spec"]["engine"] != hardening["engine"]:
                    errors.append(
                        f"profile '{name}': baseline '{baseline_name}' needs engine "
                        f"'{baseline['spec']['engine']}'"
                    )
            for control_name in hardening.get("exclude", []):
                control = self.controls.get(control_name)
                if control is None:
                    errors.append(f"profile '{name}': unknown control '{control_name}' in exclude")
                elif control["spec"]["platform"] != platform:
                    errors.append(
                        f"profile '{name}': control '{control_name}' is not for {platform}"
                    )
            for set_name in spec.get("allowedAppSets", []):
                appset = self.appsets.get(set_name)
                if appset is None:
                    errors.append(f"profile '{name}': unknown app set '{set_name}'")
                elif appset["spec"]["platform"] != platform:
                    errors.append(f"profile '{name}': app set '{set_name}' is not for {platform}")
        return errors

    # ------------------------------------------------------------ compilation

    def compile(self, profile_name: str, appsets: Iterable[str] = ()) -> Document:
        """Build the install plan for a profile plus the app sets an instance asked for."""
        profile = self.profiles.get(profile_name)
        if profile is None:
            raise CatalogError([f"unknown profile '{profile_name}'"])
        spec = profile["spec"]
        platform = spec["platform"]
        requested = _unique(appsets)

        steps = []
        for agent_name in spec.get("agents", []):
            agent_spec = self.agents[agent_name]["spec"]
            step: Document = {"agent": agent_name, **agent_spec["install"][platform]}
            validate_cmd = agent_spec.get("validate", {}).get(platform)
            if validate_cmd:
                step["validate"] = validate_cmd
            steps.append(step)

        body: Document = {
            "apiVersion": API_VERSION,
            "kind": "InstallPlan",
            "profile": profile_name,
            "platform": platform,
            "ring": spec.get("ring", 0),
            "steps": steps,
            "appSets": requested,
            "apps": self._apps(profile_name, spec, requested),
            "hardening": self._hardening(spec),
            "drift": drift_settings(spec.get("drift", {})),
        }
        body["digest"] = digest(body)
        return body

    def _apps(self, profile_name: str, spec: Document, requested: list[str]) -> list[Document]:
        errors: list[str] = []
        allowed = set(spec.get("allowedAppSets", []))
        sources: list[tuple[str, bool, list[Document]]] = [("profile", False, spec.get("apps", []))]
        for set_name in requested:
            appset = self.appsets.get(set_name)
            if appset is None:
                errors.append(f"unknown app set '{set_name}'")
            elif set_name not in allowed:
                errors.append(f"app set '{set_name}' is not allowed by profile '{profile_name}'")
            elif appset["spec"]["platform"] != spec["platform"]:
                errors.append(f"app set '{set_name}' is not for {spec['platform']}")
            else:
                sources.append(
                    (
                        f"appset:{set_name}",
                        appset["spec"].get("prune", True),
                        appset["spec"]["apps"],
                    )
                )
        merged: dict[str, Document] = {}
        for source, prune, apps in sources:
            for app in apps:
                entry = {
                    "name": app["name"],
                    "ensure": app.get("ensure", "present"),
                    "source": source,
                    "prune": prune,
                }
                if app.get("version"):
                    entry["version"] = app["version"]
                current = merged.get(app["name"])
                if current is None:
                    merged[app["name"]] = entry
                elif (current["ensure"], current.get("version")) != (
                    entry["ensure"],
                    entry.get("version"),
                ):
                    errors.append(
                        f"app '{app['name']}' conflicts between {current['source']} "
                        f"({current['ensure']} {current.get('version', '')}) and {source} "
                        f"({entry['ensure']} {entry.get('version', '')})"
                    )
        if errors:
            raise CatalogError(errors)
        return list(merged.values())

    def _hardening(self, spec: Document) -> Document | None:
        hardening = spec.get("hardening")
        if not hardening:
            return None
        if hardening["engine"] in BASELINE_ENGINES:
            baseline = self.baselines[hardening["baseline"]]
            return {"baseline": baseline["metadata"]["name"], **baseline["spec"]}
        level = hardening.get("level", "baseline")
        excluded = set(hardening.get("exclude", []))
        controls = [
            {
                "name": name,
                "cis": control["metadata"].get("references", {}).get("cis", ""),
                "check": control["spec"]["check"],
                "remediate": control["spec"]["remediate"],
            }
            for name, control in sorted(self.controls.items())
            if control["spec"]["platform"] == spec["platform"]
            and LEVELS[control["spec"]["level"]] <= LEVELS[level]
            and name not in excluded
        ]
        return {"engine": hardening["engine"], "level": level, "controls": controls}

    def compile_all(self) -> dict[str, Document]:
        return {name: self.compile(name) for name in sorted(self.profiles)}


def _unique(names: Iterable[str]) -> list[str]:
    seen: list[str] = []
    for name in names:
        if name and name not in seen:
            seen.append(name)
    return seen


def drift_settings(drift: Document) -> Document:
    """Drift block of a plan: explicit mode per category and a normalised maintenance window.

    `mode` stays in the plan as the default, so executors that predate per-category modes keep
    working; current executors read `agents`, `apps` and `hardening`.
    """
    settings: Document = {**DEFAULT_DRIFT, **drift}
    for category in DRIFT_CATEGORIES:
        settings.setdefault(category, settings["mode"])
    if "window" in settings:
        settings["window"] = {"utcOffset": DEFAULT_UTC_OFFSET, **settings["window"]}
    return settings


def digest(body: Document) -> str:
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def encode_plan(plan: Document) -> str:
    """Encode a plan for transport as one command parameter: "gz1:" + base64(gzip(json))."""
    raw = json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()
    encoded = PLAN_PREFIX + base64.b64encode(gzip.compress(raw, mtime=0)).decode("ascii")
    if len(encoded) > MAX_ENCODED_PLAN_CHARS:
        raise CatalogError(
            [
                f"plan for '{plan.get('profile')}' encodes to {len(encoded)} chars "
                f"(max {MAX_ENCODED_PLAN_CHARS}); split the profile or shorten scripts"
            ]
        )
    return encoded


def decode_plan(encoded: str) -> Document:
    """Decode a plan produced by encode_plan (plain base64 JSON is also accepted)."""
    if encoded.startswith(PLAN_PREFIX):
        raw = gzip.decompress(base64.b64decode(encoded[len(PLAN_PREFIX) :], validate=True))
    else:
        raw = base64.b64decode(encoded, validate=True)
    plan = json.loads(raw)
    if not isinstance(plan, dict) or plan.get("kind") != "InstallPlan":
        raise CatalogError(["not an InstallPlan"])
    return plan
