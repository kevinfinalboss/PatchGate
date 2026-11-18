"""Resolve a launched EC2 instance into everything the bootstrap workflow needs.

Runs in the hub account. Assumes Patchgate-Execution in the account that owns the
instance, reads its tags and SSM status, loads the profile and requested app sets from the
configuration table (DynamoDB), validates them and composes the install plan at run time.
Invalid or inconsistent configuration is refused, never applied.

Returns a flat dict consumed by the Step Functions bootstrap workflow. Every key is always
present so Choice states never hit a missing path.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any

import boto3

from patchgate.plan import CatalogError, encode_plan
from patchgate.schema import validate_document
from patchgate_aws.store import ConfigStore

LOG = logging.getLogger()
LOG.setLevel(logging.INFO)

EXECUTION_ROLE_NAME = os.environ.get("EXECUTION_ROLE_NAME", "Patchgate-Execution")
PARTITION = os.environ.get("AWS_PARTITION", "aws")
CONFIG_TABLE = os.environ.get("PATCHGATE_CONFIG_TABLE", "patchgate-config")
LOCKS_TABLE = os.environ.get("PATCHGATE_LOCKS_TABLE", "patchgate-locks")
LOCK_TTL_SECONDS = 2 * 3600  # a crashed execution never blocks an instance for longer

TAG_MANAGED = "Patchgate:Managed"
TAG_PROFILE = "Patchgate:Profile"
TAG_APPSETS = "Patchgate:AppSets"
TAG_STATUS = "Patchgate:BootstrapStatus"
TAG_DIGEST = "Patchgate:BootstrapDigest"

DEFAULT_PROFILES = {"linux": "base-linux", "windows": "base-windows"}
DOCUMENTS = {"linux": "Patchgate-Bootstrap-Linux", "windows": "Patchgate-Bootstrap-Windows"}

_ACCOUNT = re.compile(r"^\d{12}$")
_INSTANCE = re.compile(r"^i-[0-9a-f]{8,17}$")
_NAME = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
_PROFILE = _NAME


class ResolveError(Exception):
    """Instance cannot be bootstrapped (bad input, unknown profile, platform mismatch)."""


def parse_appsets(value: str) -> list[str]:
    names = [part.strip() for part in value.split(",") if part.strip()]
    for name in names:
        _require(_NAME, name, "app set")
    return names


def compose_plan(store: ConfigStore, profile: str, appsets: list[str], platform: str) -> Any:
    """Load, validate and compile; any problem becomes a ResolveError listing every issue."""
    library = store.library_for(profile, appsets)
    if profile not in library.profiles:
        raise ResolveError(f"unknown profile {profile!r}")
    errors = [
        error
        for kind in ("Profile", "Agent", "Control", "Baseline", "AppSet")
        for name, doc in library.bucket(kind).items()
        for error in validate_document(doc, f"{kind}/{name}")
    ]
    errors += library.reference_errors()
    if errors:
        raise ResolveError("invalid configuration: " + "; ".join(errors[:10]))
    profile_platform = library.profiles[profile]["spec"]["platform"]
    if profile_platform != platform:
        raise ResolveError(f"profile {profile!r} is for {profile_platform}, instance is {platform}")
    try:
        plan = library.compile(profile, appsets)
        return plan, encode_plan(plan)
    except CatalogError as exc:
        raise ResolveError("; ".join(exc.errors)) from exc


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


def acquire_lock(account_id: str, instance_id: str, owner: str) -> bool:
    """One run per instance at a time (launch and tag events arrive together).

    Re-entrant for the same execution; stale locks expire after LOCK_TTL_SECONDS.
    """
    now = int(time.time())
    try:
        boto3.client("dynamodb").put_item(
            TableName=LOCKS_TABLE,
            Item={
                "lockId": {"S": f"{account_id}#{instance_id}"},
                "owner": {"S": owner},
                "expiresAt": {"N": str(now + LOCK_TTL_SECONDS)},
            },
            ConditionExpression="attribute_not_exists(lockId) OR #o = :me OR expiresAt < :now",
            ExpressionAttributeNames={"#o": "owner"},
            ExpressionAttributeValues={":me": {"S": owner}, ":now": {"N": str(now)}},
        )
        return True
    except boto3.client("dynamodb").exceptions.ConditionalCheckFailedException:
        return False


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
        "appSets": [],
        "digest": "",
        "documentName": "",
        "documentParameters": {},
        "lockId": f"{account_id}#{instance_id}",
        "lockBusy": False,
    }

    session = spoke_session(account_id, region)
    instance = describe_instance(session, instance_id)
    tags = {t["Key"]: t["Value"] for t in instance.get("Tags", [])}
    if tags.get(TAG_MANAGED, "").lower() != "true":
        LOG.info("instance %s is not managed; skipping", instance_id)
        return result

    platform = "windows" if instance.get("Platform", "").lower() == "windows" else "linux"
    profile = _require(_PROFILE, tags.get(TAG_PROFILE, DEFAULT_PROFILES[platform]), "profile")
    appsets = parse_appsets(tags.get(TAG_APPSETS, ""))
    plan, encoded = compose_plan(ConfigStore(CONFIG_TABLE), profile, appsets, platform)

    result.update(
        managed=True,
        platform=platform,
        profile=profile,
        appSets=appsets,
        digest=plan["digest"],
        alreadyBootstrapped=(
            tags.get(TAG_STATUS) == "Success" and tags.get(TAG_DIGEST) == plan["digest"]
        ),
        ssmOnline=ssm_ping_status(session, instance_id) == "Online",
        documentName=DOCUMENTS[platform],
        documentParameters={"Plan": [encoded]},
    )
    if result["ssmOnline"] and not result["alreadyBootstrapped"]:
        owner = str(event.get("executionName") or "unknown")
        result["lockBusy"] = not acquire_lock(account_id, instance_id, owner)
    LOG.info(
        json.dumps({k: v for k, v in result.items() if k != "documentParameters"}, default=str)
    )
    return result
