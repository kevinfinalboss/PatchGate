"""Resolve a launched EC2 instance into everything the bootstrap workflow needs.

Runs in the hub account. Assumes Patchgate-Execution in the account that owns the
instance, reads its tags and SSM status, and picks the pre-compiled install plan for
its profile from plans.json (generated from core/catalog by `make plans`).

Returns a flat dict consumed by the Step Functions bootstrap workflow. Every key is always
present so Choice states never hit a missing path.
"""

from __future__ import annotations

import json
import logging
import os
import re
from functools import cache
from pathlib import Path
from typing import Any

import boto3

LOG = logging.getLogger()
LOG.setLevel(logging.INFO)

EXECUTION_ROLE_NAME = os.environ.get("EXECUTION_ROLE_NAME", "Patchgate-Execution")
PARTITION = os.environ.get("AWS_PARTITION", "aws")
PLANS_FILE = Path(__file__).with_name("plans.json")

TAG_MANAGED = "Patchgate:Managed"
TAG_PROFILE = "Patchgate:Profile"
TAG_STATUS = "Patchgate:BootstrapStatus"
TAG_DIGEST = "Patchgate:BootstrapDigest"

DEFAULT_PROFILES = {"linux": "base-linux", "windows": "base-windows"}
DOCUMENTS = {"linux": "Patchgate-Bootstrap-Linux", "windows": "Patchgate-Bootstrap-Windows"}

_ACCOUNT = re.compile(r"^\d{12}$")
_INSTANCE = re.compile(r"^i-[0-9a-f]{8,17}$")
_PROFILE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")


class ResolveError(Exception):
    """Instance cannot be bootstrapped (bad input, unknown profile, platform mismatch)."""


@cache
def load_plans() -> dict[str, dict[str, str]]:
    plans: dict[str, dict[str, str]] = json.loads(PLANS_FILE.read_text())
    return plans


def _require(pattern: re.Pattern[str], value: Any, field: str) -> str:
    if not isinstance(value, str) or not pattern.match(value):
        raise ResolveError(f"invalid {field}: {value!r}")
    return value


def spoke_session(account_id: str, region: str) -> boto3.Session:
    role_arn = f"arn:{PARTITION}:iam::{account_id}:role/{EXECUTION_ROLE_NAME}"
    creds = boto3.client("sts").assume_role(RoleArn=role_arn, RoleSessionName="patchgate-resolver")[
        "Credentials"
    ]
    return boto3.Session(
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
        region_name=region,
    )


def describe_instance(session: boto3.Session, instance_id: str) -> dict[str, Any]:
    reservations = session.client("ec2").describe_instances(InstanceIds=[instance_id])[
        "Reservations"
    ]
    if not reservations or not reservations[0]["Instances"]:
        raise ResolveError(f"instance {instance_id} not found")
    instance: dict[str, Any] = reservations[0]["Instances"][0]
    return instance


def ssm_ping_status(session: boto3.Session, instance_id: str) -> str:
    info = session.client("ssm").describe_instance_information(
        Filters=[{"Key": "InstanceIds", "Values": [instance_id]}]
    )["InstanceInformationList"]
    return str(info[0].get("PingStatus", "Unknown")) if info else "NotRegistered"


def handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    account_id = _require(_ACCOUNT, event.get("accountId"), "accountId")
    instance_id = _require(_INSTANCE, event.get("instanceId"), "instanceId")
    region = os.environ.get("AWS_REGION", "")
    if event.get("region") != region:
        raise ResolveError(f"event region {event.get('region')!r} != hub region {region!r}")

    result: dict[str, Any] = {
        "accountId": account_id,
        "instanceId": instance_id,
        "roleArn": f"arn:{PARTITION}:iam::{account_id}:role/{EXECUTION_ROLE_NAME}",
        "managed": False,
        "alreadyBootstrapped": False,
        "ssmOnline": False,
        "platform": "",
        "profile": "",
        "digest": "",
        "documentName": "",
        "documentParameters": {},
    }

    session = spoke_session(account_id, region)
    instance = describe_instance(session, instance_id)
    tags = {t["Key"]: t["Value"] for t in instance.get("Tags", [])}
    if tags.get(TAG_MANAGED, "").lower() != "true":
        LOG.info("instance %s is not managed; skipping", instance_id)
        return result

    platform = "windows" if instance.get("Platform", "").lower() == "windows" else "linux"
    profile = _require(_PROFILE, tags.get(TAG_PROFILE, DEFAULT_PROFILES[platform]), "profile")
    entry = load_plans().get(profile)
    if entry is None:
        raise ResolveError(f"unknown profile {profile!r}")
    if entry["platform"] != platform:
        raise ResolveError(
            f"profile {profile!r} is for {entry['platform']}, instance is {platform}"
        )

    result.update(
        managed=True,
        platform=platform,
        profile=profile,
        digest=entry["digest"],
        alreadyBootstrapped=(
            tags.get(TAG_STATUS) == "Success" and tags.get(TAG_DIGEST) == entry["digest"]
        ),
        ssmOnline=ssm_ping_status(session, instance_id) == "Online",
        documentName=DOCUMENTS[platform],
        documentParameters={"Plan": [entry["plan"]]},
    )
    LOG.info(
        json.dumps({k: v for k, v in result.items() if k != "documentParameters"}, default=str)
    )
    return result
