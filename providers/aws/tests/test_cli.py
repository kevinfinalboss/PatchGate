from __future__ import annotations

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
    ):
        client.create_table(
            TableName=name,
            BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[
                {"AttributeName": hash_key, "AttributeType": "S"},
                {"AttributeName": range_key, "AttributeType": "S"},
            ],
            KeySchema=[
                {"AttributeName": hash_key, "KeyType": "HASH"},
                {"AttributeName": range_key, "KeyType": "RANGE"},
            ],
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
    original_out, original_err = cli.sys.stdout, cli.sys.stderr
    cli.sys.stdout, cli.sys.stderr = out, err
    try:
        code = cli.main(["--region", REGION, *argv])
    finally:
        cli.sys.stdout, cli.sys.stderr = original_out, original_err
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
    bad["spec"] = {"platform": "windows", "apps": []}  # type: ignore[assignment]
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
