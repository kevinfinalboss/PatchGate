"""Rollout steps for the patchgate-rollout workflow (ADR 0010).

The workflow orders the steps (rings, approvals, soak); this function does the work of each one:

- plan:     check the revision is still current, stop older rollouts of the same document,
            discover managed instances in registered accounts and group them by ring and
            approver group (patchgate.rollout.plan_rollout).
- release:  give a stage's approved units the new revision; return the instances to re-plan.
- evaluate: health of a stage from its bootstrap runs; on failure revert the stage's units to
            what they had before (onFailure: revert) and return the instances to re-plan.
- complete: release the revision to everyone when every ring took it; email a summary.
- report:   email why a rollout stopped.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

import boto3

from patchgate.rollout import (
    AccountSettings,
    RolloutPlan,
    Stage,
    Target,
    complete,
    duration_seconds,
    plan_rollout,
    release,
    revert,
)
from patchgate_aws.store import ConfigStore, ReleaseStore
from patchgate_aws.validation import validate

LOG = logging.getLogger()
LOG.setLevel(logging.INFO)

CONFIG_TABLE = os.environ.get("PATCHGATE_CONFIG_TABLE", "patchgate-config")
HISTORY_TABLE = os.environ.get("PATCHGATE_HISTORY_TABLE", "patchgate-config-history")
RELEASES_TABLE = os.environ.get("PATCHGATE_RELEASES_TABLE", "patchgate-releases")
ROLLOUT_POLICY = os.environ.get("PATCHGATE_ROLLOUT_POLICY", "default")
DEFAULT_APPROVERS = os.environ.get("PATCHGATE_DEFAULT_APPROVERS", "platform")
EXECUTION_ROLE_NAME = os.environ.get("EXECUTION_ROLE_NAME", "Patchgate-Execution")
TOPIC_ARN = os.environ.get("PATCHGATE_TOPIC_ARN", "")
PARTITION = os.environ.get("AWS_PARTITION", "aws")
DEFAULT_PROFILES = {"linux": "base-linux", "windows": "base-windows"}
BY = "rollout"

_KIND = re.compile(r"^(AppSet|Profile)$")
_NAME = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
_REVISION = re.compile(r"^sha256:[0-9a-f]{64}$")


class RolloutError(Exception):
    pass


def _require(pattern: re.Pattern[str], value: Any, field: str) -> str:
    if not isinstance(value, str) or not pattern.match(value):
        raise RolloutError(f"invalid {field}: {value!r}")
    return value


def store() -> ConfigStore:
    return ConfigStore(
        CONFIG_TABLE, history_table=HISTORY_TABLE, releases=ReleaseStore(RELEASES_TABLE)
    )


def execution_prefix(kind: str, name: str) -> str:
    """Rollout execution names start with this (see the config-audit function)."""
    return f"{kind}-{name}"[:48] + "-"


# ------------------------------------------------------------------ discovery


def spoke_session(account_id: str) -> boto3.Session:
    role_arn = f"arn:{PARTITION}:iam::{account_id}:role/{EXECUTION_ROLE_NAME}"
    creds = boto3.client("sts").assume_role(RoleArn=role_arn, RoleSessionName="patchgate-rollout")[
        "Credentials"
    ]
    return boto3.Session(
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
        region_name=os.environ.get("AWS_REGION"),
    )


def discover(account_id: str) -> list[Target]:
    """Running managed instances of one account (stopped ones re-plan when they start)."""
    ec2 = spoke_session(account_id).client("ec2")
    targets: list[Target] = []
    for page in ec2.get_paginator("describe_instances").paginate(
        Filters=[
            {"Name": "tag:Patchgate:Managed", "Values": ["true"]},
            {"Name": "instance-state-name", "Values": ["running"]},
        ]
    ):
        for reservation in page["Reservations"]:
            for instance in reservation["Instances"]:
                tags = {t["Key"]: t["Value"] for t in instance.get("Tags", [])}
                platform = (
                    "windows" if instance.get("Platform", "").lower() == "windows" else "linux"
                )
                appsets = tuple(
                    s.strip() for s in tags.get("Patchgate:AppSets", "").split(",") if s.strip()
                )
                targets.append(
                    Target(
                        instance_id=instance["InstanceId"],
                        account=account_id,
                        profile=tags.get("Patchgate:Profile", DEFAULT_PROFILES[platform]),
                        appsets=appsets,
                        ring_tag=tags.get("Patchgate:Ring"),
                        approvers_tag=tags.get("Patchgate:Approvers"),
                    )
                )
    return targets


def registered_accounts(config: ConfigStore) -> dict[str, AccountSettings]:
    accounts: dict[str, AccountSettings] = {}
    for record in config.list("Account"):
        if validate(record.document):
            LOG.warning("ignoring invalid Account/%s", record.document["metadata"]["name"])
            continue
        spec = record.document["spec"]
        accounts[record.document["metadata"]["name"]] = AccountSettings(
            ring=spec["ring"],
            approvers=spec["approvers"],
            allowed_approvers=frozenset(spec.get("allowedApprovers", [])),
            allow_ring_tag=spec.get("allowRingTag", False),
        )
    return accounts


# ------------------------------------------------------------------ actions


def stop_older_rollouts(kind: str, name: str, execution_arn: str) -> list[str]:
    """A newer revision supersedes rollouts of older ones; their pending approvals die with them."""
    if ":execution:" not in execution_arn:
        return []  # not started by the workflow (direct invocation): nothing to compare with
    sfn = boto3.client("stepfunctions")
    machine_arn = execution_arn.replace(":execution:", ":stateMachine:").rsplit(":", 1)[0]
    stopped: list[str] = []
    for page in sfn.get_paginator("list_executions").paginate(
        stateMachineArn=machine_arn, statusFilter="RUNNING"
    ):
        for execution in page["executions"]:
            if execution["executionArn"] == execution_arn:
                continue
            if not execution["name"].startswith(execution_prefix(kind, name)):
                continue
            detail = json.loads(
                sfn.describe_execution(executionArn=execution["executionArn"])["input"]
            )
            if (detail.get("kind"), detail.get("name")) == (kind, name):
                sfn.stop_execution(
                    executionArn=execution["executionArn"],
                    error="Superseded",
                    cause=f"a newer revision of {kind}/{name} is being rolled out",
                )
                stopped.append(execution["name"])
    return stopped


def stage_output(
    stage: Stage, accounts_of: dict[str, str], policy: dict[str, Any]
) -> dict[str, Any]:
    return {
        "ring": stage.ring,
        "approval": stage.approval,
        "soakSeconds": stage.soak_seconds,
        "approvalTimeoutSeconds": duration_seconds(policy["spec"].get("approvalTimeout", "72h")),
        "empty": not stage.units,
        "units": [
            {
                "unit": unit,
                "group": unit.split(":", 1)[1],
                "instances": [{"instanceId": i, "accountId": accounts_of[i]} for i in ids],
            }
            for unit, ids in sorted(stage.units.items())
        ],
    }


def plan(event: dict[str, Any]) -> dict[str, Any]:
    kind = _require(_KIND, event.get("kind"), "kind")
    name = _require(_NAME, event.get("name"), "name")
    revision = _require(_REVISION, event.get("revision"), "revision")
    config = store()
    record = config.get(kind, name)
    if record is None or record.revision != revision:
        return {"skip": True, "reason": f"{kind}/{name} changed again or was deleted"}
    policy = config.get("RolloutPolicy", ROLLOUT_POLICY)
    if policy is None or validate(policy.document):
        return {"skip": True, "reason": f"rollout policy {ROLLOUT_POLICY!r} missing or invalid"}
    stopped = stop_older_rollouts(kind, name, str(event.get("executionArn", "")))
    previous = config.releases.get(kind, name) if config.releases else {}
    accounts = registered_accounts(config)
    targets = [t for account in sorted(accounts) for t in discover(account)]
    rollout_plan = plan_rollout(
        kind, name, revision, targets, accounts, policy.document, DEFAULT_APPROVERS
    )
    accounts_of = {t.instance_id: t.account for t in targets}
    LOG.info("%s/%s %s: %s", kind, name, revision[:19], rollout_plan.summary())
    return {
        "skip": False,
        "version": record.version,
        "previous": previous,
        "onFailure": policy.document["spec"].get("onFailure", "revert"),
        "stages": [stage_output(s, accounts_of, policy.document) for s in rollout_plan.stages],
        "summary": rollout_plan.summary(),
        "warnings": rollout_plan.warnings[:20],
        "superseded": stopped,
    }


def release_stage(event: dict[str, Any]) -> dict[str, Any]:
    kind = _require(_KIND, event.get("kind"), "kind")
    name = _require(_NAME, event.get("name"), "name")
    revision = _require(_REVISION, event.get("revision"), "revision")
    stage = event["stage"]
    decisions = {d["unit"]: d.get("decision", {}) for d in event.get("decisions") or []}
    approved, declined, instances = [], [], []
    for unit in stage["units"]:
        outcome = (
            decisions.get(unit["unit"], {"approved": True})
            if stage["approval"]
            else {"approved": True}
        )
        if outcome.get("approved") is True:
            approved.append(unit["unit"])
            instances.extend(unit["instances"])
        else:
            declined.append({"unit": unit["unit"], "outcome": outcome.get("outcome", "rejected")})
    releases = ReleaseStore(RELEASES_TABLE)
    released = releases.get(kind, name)
    for unit in approved:
        released = release(released, unit, revision)
    if approved:
        releases.put(kind, name, released, BY)
    return {
        "ring": stage["ring"],
        "approved": approved,
        "declined": declined,
        "instances": instances,
    }


def evaluate(event: dict[str, Any]) -> dict[str, Any]:
    kind = _require(_KIND, event.get("kind"), "kind")
    name = _require(_NAME, event.get("name"), "name")
    released = event["released"]
    failed = [r["instanceId"] for r in event.get("applied") or [] if not r.get("ok")]
    if not failed:
        return {"healthy": True, "failed": [], "reverted": False, "reapply": []}
    if event.get("onFailure", "revert") != "revert":
        return {"healthy": False, "failed": failed, "reverted": False, "reapply": []}
    releases = ReleaseStore(RELEASES_TABLE)
    current = releases.get(kind, name)
    for unit in released["approved"]:
        current = revert(current, unit, event.get("previous") or {})
    releases.put(kind, name, current, f"{BY} (revert)")
    return {"healthy": False, "failed": failed, "reverted": True, "reapply": released["instances"]}


def notify(subject: str, message: str) -> None:
    if TOPIC_ARN:
        boto3.client("sns").publish(TopicArn=TOPIC_ARN, Subject=subject[:100], Message=message)


def finish(event: dict[str, Any]) -> dict[str, Any]:
    kind = _require(_KIND, event.get("kind"), "kind")
    name = _require(_NAME, event.get("name"), "name")
    revision = _require(_REVISION, event.get("revision"), "revision")
    stages = event.get("stages") or []
    results = {
        r["ring"]: r for r in event.get("results") or [] if isinstance(r, dict) and "ring" in r
    }
    synthetic = RolloutPlan(
        kind,
        name,
        revision,
        [
            Stage(s["ring"], s["approval"], s["soakSeconds"], {u["unit"]: [] for u in s["units"]})
            for s in stages
        ],
        [],
    )
    releases = ReleaseStore(RELEASES_TABLE)
    final = complete(releases.get(kind, name), revision, synthetic)
    if final != releases.get(kind, name):
        releases.put(kind, name, final, f"{BY} (complete)")
    everywhere = final.get("*") == revision
    lines = [f"{kind}/{name} version {event.get('version')} ({revision[:19]})", ""]
    for s in stages:
        r = results.get(s["ring"], {})
        if s["empty"]:
            lines.append(f"ring {s['ring']}: no running instances")
            continue
        declined = ", ".join(f"{d['unit']} {d['outcome']}" for d in r.get("declined", []))
        lines.append(
            f"ring {s['ring']}: released to {', '.join(r.get('approved', [])) or 'nobody'}; "
            f"{len(r.get('instances', []))} instance(s) updated"
            + (f"; {declined}" if declined else "")
        )
    lines += [
        "",
        "Released to every ring and approver group: new and restarted instances get it too."
        if everywhere
        else "Not released everywhere: units that did not approve, and rings without instances, "
        "keep the previous revision.",
    ]
    notify(f"[Patchgate] Rollout finished: {kind}/{name}", "\n".join(lines))
    return {"releasedEverywhere": everywhere, "released": final}


def report(event: dict[str, Any]) -> dict[str, Any]:
    error = event.get("error") or {}
    cause = str(error.get("Cause", ""))[:1500]
    notify(
        f"[Patchgate] Rollout STOPPED: {event.get('kind')}/{event.get('name')}",
        f"The rollout of {event.get('kind')}/{event.get('name')} stopped: "
        f"{error.get('Error', 'error')}.\n\n"
        f"{cause}\n\nInstances that failed were reverted to the previous revision when the "
        "rollout policy says onFailure: revert. Later rings were not touched.",
    )
    return {"reported": True}


ACTIONS = {
    "plan": plan,
    "release": release_stage,
    "evaluate": evaluate,
    "complete": finish,
    "report": report,
}


def handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    action = event.get("action")
    if action not in ACTIONS:
        raise RolloutError(f"unknown action {action!r}")
    return ACTIONS[action](event)
