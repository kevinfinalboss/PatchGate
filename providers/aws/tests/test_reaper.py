from __future__ import annotations

import importlib.util
import sys
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

import boto3
import pytest
from moto import mock_aws

HANDLER = Path(__file__).resolve().parents[1] / "functions" / "reaper" / "handler.py"
NOW = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)


@pytest.fixture
def reaper(monkeypatch: pytest.MonkeyPatch) -> Iterator[ModuleType]:
    for key, value in {
        "AWS_DEFAULT_REGION": "us-east-2",
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
    }.items():
        monkeypatch.setenv(key, value)
    spec = importlib.util.spec_from_file_location("reaper_handler", HANDLER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["reaper_handler"] = module
    spec.loader.exec_module(module)
    with mock_aws():
        yield module


def instance(age_hours: float, **tags: str) -> dict[str, Any]:
    return {
        "InstanceId": f"i-{abs(hash((age_hours, tuple(tags.items())))):017x}"[:19],
        "LaunchTime": NOW - timedelta(hours=age_hours),
        "Tags": [{"Key": k, "Value": v} for k, v in tags.items()],
    }


def test_expired_by_default_ttl(reaper: ModuleType) -> None:
    old, young = instance(3), instance(1)
    result = reaper.expired_instances([{"Instances": [old, young]}], NOW, 2.0)
    assert result == [old["InstanceId"]]


def test_ttl_tag_overrides_default(reaper: ModuleType) -> None:
    keep = instance(3, **{"Patchgate:TTL": "4"})
    kill = instance(1, **{"Patchgate:TTL": "0.5"})
    result = reaper.expired_instances([{"Instances": [keep, kill]}], NOW, 2.0)
    assert result == [kill["InstanceId"]]


@pytest.mark.parametrize("value", ["abc", "-1", "0", "999"])
def test_invalid_ttl_falls_back_to_default(reaper: ModuleType, value: str) -> None:
    assert reaper.ttl_hours({"Patchgate:TTL": value}, 2.0) == 2.0


def test_handler_only_terminates_lab_instances(reaper: ModuleType) -> None:
    ec2 = boto3.client("ec2", region_name="us-east-2")
    image = ec2.describe_images()["Images"][0]["ImageId"]

    def launch(tags: dict[str, str]) -> str:
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

    lab = launch({"Patchgate:Lab": "true", "Patchgate:TTL": "0.0001"})
    other = launch({"Name": "not-lab"})
    import time

    time.sleep(1)
    result = reaper.handler({})
    assert result["terminated"] == [lab]
    states = {
        i["InstanceId"]: i["State"]["Name"]
        for r in ec2.describe_instances()["Reservations"]
        for i in r["Instances"]
    }
    assert states[other] == "running"
