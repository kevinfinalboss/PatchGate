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
ACCOUNT = "123456789012"
REGION = "us-east-2"


@pytest.fixture
def resolver(monkeypatch: pytest.MonkeyPatch) -> Iterator[ModuleType]:
    monkeypatch.setenv("AWS_REGION", REGION)
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    spec = importlib.util.spec_from_file_location("resolver_handler", HANDLER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["resolver_handler"] = module
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "ssm_ping_status", lambda session, iid: "Online")
    with mock_aws():
        yield module


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
    digest = resolver.load_plans()["base-linux"]["digest"]
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


def test_plans_json_is_up_to_date_with_catalog() -> None:
    from patchgate.catalog import Catalog

    catalog = Catalog.load(Path(__file__).resolve().parents[3] / "core" / "catalog")
    import json

    bundled = json.loads((HANDLER.parent / "plans.json").read_text())
    expected = {name: plan["digest"] for name, plan in catalog.compile_all().items()}
    assert {name: entry["digest"] for name, entry in bundled.items()} == expected, (
        "plans.json is stale: run `make plans`"
    )
