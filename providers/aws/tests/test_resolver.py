from __future__ import annotations

import importlib.util
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import boto3
import pytest
from moto import mock_aws

HANDLER = Path(__file__).resolve().parents[1] / "functions" / "resolver" / "handler.py"
CATALOG = Path(__file__).resolve().parents[3] / "core" / "catalog"
ACCOUNT = "123456789012"
REGION = "us-east-2"


@pytest.fixture
def resolver(monkeypatch: pytest.MonkeyPatch) -> Iterator[ModuleType]:
    monkeypatch.setenv("AWS_REGION", REGION)
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("PATCHGATE_CONFIG_TABLE", "patchgate-config")
    spec = importlib.util.spec_from_file_location("resolver_handler", HANDLER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["resolver_handler"] = module
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "ssm_ping_status", lambda session, iid: "Online")
    with mock_aws():
        seed_config_table()
        yield module


def seed_config_table() -> None:
    from patchgate.catalog import Catalog
    from patchgate_aws.store import BUILT_IN, ConfigStore

    boto3.client("dynamodb", region_name=REGION).create_table(
        TableName="patchgate-config",
        BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[
            {"AttributeName": "kind", "AttributeType": "S"},
            {"AttributeName": "name", "AttributeType": "S"},
        ],
        KeySchema=[
            {"AttributeName": "kind", "KeyType": "HASH"},
            {"AttributeName": "name", "KeyType": "RANGE"},
        ],
    )
    for name, keys in (
        ("patchgate-locks", [("lockId", "HASH")]),
        ("patchgate-releases", [("id", "HASH")]),
        ("patchgate-config-history", [("id", "HASH"), ("changeId", "RANGE")]),
    ):
        boto3.client("dynamodb", region_name=REGION).create_table(
            TableName=name,
            BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[{"AttributeName": k, "AttributeType": "S"} for k, _ in keys],
            KeySchema=[{"AttributeName": k, "KeyType": t} for k, t in keys],
        )
    store = ConfigStore("patchgate-config")
    for doc in Catalog.load(CATALOG).documents():
        store.put(doc, "test-seed", managed_by=BUILT_IN)


def launch(tags: dict[str, str], platform: str | None = None) -> str:
    ec2 = boto3.client("ec2", region_name=REGION)
    linux_images = [i for i in ec2.describe_images()["Images"] if "Platform" not in i]
    image = linux_images[0]["ImageId"]
    instance = ec2.run_instances(
        ImageId=image,
        MinCount=1,
        MaxCount=1,
        TagSpecifications=[
            {"ResourceType": "instance", "Tags": [{"Key": k, "Value": v} for k, v in tags.items()]}
        ],
    )["Instances"][0]
    return str(instance["InstanceId"])


def event(instance_id: str, **overrides: Any) -> dict[str, Any]:
    return {"accountId": ACCOUNT, "region": REGION, "instanceId": instance_id, **overrides}


def test_unmanaged_instance_is_skipped(resolver: ModuleType) -> None:
    result = resolver.handler(event(launch({"Name": "x"})))
    assert result["managed"] is False
    assert result["roleArn"].endswith(":role/Patchgate-Execution")
    assert set(result) >= {"alreadyBootstrapped", "ssmOnline", "documentName"}


def test_managed_linux_gets_default_profile_and_plan(resolver: ModuleType) -> None:
    result = resolver.handler(event(launch({"Patchgate:Managed": "true"})))
    assert result["managed"] is True
    assert result["platform"] == "linux"
    assert result["profile"] == "base-linux"
    assert result["documentName"] == "Patchgate-Bootstrap-Linux"
    assert result["documentParameters"]["Plan"][0].startswith("gz1:")
    assert result["ssmOnline"] is True
    assert result["alreadyBootstrapped"] is False


def test_windows_instance_uses_windows_document(
    resolver: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = resolver.describe_instance
    monkeypatch.setattr(
        resolver,
        "describe_instance",
        lambda s, i: {**original(s, i), "Platform": "windows"},
    )
    result = resolver.handler(
        event(launch({"Patchgate:Managed": "true", "Patchgate:Profile": "web-windows"}))
    )
    assert result["platform"] == "windows"
    assert result["documentName"] == "Patchgate-Bootstrap-Windows"


def test_already_bootstrapped_with_same_digest(resolver: ModuleType) -> None:
    first = resolver.handler(event(launch({"Patchgate:Managed": "true"})))
    digest = first["digest"]
    tags = {
        "Patchgate:Managed": "true",
        "Patchgate:BootstrapStatus": "Success",
        "Patchgate:BootstrapDigest": digest,
    }
    assert resolver.handler(event(launch(tags)))["alreadyBootstrapped"] is True


def test_catalog_change_triggers_rebootstrap(resolver: ModuleType) -> None:
    tags = {
        "Patchgate:Managed": "true",
        "Patchgate:BootstrapStatus": "Success",
        "Patchgate:BootstrapDigest": "sha256:old",
    }
    assert resolver.handler(event(launch(tags)))["alreadyBootstrapped"] is False


def test_profile_platform_mismatch_is_rejected(resolver: ModuleType) -> None:
    instance = launch({"Patchgate:Managed": "true", "Patchgate:Profile": "web-windows"})
    with pytest.raises(resolver.ResolveError, match="is for windows"):
        resolver.handler(event(instance))


@pytest.mark.parametrize("profile", ["missing-profile", "Bad;Name", "../etc"])
def test_unknown_or_invalid_profile_is_rejected(resolver: ModuleType, profile: str) -> None:
    instance = launch({"Patchgate:Managed": "true", "Patchgate:Profile": profile})
    with pytest.raises(resolver.ResolveError):
        resolver.handler(event(instance))


@pytest.mark.parametrize(
    "overrides",
    [
        {"accountId": "arn:aws:iam::evil"},
        {"instanceId": "i-; rm -rf /"},
        {"region": "us-east-1"},
    ],
)
def test_invalid_events_are_rejected(resolver: ModuleType, overrides: dict[str, str]) -> None:
    with pytest.raises(resolver.ResolveError):
        resolver.handler(event("i-0123456789abcdef0", **overrides))


def test_appsets_are_composed_and_change_the_digest(resolver: ModuleType) -> None:
    plain = resolver.handler(event(launch({"Patchgate:Managed": "true"})))
    tools = resolver.handler(
        event(launch({"Patchgate:Managed": "true", "Patchgate:AppSets": "devtools"}))
    )
    assert tools["appSets"] == ["devtools"]
    assert tools["digest"] != plain["digest"]
    from patchgate.plan import decode_plan

    plan = decode_plan(tools["documentParameters"]["Plan"][0])
    assert {a["name"] for a in plan["apps"]} == {"git", "jq"}


@pytest.mark.parametrize(
    ("appsets", "message"),
    [
        ("dotnet8", "not allowed by profile"),  # Windows set on a Linux profile
        ("does-not-exist", "unknown app set"),
        ("bad;name", "invalid app set"),
    ],
)
def test_invalid_appset_requests_are_refused(
    resolver: ModuleType, appsets: str, message: str
) -> None:
    instance = launch({"Patchgate:Managed": "true", "Patchgate:AppSets": appsets})
    with pytest.raises(resolver.ResolveError, match=message):
        resolver.handler(event(instance))


def test_invalid_document_edited_in_console_is_refused(resolver: ModuleType) -> None:
    """Someone edits an item directly in DynamoDB and breaks it: never apply it."""
    import json

    table = boto3.resource("dynamodb", region_name=REGION).Table("patchgate-config")
    item = table.get_item(Key={"kind": "AppSet", "name": "devtools"})["Item"]
    doc = json.loads(item["document"])
    doc["spec"]["apps"] = []  # schema requires at least one app
    table.update_item(
        Key={"kind": "AppSet", "name": "devtools"},
        UpdateExpression="SET #d = :d",
        ExpressionAttributeNames={"#d": "document"},
        ExpressionAttributeValues={":d": json.dumps(doc)},
    )
    instance = launch({"Patchgate:Managed": "true", "Patchgate:AppSets": "devtools"})
    with pytest.raises(resolver.ResolveError, match="invalid configuration: AppSet/devtools"):
        resolver.handler(event(instance))


def test_configuration_changes_apply_without_redeploy(resolver: ModuleType) -> None:
    from patchgate_aws.store import ConfigStore

    store = ConfigStore("patchgate-config")
    before = resolver.handler(event(launch({"Patchgate:Managed": "true"})))["digest"]
    record = store.get("Profile", "base-linux")
    assert record is not None
    doc = record.document
    doc["spec"]["drift"]["interval"] = "2h"
    store.put(doc, "tester")
    after = resolver.handler(event(launch({"Patchgate:Managed": "true"})))["digest"]
    assert after != before


def test_seed_file_is_up_to_date_with_catalog() -> None:
    import json

    from patchgate.catalog import Catalog

    seed = json.loads((Path(__file__).resolve().parents[1] / "config" / "seed.json").read_text())
    expected = [
        {
            "kind": d["kind"],
            "name": d["metadata"]["name"],
            "document": json.dumps(d, sort_keys=True),
        }
        for d in Catalog.load(CATALOG).documents()
    ]
    assert seed == expected, "seed.json is stale: run `make seed`"


def test_concurrent_runs_on_one_instance_are_serialised(resolver: ModuleType) -> None:
    """Launch and tag-change events arrive together: only one run may install at a time."""
    instance = launch({"Patchgate:Managed": "true"})
    first = resolver.handler(event(instance, executionName="launch-run"))
    second = resolver.handler(event(instance, executionName="tag-run"))
    again = resolver.handler(event(instance, executionName="launch-run"))
    assert first["lockBusy"] is False
    assert second["lockBusy"] is True
    assert again["lockBusy"] is False  # re-entrant for the owner (SSM wait loop)


def test_stale_locks_expire(resolver: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    instance = launch({"Patchgate:Managed": "true"})
    resolver.handler(event(instance, executionName="crashed-run"))
    later = resolver.time.time() + resolver.LOCK_TTL_SECONDS + 1
    monkeypatch.setattr(resolver.time, "time", lambda: later)
    assert resolver.handler(event(instance, executionName="new-run"))["lockBusy"] is False


def test_no_lock_when_nothing_to_do(resolver: ModuleType) -> None:
    instance = launch({"Name": "unmanaged"})
    result = resolver.handler(event(instance, executionName="x"))
    assert result["lockBusy"] is False
    items = boto3.client("dynamodb", region_name=REGION).scan(TableName="patchgate-locks")
    assert items["Count"] == 0


# ---------------------------------------------------------------- rings, approver groups, releases


def register_account(ring: int, approvers: str = "platform", **spec: Any) -> None:
    from patchgate_aws.store import ConfigStore

    store = ConfigStore("patchgate-config")
    for group in {approvers, *spec.get("allowedApprovers", [])}:
        store.put(
            {
                "apiVersion": "patchgate.io/v1alpha1",
                "kind": "ApproverGroup",
                "metadata": {"name": group, "description": "test"},
                "spec": {"members": [f"{group}@example.com"]},
            },
            "test",
        )
    store.put(
        {
            "apiVersion": "patchgate.io/v1alpha1",
            "kind": "Account",
            "metadata": {"name": ACCOUNT},
            "spec": {"ring": ring, "approvers": approvers, **spec},
        },
        "test",
    )


def change_base_linux_pending_in_ring_0() -> tuple[str, str]:
    """base-linux gets a new revision released only to ring 0 / platform (as a rollout would)."""
    import json

    from patchgate_aws.store import ConfigStore, ReleaseStore

    store = ConfigStore("patchgate-config")
    old = store.get("Profile", "base-linux")
    assert old is not None
    # what the config-audit stream consumer records for every version
    boto3.resource("dynamodb").Table("patchgate-config-history").put_item(
        Item={"id": "Profile#base-linux", "changeId": "1#1", "version": 1, "document": old.raw}
    )
    ReleaseStore("patchgate-releases").pin("Profile", "base-linux", old.revision, "test")
    doc = json.loads(old.raw)
    doc["spec"]["drift"]["interval"] = "2h"
    store.put(doc, "tester")
    new = store.get("Profile", "base-linux")
    assert new is not None
    boto3.resource("dynamodb").Table("patchgate-releases").update_item(
        Key={"id": "Profile#base-linux"},
        UpdateExpression="SET released.#u = :r",
        ExpressionAttributeNames={"#u": "0:platform"},
        ExpressionAttributeValues={":r": new.revision},
    )
    return old.revision, new.revision


def drift_interval(result: dict[str, Any]) -> str:
    from patchgate.plan import decode_plan

    return str(decode_plan(result["documentParameters"]["Plan"][0])["drift"]["interval"])


def test_each_ring_gets_the_revision_released_to_it(resolver: ModuleType) -> None:
    change_base_linux_pending_in_ring_0()
    register_account(ring=0)
    dev = resolver.handler(event(launch({"Patchgate:Managed": "true"})))
    assert (dev["ring"], dev["approvers"], drift_interval(dev)) == (0, "platform", "2h")
    register_account(ring=1)
    prod = resolver.handler(event(launch({"Patchgate:Managed": "true"})))
    assert (prod["ring"], drift_interval(prod)) == (1, "30m")  # still the approved revision


def test_unregistered_account_is_served_as_the_last_ring(resolver: ModuleType) -> None:
    change_base_linux_pending_in_ring_0()
    result = resolver.handler(event(launch({"Patchgate:Managed": "true"})))
    assert result["ring"] == 1 and drift_interval(result) == "30m"
    assert "not registered" in result["placementWarnings"][0]


def test_approver_tag_is_honoured_only_for_allowed_groups(resolver: ModuleType) -> None:
    register_account(ring=1, allowedApprovers=["payments"])
    tags = {"Patchgate:Managed": "true", "Patchgate:Approvers": "payments"}
    assert resolver.handler(event(launch(tags)))["approvers"] == "payments"
    refused = resolver.handler(event(launch({**tags, "Patchgate:Approvers": "myself"})))
    assert refused["approvers"] == "platform"
    assert "not allowed" in refused["placementWarnings"][0]
    odd = resolver.handler(event(launch({**tags, "Patchgate:Approvers": "Bad Name!"})))
    assert "not a valid group name" in odd["placementWarnings"][0]


def test_released_revision_missing_from_history_is_refused(resolver: ModuleType) -> None:
    from patchgate_aws.store import ReleaseStore

    ReleaseStore("patchgate-releases").pin("Profile", "base-linux", "sha256:gone", "test")
    with pytest.raises(resolver.ResolveError, match="not in history"):
        resolver.handler(event(launch({"Patchgate:Managed": "true"})))
