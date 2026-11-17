"""Load, validate and compile the Patchgate catalog.

The catalog is a directory of YAML documents (agents and profiles). Compiling a profile
produces an *install plan*: a self-contained JSON document with everything the in-VM
installer needs, so the VM never reads the catalog or calls a cloud API to decide what to do.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError, best_match

API_VERSION = "patchgate.io/v1alpha1"
SCHEMA_DIR = Path(__file__).resolve().parents[2] / "schemas"
# Plans travel as a single command parameter: gzip + base64 with a format prefix.
# Keep the encoded size well under common provider parameter limits (4 KB).
PLAN_PREFIX = "gz1:"
MAX_ENCODED_PLAN_CHARS = 3_800

Document = dict[str, Any]


class CatalogError(ValueError):
    """Raised when the catalog is invalid or a plan cannot be compiled."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("\n".join(errors))


@cache
def _validator(kind: str) -> Draft202012Validator:
    schema = json.loads((SCHEMA_DIR / f"{kind.lower()}.schema.json").read_text())
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def validate_document(doc: Any, source: str = "<memory>") -> list[str]:
    """Return schema errors for one document (empty list when valid)."""
    if not isinstance(doc, dict) or doc.get("kind") not in ("Agent", "Profile"):
        return [f"{source}: kind must be 'Agent' or 'Profile'"]
    validator = _validator(str(doc["kind"]))
    errors = sorted(validator.iter_errors(doc), key=lambda e: list(e.absolute_path))
    return [f"{source}: {_describe(err)}" for err in errors]


def _describe(err: ValidationError) -> str:
    """Explain an error; for oneOf/anyOf, report errors of the branch matching `method`."""
    if err.context:
        branches: dict[Any, list[ValidationError]] = {}
        for sub in err.context:
            branches.setdefault(sub.schema_path[0], []).append(sub)
        matching = [
            subs
            for subs in branches.values()
            if not any(list(sub.relative_path) == ["method"] for sub in subs)
        ]
        candidates = matching[0] if len(matching) == 1 else list(err.context)
        err = best_match(candidates)
    return f"{'/'.join(map(str, err.absolute_path)) or '<root>'}: {err.message}"


@dataclass
class Catalog:
    agents: dict[str, Document] = field(default_factory=dict)
    profiles: dict[str, Document] = field(default_factory=dict)

    @classmethod
    def load(cls, root: Path) -> Catalog:
        """Load and fully validate a catalog directory. Raises CatalogError on any problem."""
        catalog = cls()
        errors: list[str] = []
        for path in sorted(root.rglob("*.y*ml")):
            source = str(path.relative_to(root))
            try:
                doc = yaml.safe_load(path.read_text())
            except yaml.YAMLError as exc:
                errors.append(f"{source}: invalid YAML: {exc}")
                continue
            doc_errors = validate_document(doc, source)
            if doc_errors:
                errors.extend(doc_errors)
                continue
            name = doc["metadata"]["name"]
            if path.stem != name:
                errors.append(f"{source}: file name must match metadata.name ({name})")
            bucket = catalog.agents if doc["kind"] == "Agent" else catalog.profiles
            if name in bucket:
                errors.append(f"{source}: duplicate {doc['kind']} '{name}'")
            bucket[name] = doc
        errors.extend(catalog._cross_reference_errors())
        if errors:
            raise CatalogError(errors)
        return catalog

    def _cross_reference_errors(self) -> list[str]:
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
            if platform != "windows" and spec.get("chocolatey"):
                errors.append(f"profile '{name}': chocolatey is only supported on windows")
        return errors

    def compile(self, profile_name: str) -> Document:
        """Build the install plan for one profile."""
        profile = self.profiles.get(profile_name)
        if profile is None:
            raise CatalogError([f"unknown profile '{profile_name}'"])
        spec = profile["spec"]
        platform = spec["platform"]
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
            "hardening": spec.get("hardening", "none"),
            "steps": steps,
            "chocolatey": spec.get("chocolatey", []),
        }
        body["digest"] = _digest(body)
        return body

    def compile_all(self) -> dict[str, Document]:
        return {name: self.compile(name) for name in sorted(self.profiles)}


def _digest(body: Document) -> str:
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
