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

HANDLER = Path(__file__).resolve().parents[1] / "functions" / "config_audit" / "handler.py"
REGION = "us-east-2"


@pytest.fixture
def audit(monkeypatch: pytest.MonkeyPatch) -> Iterator[ModuleType]:
    for key, value in {
        "AWS_DEFAULT_REGION": REGION,
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "PATCHGATE_HISTORY_TABLE": "history",
    }.items():
        monkeypatch.setenv(key, value)
    with mock_aws():
        boto3.client("dynamodb").create_table(
            TableName="history",
            BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[
                {"AttributeName": "id", "AttributeType": "S"},
                {"AttributeName": "changeId", "AttributeType": "S"},
            ],
            KeySchema=[
                {"AttributeName": "id", "KeyType": "HASH"},
                {"AttributeName": "changeId", "KeyType": "RANGE"},
            ],
        )
        for name, keys in (
            ("patchgate-config", [("kind", "HASH"), ("name", "RANGE")]),
            ("patchgate-releases", [("id", "HASH")]),
        ):
            boto3.client("dynamodb").create_table(
                TableName=name,
                BillingMode="PAY_PER_REQUEST",
                AttributeDefinitions=[{"AttributeName": k, "AttributeType": "S"} for k, _ in keys],
                KeySchema=[{"AttributeName": k, "KeyType": t} for k, t in keys],
            )
        topic = boto3.client("sns").create_topic(Name="alerts")["TopicArn"]
        monkeypatch.setenv("PATCHGATE_ALERT_TOPIC", topic)
        spec = importlib.util.spec_from_file_location("audit_handler", HANDLER)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules["audit_handler"] = module
        spec.loader.exec_module(module)
        yield module


def appset(apps: list[dict[str, str]]) -> str:
    return json.dumps(
        {
            "apiVersion": "patchgate.io/v1alpha1",
            "kind": "AppSet",
            "metadata": {"name": "tools", "description": "d"},
            "spec": {"platform": "linux", "apps": apps},
        }
    )


def record(event: str, new: str | None, old: str | None, version: int, by: str) -> Any:
    def image(doc: str | None) -> dict[str, Any] | None:
        if doc is None:
            return None
        return {
            "kind": {"S": "AppSet"},
            "name": {"S": "tools"},
            "document": {"S": doc},
            "version": {"N": str(version)},
            "updatedBy": {"S": by},
            "managedBy": {"S": "user"},
        }

    dynamodb: dict[str, Any] = {"ApproximateCreationDateTime": 1_700_000_000 + version}
    if new is not None:
        dynamodb["NewImage"] = image(new)
    if old is not None:
        dynamodb["OldImage"] = image(old)
    return {"eventName": event, "dynamodb": dynamodb}


def test_changes_are_recorded_with_previous_version(audit: ModuleType) -> None:
    v1, v2 = appset([{"name": "jq"}]), appset([{"name": "jq"}, {"name": "git"}])
    result = audit.handler(
        {
            "Records": [
                record("INSERT", v1, None, 1, "alice"),
                record("MODIFY", v2, v1, 2, "bob"),
            ]
        }
    )
    assert result == {"records": 2, "invalid": 0}
    items = boto3.resource("dynamodb").Table("history").scan()["Items"]
    by_version = {int(i["version"]): i for i in items}
    assert by_version[2]["previous"] == v1 and by_version[2]["updatedBy"] == "bob"
    assert {i["id"] for i in items} == {"AppSet#tools"}


def test_invalid_document_raises_an_alert(audit: ModuleType) -> None:
    result = audit.handler({"Records": [record("MODIFY", appset([]), None, 3, "")]})
    assert result["invalid"] == 1
    item = boto3.resource("dynamodb").Table("history").scan()["Items"][0]
    assert item["updatedBy"].startswith("unknown")


def test_deletes_are_recorded(audit: ModuleType) -> None:
    result = audit.handler({"Records": [record("REMOVE", None, appset([{"name": "jq"}]), 4, "x")]})
    assert result == {"records": 1, "invalid": 0}
    item = boto3.resource("dynamodb").Table("history").scan()["Items"][0]
    assert item["event"] == "REMOVE" and item["previous"]


def put_default_policy() -> None:
    from patchgate.catalog import Catalog
    from patchgate_aws.store import ConfigStore

    catalog = Catalog.load(Path(__file__).resolve().parents[3] / "core" / "catalog")
    ConfigStore("patchgate-config").put(catalog.policies["default"], "seed")


def released() -> dict[str, Any]:
    item = (
        boto3.resource("dynamodb").Table("patchgate-releases").get_item(Key={"id": "AppSet#tools"})
    )
    return dict(item.get("Item", {}).get("released", {}))


def test_gated_change_outside_the_cli_pins_the_previous_revision(audit: ModuleType) -> None:
    from patchgate.rollout import revision_of

    put_default_policy()
    v1, v2 = appset([{"name": "jq"}]), appset([{"name": "jq"}, {"name": "git"}])
    audit.handler({"Records": [record("MODIFY", v2, v1, 2, "console")]})
    assert released() == {"*": revision_of(v1)}
    v3 = appset([{"name": "git"}])
    audit.handler({"Records": [record("MODIFY", v3, v2, 3, "console")]})
    assert released() == {"*": revision_of(v1)}  # a rollout (or the CLI) already owns it


def test_no_pin_without_a_rollout_policy(audit: ModuleType) -> None:
    v1, v2 = appset([{"name": "jq"}]), appset([{"name": "git"}])
    audit.handler({"Records": [record("MODIFY", v2, v1, 2, "console")]})
    assert released() == {}


def test_accounts_are_validated_with_the_provider_schema(audit: ModuleType) -> None:
    def account(spec: dict[str, Any]) -> dict[str, Any]:
        doc = json.dumps(
            {
                "apiVersion": "patchgate.io/v1alpha1",
                "kind": "Account",
                "metadata": {"name": "123456789012"},
                "spec": spec,
            }
        )
        rec = record("INSERT", doc, None, 1, "cli")
        rec["dynamodb"]["NewImage"]["kind"] = {"S": "Account"}
        return dict(rec)

    ok = audit.handler({"Records": [account({"ring": 1, "approvers": "platform"})]})
    bad = audit.handler({"Records": [account({"ring": 12, "approvers": "platform"})]})
    assert (ok["invalid"], bad["invalid"]) == (0, 1)
