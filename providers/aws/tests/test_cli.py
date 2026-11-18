from __future__ import annotations

import contextlib
import io
import json
from collections.abc import Iterator
from pathlib import Path

import boto3
import pytest
import yaml
from moto import mock_aws

from patchgate.catalog import Catalog
from patchgate_aws import cli
from patchgate_aws.store import BUILT_IN, ConfigStore

REGION = "us-east-2"
CATALOG = Path(__file__).resolve().parents[3] / "core" / "catalog"


def create_tables() -> None:
    client = boto3.client("dynamodb", region_name=REGION)
    for name, hash_key, range_key in (
        ("patchgate-config", "kind", "name"),
        ("patchgate-config-history", "id", "changeId"),
        ("patchgate-releases", "id", None),
        ("patchgate-approvals", "requestId", None),
    ):
        keys = [(hash_key, "HASH")] + ([(range_key, "RANGE")] if range_key else [])
        client.create_table(
            TableName=name,
            BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[{"AttributeName": k, "AttributeType": "S"} for k, _ in keys],
            KeySchema=[{"AttributeName": k, "KeyType": t} for k, t in keys],
        )


@pytest.fixture
def aws(monkeypatch: pytest.MonkeyPatch) -> Iterator[ConfigStore]:
    for key, value in {
        "AWS_DEFAULT_REGION": REGION,
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
    }.items():
        monkeypatch.setenv(key, value)
    with mock_aws():
        create_tables()
        store = ConfigStore("patchgate-config", boto3.Session(region_name=REGION))
        for doc in Catalog.load(CATALOG).documents():
            store.put(doc, "seed", managed_by=BUILT_IN)
        yield store


def run(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(["--region", REGION, *argv])
    return code, out.getvalue(), err.getvalue()


def write(tmp_path: Path, doc: dict[str, object]) -> str:
    path = tmp_path / "doc.yaml"
    path.write_text(yaml.safe_dump(doc))
    return str(path)


def appset(name: str, *apps: str, platform: str = "windows") -> dict[str, object]:
    return {
        "apiVersion": "patchgate.io/v1alpha1",
        "kind": "AppSet",
        "metadata": {"name": name, "description": "test"},
        "spec": {"platform": platform, "apps": [{"name": a} for a in apps]},
    }


def test_list_and_describe(aws: ConfigStore) -> None:
    code, out, _ = run("list", "appsets")
    assert code == 0 and "dotnet8" in out and "patchgate" in out
    code, out, _ = run("describe", "appset", "dotnet8")
    assert code == 0 and "dotnet-8.0-runtime" in out and "# version 1" in out


def test_apply_creates_and_versions(aws: ConfigStore, tmp_path: Path) -> None:
    code, out, _ = run("apply", "-f", write(tmp_path, appset("java17", "temurin17")))
    assert code == 0 and "created (version 1)" in out
    code, out, _ = run("apply", "-f", write(tmp_path, appset("java17", "temurin17", "maven")))
    assert code == 0 and "updated (version 2)" in out
    code, out, _ = run("apply", "-f", write(tmp_path, appset("java17", "temurin17", "maven")))
    assert "unchanged" in out


def test_apply_rejects_invalid_documents(aws: ConfigStore, tmp_path: Path) -> None:
    bad = appset("broken")
    bad["spec"] = {"platform": "windows", "apps": []}
    code, _, err = run("apply", "-f", write(tmp_path, bad))
    assert code == 1 and "should be non-empty" in err
    assert aws.get("AppSet", "broken") is None


def test_apply_rejects_dangling_references(aws: ConfigStore, tmp_path: Path) -> None:
    record = aws.get("Profile", "web-windows")
    assert record is not None
    profile = record.document
    profile["metadata"]["name"] = "custom-web"
    profile["spec"]["allowedAppSets"] = ["does-not-exist"]
    code, _, err = run("apply", "-f", write(tmp_path, profile))
    assert code == 1 and "unknown app set 'does-not-exist'" in err


def test_built_in_items_are_protected_and_copyable(aws: ConfigStore, tmp_path: Path) -> None:
    record = aws.get("AppSet", "dotnet8")
    assert record is not None
    changed = record.document
    changed["spec"]["apps"][0]["version"] = "8.0.35"
    code, _, err = run("apply", "-f", write(tmp_path, changed))
    assert code == 1 and "built-in" in err
    code, _, _ = run("copy", "appset", "dotnet8", "dotnet8-custom")
    assert code == 0 and aws.get("AppSet", "dotnet8-custom") is not None


def test_delete_refuses_referenced_documents(aws: ConfigStore, tmp_path: Path) -> None:
    run("apply", "-f", write(tmp_path, appset("extra", "putty")))
    record = aws.get("Profile", "web-windows")
    assert record is not None
    profile = record.document
    profile["metadata"]["name"] = "web-extra"
    profile["spec"]["allowedAppSets"] = ["extra"]
    assert run("apply", "-f", write(tmp_path, profile))[0] == 0
    code, _, err = run("delete", "appset", "extra")
    assert code == 1 and "web-extra" in err
    assert run("delete", "profile", "web-extra")[0] == 0
    assert run("delete", "appset", "extra")[0] == 0


def test_rollback_restores_a_previous_version(aws: ConfigStore, tmp_path: Path) -> None:
    v1, v2 = appset("java17", "temurin17"), appset("java17", "temurin17", "maven")
    run("apply", "-f", write(tmp_path, v1))
    run("apply", "-f", write(tmp_path, v2))
    # The audit Lambda writes history from the stream; simulate its entry for version 1.
    history = boto3.resource("dynamodb", region_name=REGION).Table("patchgate-config-history")
    history.put_item(
        Item={
            "id": "AppSet#java17",
            "changeId": "1#1",
            "event": "INSERT",
            "version": 1,
            "updatedBy": "x",
            "document": json.dumps(v1),
        }
    )
    code, out, _ = run("rollback", "appset", "java17", "--version", "1")
    assert code == 0 and "restored version 1 as 3" in out
    record = aws.get("AppSet", "java17")
    assert record is not None and record.document == v1


def launch(tags: dict[str, str]) -> str:
    ec2 = boto3.client("ec2", region_name=REGION)
    image = next(i for i in ec2.describe_images()["Images"] if i.get("Platform") == "windows")
    return str(
        ec2.run_instances(
            ImageId=image["ImageId"],
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


def instance_tags(instance_id: str) -> dict[str, str]:
    ec2 = boto3.client("ec2", region_name=REGION)
    instance = ec2.describe_instances(InstanceIds=[instance_id])["Reservations"][0]["Instances"][0]
    return {t["Key"]: t["Value"] for t in instance.get("Tags", [])}


def test_instance_appsets_are_validated_against_the_profile(aws: ConfigStore) -> None:
    instance = launch({"Patchgate:Managed": "true", "Patchgate:Profile": "web-windows"})
    code, out, _ = run("instance", "appsets", instance, "--add", "dotnet8")
    assert code == 0 and "-> dotnet8" in out
    assert instance_tags(instance)["Patchgate:AppSets"] == "dotnet8"
    code, _, err = run("instance", "appsets", instance, "--add", "devtools")  # Linux-only set
    assert code == 1 and "not allowed" in err
    assert run("instance", "appsets", instance, "--remove", "dotnet8")[0] == 0
    assert "Patchgate:AppSets" not in instance_tags(instance)


DRIFT_DOCUMENT = Path(__file__).resolve().parents[1] / "ssm" / "Patchgate-Drift.json"


def register_drift_document() -> None:
    boto3.client("ssm", region_name=REGION).create_document(
        Name="Patchgate-Drift",
        Content=DRIFT_DOCUMENT.read_text(),
        DocumentType="Command",
        DocumentFormat="JSON",
    )


def test_instance_reconcile_runs_the_drift_document_with_force(aws: ConfigStore) -> None:
    register_drift_document()
    instance = launch({"Patchgate:Managed": "true", "Patchgate:Profile": "web-windows"})
    code, out, _ = run("instance", "reconcile", instance)
    assert code == 0
    assert f"reconcile success on {instance}" in out
    ssm = boto3.client("ssm", region_name=REGION)
    command = ssm.list_commands(InstanceId=instance)["Commands"][0]
    assert command["DocumentName"] == "Patchgate-Drift"
    assert command["Parameters"] == {"Force": ["true"]}


def test_instance_reconcile_refuses_unmanaged_instances(aws: ConfigStore) -> None:
    instance = launch({"Name": "not-patchgate"})
    code, _, err = run("instance", "reconcile", instance)
    assert code == 1 and "not managed by Patchgate" in err


def test_reconcile_waits_until_the_command_finishes(
    aws: ConfigStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    from botocore.exceptions import ClientError

    class FakeSsm:
        def __init__(self) -> None:
            self.calls = 0

        def get_command_invocation(self, **kwargs: str) -> dict[str, str]:
            if "PluginName" in kwargs:  # per-step output, read once the command is done
                return {"Status": "Failed", "StandardOutputContent": f"{kwargs['PluginName']} out"}
            self.calls += 1
            if self.calls == 1:
                raise ClientError(
                    {"Error": {"Code": "InvocationDoesNotExist", "Message": "not yet"}},
                    "GetCommandInvocation",
                )
            if self.calls == 2:
                return {"Status": "InProgress"}
            return {"Status": "Failed", "StandardOutputContent": "agent demo failed"}

    monkeypatch.setattr(cli, "POLL_SECONDS", 0)
    args = cli.parser().parse_args(["--region", REGION, "instance", "reconcile", "i-1"])
    fake = FakeSsm()
    invocation = cli.Cli(args).wait_for_command(fake, "cmd-1", "i-1")
    assert invocation["Status"] == "Failed" and fake.calls == 3
    assert cli.step_output(fake, "cmd-1", "i-1", "driftWindows") == "driftWindows out"


def approver_group(name: str) -> dict[str, object]:
    return {
        "apiVersion": "patchgate.io/v1alpha1",
        "kind": "ApproverGroup",
        "metadata": {"name": name, "description": "test"},
        "spec": {"members": [f"{name}@example.com"]},
    }


def test_account_set_validates_groups_and_ring(aws: ConfigStore, tmp_path: Path) -> None:
    code, _, err = run("account", "set", "123456789012", "--ring", "1", "--approvers", "platform")
    assert code == 1 and "unknown approver group 'platform'" in err
    for group in ("platform", "payments"):
        assert run("apply", "-f", write(tmp_path, approver_group(group)))[0] == 0
    code, _, err = run("account", "set", "123456789012", "--ring", "5", "--approvers", "platform")
    assert code == 1 and "ring 5 is not in rollout policy" in err
    code, out, _ = run(
        "account", "set", "123456789012", "--ring", "1", "--approvers", "platform",
        "--allow", "payments",
    )  # fmt: skip
    assert code == 0 and "ring 1, approvers platform (also allowed: payments)" in out
    assert "123456789012" in run("list", "accounts")[1]
    code, _, err = run("account", "set", "12345", "--ring", "1", "--approvers", "platform")
    assert code == 1 and "invalid document" in err


def test_changing_a_gated_document_keeps_instances_on_the_current_revision(
    aws: ConfigStore, tmp_path: Path
) -> None:
    from patchgate_aws.store import ReleaseStore

    assert run("apply", "-f", write(tmp_path, appset("tools", "7zip")))[0] == 0
    first = aws.get("AppSet", "tools")
    assert first is not None
    assert ReleaseStore("patchgate-releases").get("AppSet", "tools") == {}  # new: served as is
    code, out, _ = run("apply", "-f", write(tmp_path, appset("tools", "7zip", "git")))
    assert code == 0 and "pending: instances keep version 1" in out
    assert ReleaseStore("patchgate-releases").get("AppSet", "tools") == {"*": first.revision}
    run("apply", "-f", write(tmp_path, appset("tools", "git")))
    assert ReleaseStore("patchgate-releases").get("AppSet", "tools") == {"*": first.revision}


def test_rollout_list_status_and_cancel(aws: ConfigStore) -> None:
    sfn = boto3.client("stepfunctions", region_name=REGION)
    machine = sfn.create_state_machine(
        name="patchgate-rollout",
        definition=json.dumps(
            {"StartAt": "W", "States": {"W": {"Type": "Wait", "Seconds": 600, "End": True}}}
        ),
        roleArn="arn:aws:iam::123456789012:role/x",
    )["stateMachineArn"]
    execution = sfn.start_execution(
        stateMachineArn=machine,
        name="AppSet-dotnet8-1a2b3c4d5e6f-1",
        input=json.dumps({"kind": "AppSet", "name": "dotnet8", "revision": "sha256:" + "a" * 64}),
    )["executionArn"]
    boto3.resource("dynamodb", region_name=REGION).Table("patchgate-approvals").put_item(
        Item={
            "requestId": "r1",
            "executionArn": execution,
            "unit": "1:payments",
            "status": "pending",
            "instances": ["i-1", "i-2"],
            "decisions": [{"member": "ana@example.com", "approve": True}],
        }
    )
    code, out, _ = run("rollout", "list")
    assert code == 0 and "AppSet-dotnet8-1a2b3c4d5e6f-1" in out and "RUNNING" in out
    code, out, _ = run("rollout", "status", "AppSet-dotnet8-1a2b3c4d5e6f-1")
    assert code == 0 and "AppSet/dotnet8" in out
    assert "1:payments" in out and "2 instance(s)" in out and "ana@example.com approved" in out
    assert run("rollout", "status", "nope")[0] == 1
    code, out, _ = run("rollout", "cancel", "AppSet-dotnet8-1a2b3c4d5e6f-1")
    assert code == 0 and "cancelled" in out
    assert sfn.describe_execution(executionArn=execution)["status"] == "ABORTED"
