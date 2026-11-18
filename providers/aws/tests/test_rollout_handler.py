"""patchgate-rollout steps against mocked AWS (moto)."""

from __future__ import annotations

import importlib.util
import json
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import boto3
import pytest
from moto import mock_aws

HANDLER = Path(__file__).resolve().parents[1] / "functions" / "rollout" / "handler.py"
CATALOG = Path(__file__).resolve().parents[3] / "core" / "catalog"
REGION = "us-east-2"
ACCOUNT = "123456789012"  # moto's account: registered as ring 0 or 1 depending on the test


def create_tables() -> None:
    for name, keys in (
        ("patchgate-config", [("kind", "HASH"), ("name", "RANGE")]),
        ("patchgate-config-history", [("id", "HASH"), ("changeId", "RANGE")]),
        ("patchgate-releases", [("id", "HASH")]),
    ):
        boto3.client("dynamodb").create_table(
            TableName=name,
            BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[{"AttributeName": k, "AttributeType": "S"} for k, _ in keys],
            KeySchema=[{"AttributeName": k, "KeyType": t} for k, t in keys],
        )


@pytest.fixture
def rollout(monkeypatch: pytest.MonkeyPatch) -> Iterator[ModuleType]:
    for key, value in {
        "AWS_REGION": REGION,
        "AWS_DEFAULT_REGION": REGION,
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
    }.items():
        monkeypatch.setenv(key, value)
    with mock_aws():
        create_tables()
        from patchgate.catalog import Catalog
        from patchgate_aws.store import BUILT_IN, ConfigStore

        store = ConfigStore("patchgate-config")
        for doc in Catalog.load(CATALOG).documents():
            store.put(doc, "seed", managed_by=BUILT_IN)
        topic = boto3.client("sns").create_topic(Name="notes")["TopicArn"]
        monkeypatch.setenv("PATCHGATE_TOPIC_ARN", topic)
        spec = importlib.util.spec_from_file_location("rollout_handler", HANDLER)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules["rollout_handler"] = module
        spec.loader.exec_module(module)
        yield module


def register(ring: int, allowed: list[str] | None = None) -> None:
    from patchgate_aws.store import ConfigStore

    store = ConfigStore("patchgate-config")
    for group in ["platform", *(allowed or [])]:
        store.put(
            {
                "apiVersion": "patchgate.io/v1alpha1",
                "kind": "ApproverGroup",
                "metadata": {"name": group, "description": "test"},
                "spec": {"members": [f"{group}@example.com"]},
            },
            "test",
        )
    spec: dict[str, Any] = {"ring": ring, "approvers": "platform"}
    if allowed:
        spec["allowedApprovers"] = allowed
    store.put(
        {
            "apiVersion": "patchgate.io/v1alpha1",
            "kind": "Account",
            "metadata": {"name": ACCOUNT},
            "spec": spec,
        },
        "test",
    )


def launch(tags: dict[str, str]) -> str:
    ec2 = boto3.client("ec2")
    image = next(i for i in ec2.describe_images()["Images"] if "Platform" not in i)["ImageId"]
    return str(
        ec2.run_instances(
            ImageId=image,
            MinCount=1,
            MaxCount=1,
            TagSpecifications=[
                {
                    "ResourceType": "instance",
                    "Tags": [{"Key": k, "Value": v} for k, v in tags.items()],
                }
            ],
        )["Instances"][0]["InstanceId"]
    )


def current_revision(kind: str, name: str) -> str:
    from patchgate_aws.store import ConfigStore

    record = ConfigStore("patchgate-config").get(kind, name)
    assert record is not None
    return record.revision


def released(kind: str, name: str) -> dict[str, str]:
    from patchgate_aws.store import ReleaseStore

    return ReleaseStore("patchgate-releases").get(kind, name)


def test_plan_groups_running_managed_instances_by_ring_and_group(rollout: ModuleType) -> None:
    register(ring=1, allowed=["payments"])
    a = launch({"Patchgate:Managed": "true", "Patchgate:AppSets": "devtools"})
    b = launch(
        {
            "Patchgate:Managed": "true",
            "Patchgate:AppSets": "devtools",
            "Patchgate:Approvers": "payments",
        }
    )
    launch({"Patchgate:Managed": "true"})  # does not use devtools
    launch({"Patchgate:AppSets": "devtools"})  # not managed
    revision = current_revision("AppSet", "devtools")
    plan = rollout.handler(
        {"action": "plan", "kind": "AppSet", "name": "devtools", "revision": revision}
    )
    assert plan["skip"] is False
    ring0, ring1 = plan["stages"]
    assert ring0["empty"] is True and ring0["approval"] is False
    assert ring1["approval"] is True and ring1["approvalTimeoutSeconds"] == 72 * 3600
    assert {u["unit"]: [i["instanceId"] for i in u["instances"]] for u in ring1["units"]} == {
        "1:platform": [a],
        "1:payments": [b],
    }
    assert ring1["units"][0]["instances"][0]["accountId"] == ACCOUNT


def test_default_profile_instances_are_affected_by_profile_changes(rollout: ModuleType) -> None:
    register(ring=0)
    instance = launch({"Patchgate:Managed": "true"})  # no profile tag: base-linux
    plan = rollout.handler(
        {
            "action": "plan",
            "kind": "Profile",
            "name": "base-linux",
            "revision": current_revision("Profile", "base-linux"),
        }
    )
    assert plan["stages"][0]["units"][0]["instances"][0]["instanceId"] == instance


def test_outdated_revision_is_skipped(rollout: ModuleType) -> None:
    plan = rollout.handler(
        {"action": "plan", "kind": "AppSet", "name": "devtools", "revision": "sha256:" + "0" * 64}
    )
    assert plan["skip"] is True


@pytest.mark.parametrize(
    "event",
    [
        {"action": "plan", "kind": "Agent", "name": "x", "revision": "sha256:" + "0" * 64},
        {"action": "plan", "kind": "AppSet", "name": "Bad Name", "revision": "sha256:" + "0" * 64},
        {"action": "plan", "kind": "AppSet", "name": "devtools", "revision": "md5:1"},
        {"action": "drop-tables"},
    ],
)
def test_invalid_input_is_refused(rollout: ModuleType, event: dict[str, Any]) -> None:
    with pytest.raises(rollout.RolloutError):
        rollout.handler(event)


def stage(approval: bool) -> dict[str, Any]:
    return {
        "ring": 1,
        "approval": approval,
        "soakSeconds": 0,
        "empty": False,
        "units": [
            {"unit": f"1:{g}", "group": g, "instances": [{"instanceId": i, "accountId": ACCOUNT}]}
            for g, i in (("platform", "i-1"), ("payments", "i-2"))
        ],
    }


NEW = "sha256:" + "5" * 64
OLD = "sha256:" + "4" * 64


def test_release_gives_the_revision_only_to_approved_groups(rollout: ModuleType) -> None:
    from patchgate_aws.store import ReleaseStore

    ReleaseStore("patchgate-releases").pin("AppSet", "devtools", OLD, "test")
    result = rollout.handler(
        {
            "action": "release",
            "kind": "AppSet",
            "name": "devtools",
            "revision": NEW,
            "stage": stage(approval=True),
            "decisions": [
                {"unit": "1:platform", "decision": {"approved": True, "outcome": "approved"}},
                {"unit": "1:payments", "decision": {"approved": False, "outcome": "expired"}},
            ],
        }
    )
    assert result["approved"] == ["1:platform"]
    assert result["declined"] == [{"unit": "1:payments", "outcome": "expired"}]
    assert result["instances"] == [{"instanceId": "i-1", "accountId": ACCOUNT}]
    assert released("AppSet", "devtools") == {"*": OLD, "1:platform": NEW}


def test_rings_without_approval_release_every_group(rollout: ModuleType) -> None:
    result = rollout.handler(
        {
            "action": "release",
            "kind": "AppSet",
            "name": "devtools",
            "revision": NEW,
            "stage": stage(approval=False),
            "decisions": [],
        }
    )
    assert result["approved"] == ["1:payments", "1:platform"] or result["approved"] == [
        "1:platform",
        "1:payments",
    ]
    assert len(result["instances"]) == 2


def test_failed_instances_revert_the_ring(rollout: ModuleType) -> None:
    from patchgate_aws.store import ReleaseStore

    ReleaseStore("patchgate-releases").put("AppSet", "devtools", {"*": OLD, "1:platform": NEW}, "t")
    released_stage = {
        "approved": ["1:platform"],
        "instances": [{"instanceId": "i-1", "accountId": ACCOUNT}],
    }
    health = rollout.handler(
        {
            "action": "evaluate",
            "kind": "AppSet",
            "name": "devtools",
            "released": released_stage,
            "applied": [{"instanceId": "i-1", "accountId": ACCOUNT, "ok": False}],
            "previous": {"*": OLD},
            "onFailure": "revert",
        }
    )
    assert health["healthy"] is False and health["reverted"] is True
    assert health["reapply"] == released_stage["instances"]
    assert released("AppSet", "devtools") == {"*": OLD}


def test_healthy_ring_and_stop_without_revert(rollout: ModuleType) -> None:
    common = {"action": "evaluate", "kind": "AppSet", "name": "devtools", "previous": {"*": OLD}}
    released_stage = {"approved": ["1:platform"], "instances": []}
    ok = rollout.handler(
        {**common, "released": released_stage, "applied": [{"instanceId": "i-1", "ok": True}]}
    )
    assert ok["healthy"] is True
    stop = rollout.handler(
        {
            **common,
            "released": released_stage,
            "applied": [{"instanceId": "i-1", "ok": False}],
            "onFailure": "stop",
        }
    )
    assert (stop["healthy"], stop["reverted"], stop["failed"]) == (False, False, ["i-1"])


def stages(*rings: tuple[int, list[str]]) -> list[dict[str, Any]]:
    return [
        {
            "ring": ring,
            "approval": ring > 0,
            "soakSeconds": 0,
            "empty": not units,
            "units": [{"unit": u, "group": u.split(":")[1], "instances": []} for u in units],
        }
        for ring, units in rings
    ]


def test_complete_releases_everywhere_only_when_every_ring_took_it(rollout: ModuleType) -> None:
    from patchgate_aws.store import ReleaseStore

    releases = ReleaseStore("patchgate-releases")
    releases.put("AppSet", "devtools", {"*": OLD, "0:platform": NEW, "1:platform": NEW}, "t")
    everywhere = rollout.handler(
        {
            "action": "complete",
            "kind": "AppSet",
            "name": "devtools",
            "revision": NEW,
            "version": 2,
            "stages": stages((0, ["0:platform"]), (1, ["1:platform"])),
            "results": [
                {"ring": 0, "approved": ["0:platform"], "declined": [], "instances": [{}]},
                {"ring": 1, "approved": ["1:platform"], "declined": [], "instances": [{}]},
            ],
        }
    )
    assert everywhere["releasedEverywhere"] is True
    assert released("AppSet", "devtools") == {"*": NEW}

    releases.put("AppSet", "devtools", {"*": OLD, "0:platform": NEW}, "t")
    partial = rollout.handler(
        {
            "action": "complete",
            "kind": "AppSet",
            "name": "devtools",
            "revision": NEW,
            "version": 2,
            "stages": stages((0, ["0:platform"]), (1, [])),
            "results": [{"ring": 0, "approved": ["0:platform"], "declined": [], "instances": [{}]}],
        }
    )
    assert partial["releasedEverywhere"] is False
    assert released("AppSet", "devtools") == {"*": OLD, "0:platform": NEW}


def test_newer_rollouts_stop_older_ones_of_the_same_document(rollout: ModuleType) -> None:
    sfn = boto3.client("stepfunctions")
    role = f"arn:aws:iam::{ACCOUNT}:role/x"
    machine = sfn.create_state_machine(
        name="patchgate-rollout",
        definition=json.dumps(
            {"StartAt": "W", "States": {"W": {"Type": "Wait", "Seconds": 600, "End": True}}}
        ),
        roleArn=role,
    )["stateMachineArn"]
    old = sfn.start_execution(
        stateMachineArn=machine,
        name="AppSet-devtools-aaaaaaaaaaaa-1",
        input=json.dumps({"kind": "AppSet", "name": "devtools"}),
    )["executionArn"]
    other = sfn.start_execution(
        stateMachineArn=machine,
        name="AppSet-devtools-extra-bbbbbbbbbbbb-1",
        input=json.dumps({"kind": "AppSet", "name": "devtools-extra"}),
    )["executionArn"]
    new = sfn.start_execution(
        stateMachineArn=machine,
        name="AppSet-devtools-cccccccccccc-2",
        input=json.dumps({"kind": "AppSet", "name": "devtools"}),
    )["executionArn"]
    stopped = rollout.stop_older_rollouts("AppSet", "devtools", new)
    assert stopped == ["AppSet-devtools-aaaaaaaaaaaa-1"]
    assert sfn.describe_execution(executionArn=old)["status"] == "ABORTED"
    assert sfn.describe_execution(executionArn=other)["status"] == "RUNNING"


def test_rollouts_that_end_without_their_report_are_reported(
    rollout: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[str] = []
    monkeypatch.setattr(rollout, "notify", lambda subject, message: sent.append(subject))
    sfn = boto3.client("stepfunctions")
    machine = sfn.create_state_machine(
        name="patchgate-rollout",
        definition=json.dumps(
            {"StartAt": "W", "States": {"W": {"Type": "Wait", "Seconds": 600, "End": True}}}
        ),
        roleArn=f"arn:aws:iam::{ACCOUNT}:role/x",
    )["stateMachineArn"]
    arn = sfn.start_execution(
        stateMachineArn=machine,
        name="AppSet-devtools-aaaaaaaaaaaa-1",
        input=json.dumps({"kind": "AppSet", "name": "devtools"}),
    )["executionArn"]
    sfn.stop_execution(executionArn=arn, error="Cancelled", cause="by test")

    def event(status: str) -> dict[str, Any]:
        return {"source": "aws.states", "detail": {"status": status, "executionArn": arn}}

    assert rollout.handler(event("ABORTED")) == {"reported": True}
    assert sent == ["[Patchgate] Rollout ABORTED: AppSet/devtools"]
    assert rollout.handler(event("SUCCEEDED")) == {"reported": False}

    original = rollout.boto3.client

    class Stopped:
        def describe_execution(self, executionArn: str) -> dict[str, Any]:  # noqa: N803
            return {"error": "RolloutStopped", "name": "x"}

    monkeypatch.setattr(
        rollout.boto3,
        "client",
        lambda service, *a, **k: (
            Stopped() if service == "stepfunctions" else original(service, *a, **k)
        ),
    )
    assert rollout.handler(event("FAILED")) == {"reported": False}  # Report already emailed
