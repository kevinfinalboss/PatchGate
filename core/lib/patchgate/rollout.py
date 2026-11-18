"""Rollout rules: which ring and approver group an instance belongs to, how a change moves
through the rings, which version each instance receives and when an approval is decided.

Standard library only and free of cloud APIs (ADR 0010): providers feed it instances and account
settings and execute the resulting stages with their own orchestration.

Vocabulary:
- **unit**: the approval unit, a ring plus an approver group ("1:payments"). Each unit approves
  its own instances and each unit records the version released to it.
- **revision**: a document's content digest ("sha256:..."). Releases name revisions, not version
  numbers, so built-in documents re-applied by a release are tracked too.
- **released**: per document, {unit: revision} plus "*" for the revision every other unit gets
  (the last one released everywhere). Documents without releases are served as they are.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

ALL_UNITS = "*"
GATED_KINDS = ("AppSet", "Profile")
_DURATION = re.compile(r"^(\d+)([mh])$")

Document = dict[str, Any]


class RolloutError(ValueError):
    pass


def duration_seconds(value: str) -> int:
    """'10m' -> 600, '72h' -> 259200."""
    match = _DURATION.match(value)
    if not match:
        raise RolloutError(f"invalid duration {value!r}")
    return int(match.group(1)) * (60 if match.group(2) == "m" else 3600)


def unit_key(ring: int, group: str) -> str:
    return f"{ring}:{group}"


# ------------------------------------------------------------------ placement


@dataclass(frozen=True)
class Target:
    """A managed instance as the provider sees it (tags already read)."""

    instance_id: str
    account: str
    profile: str
    appsets: tuple[str, ...] = ()
    ring_tag: str | None = None
    approvers_tag: str | None = None


@dataclass(frozen=True)
class AccountSettings:
    ring: int
    approvers: str
    allowed_approvers: frozenset[str] = frozenset()
    allow_ring_tag: bool = False


@dataclass(frozen=True)
class Placement:
    ring: int
    group: str
    warnings: tuple[str, ...] = ()

    @property
    def unit(self) -> str:
        return unit_key(self.ring, self.group)


def policy_rings(policy: Document) -> list[int]:
    return [r["ring"] for r in policy["spec"]["rings"]]


def place(
    target: Target,
    accounts: Mapping[str, AccountSettings],
    policy: Document,
    default_group: str,
) -> Placement:
    """Ring and approver group of an instance.

    Group: Patchgate:Approvers tag (only groups the account allows) -> account group -> hub
    default. Ring: account ring, or the Patchgate:Ring tag when the account allows it (single-
    account mode). Unregistered accounts go to the last (most protected) ring.
    """
    rings = policy_rings(policy)
    warnings: list[str] = []
    settings = accounts.get(target.account)
    if settings is None:
        warnings.append(f"account {target.account} is not registered; using ring {rings[-1]}")
        ring, group, allowed = rings[-1], default_group, frozenset({default_group})
    else:
        ring, group = settings.ring, settings.approvers
        allowed = settings.allowed_approvers | {settings.approvers}
        if ring not in rings:
            warnings.append(f"account ring {ring} is not in the rollout policy; using {rings[-1]}")
            ring = rings[-1]
        if target.ring_tag is not None:
            if not settings.allow_ring_tag:
                warnings.append(
                    f"Patchgate:Ring ignored: account {target.account} does not allow it"
                )
            elif not target.ring_tag.isdigit() or int(target.ring_tag) not in rings:
                warnings.append(f"Patchgate:Ring {target.ring_tag!r} is not a policy ring")
            else:
                ring = int(target.ring_tag)
    if target.approvers_tag is not None and target.approvers_tag != group:
        if target.approvers_tag in allowed:
            group = target.approvers_tag
        else:
            warnings.append(
                f"Patchgate:Approvers {target.approvers_tag!r} is not allowed in account "
                f"{target.account}; using {group!r}"
            )
    return Placement(ring, group, tuple(warnings))


def affected(targets: Iterable[Target], kind: str, name: str) -> list[Target]:
    """Instances whose plan uses the document."""
    if kind == "Profile":
        return [t for t in targets if t.profile == name]
    if kind == "AppSet":
        return [t for t in targets if name in t.appsets]
    raise RolloutError(f"{kind} changes are not rolled out")


# ------------------------------------------------------------------ planning


@dataclass
class Stage:
    ring: int
    approval: bool
    soak_seconds: int
    units: dict[str, list[str]] = field(default_factory=dict)  # unit -> instance ids

    def groups(self) -> dict[str, list[str]]:
        return {unit.split(":", 1)[1]: ids for unit, ids in self.units.items()}


@dataclass
class RolloutPlan:
    kind: str
    name: str
    revision: str
    stages: list[Stage]
    warnings: list[str]

    @property
    def covers_every_ring(self) -> bool:
        """True when every ring has instances (needed to release the revision everywhere)."""
        return all(stage.units for stage in self.stages)

    def summary(self) -> str:
        parts = []
        for stage in self.stages:
            count = sum(len(ids) for ids in stage.units.values())
            groups = ", ".join(f"{g} {len(ids)}" for g, ids in sorted(stage.groups().items()))
            parts.append(f"ring {stage.ring}: {count}" + (f" ({groups})" if groups else ""))
        return " | ".join(parts)


def plan_rollout(
    kind: str,
    name: str,
    revision: str,
    targets: Iterable[Target],
    accounts: Mapping[str, AccountSettings],
    policy: Document,
    default_group: str,
) -> RolloutPlan:
    """Ordered stages for a new revision of a gated document. Rings without instances still get a
    stage (empty) so the provider can report them; they need no approval."""
    if kind not in policy["spec"].get("kinds", list(GATED_KINDS)):
        raise RolloutError(f"{kind} changes are not gated by rollout policy")
    stages = [
        Stage(
            ring=r["ring"],
            approval=r.get("approval", "none") == "required",
            soak_seconds=duration_seconds(r.get("soak", "0m")),
        )
        for r in policy["spec"]["rings"]
    ]
    by_ring = {stage.ring: stage for stage in stages}
    warnings: list[str] = []
    for target in sorted(affected(targets, kind, name), key=lambda t: t.instance_id):
        placement = place(target, accounts, policy, default_group)
        warnings.extend(f"{target.instance_id}: {w}" for w in placement.warnings)
        by_ring[placement.ring].units.setdefault(placement.unit, []).append(target.instance_id)
    return RolloutPlan(kind, name, revision, stages, warnings)


# ------------------------------------------------------------------ released versions


def revision_of(document_json: str) -> str:
    """Content digest of a stored document (the exact JSON string kept by the store)."""
    return "sha256:" + hashlib.sha256(document_json.encode()).hexdigest()


def revision_for(released: Mapping[str, str], placement: Placement) -> str | None:
    """Revision an instance receives: its unit's release, else the one released everywhere."""
    return released.get(placement.unit, released.get(ALL_UNITS))


def release(released: Mapping[str, str], unit: str, revision: str) -> dict[str, str]:
    return {**released, unit: revision}


def revert(released: Mapping[str, str], unit: str, previous: Mapping[str, str]) -> dict[str, str]:
    """Give a unit back what it had before the rollout started."""
    restored = dict(released)
    if unit in previous:
        restored[unit] = previous[unit]
    else:
        restored.pop(unit, None)
    return restored


def complete(released: Mapping[str, str], revision: str, plan: RolloutPlan) -> dict[str, str]:
    """After a rollout: if every ring had instances and every unit took the revision, it becomes
    the revision for everyone (new instances included). Otherwise units keep explicit entries and
    "*" stays where it was, so instances nobody approved for never receive it."""
    units = [unit for stage in plan.stages for unit in stage.units]
    if plan.covers_every_ring and all(released.get(u) == revision for u in units):
        return {ALL_UNITS: revision}
    return dict(released)


# ------------------------------------------------------------------ approvals


@dataclass(frozen=True)
class Decision:
    member: str
    approve: bool


def approval_state(decisions: Iterable[Decision], group: Document) -> str:
    """'approved', 'rejected' or 'pending'. Any member's rejection rejects; approvals count once
    per member; people outside the group are ignored."""
    members = {m.lower() for m in group["spec"]["members"]}
    approvers: set[str] = set()
    for decision in decisions:
        member = decision.member.lower()
        if member not in members:
            continue
        if not decision.approve:
            return "rejected"
        approvers.add(member)
    return "approved" if len(approvers) >= group["spec"].get("minApprovals", 1) else "pending"
