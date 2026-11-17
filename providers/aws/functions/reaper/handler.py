"""TTL reaper: terminates lab instances that outlived their time-to-live.

Runs on a schedule in the lab account. Only instances tagged Patchgate:Lab=true are
considered; the TTL comes from the Patchgate:TTL tag (hours) or DEFAULT_TTL_HOURS.
The IAM policy enforces the same tag condition, so the reaper cannot touch anything else.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime, timedelta
from typing import Any

import boto3

LOG = logging.getLogger()
LOG.setLevel(logging.INFO)

TAG_LAB = "Patchgate:Lab"
TAG_TTL = "Patchgate:TTL"


def ttl_hours(tags: dict[str, str], default: float) -> float:
    try:
        value = float(tags.get(TAG_TTL, default))
    except ValueError:
        return default
    return value if 0 < value <= 24 else default


def expired_instances(
    reservations: list[dict[str, Any]], now: datetime, default_ttl: float
) -> list[str]:
    expired = []
    for reservation in reservations:
        for instance in reservation["Instances"]:
            tags = {t["Key"]: t["Value"] for t in instance.get("Tags", [])}
            age = now - instance["LaunchTime"]
            if age > timedelta(hours=ttl_hours(tags, default_ttl)):
                expired.append(instance["InstanceId"])
    return expired


def handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    ec2 = boto3.client("ec2")
    default_ttl = float(os.environ.get("DEFAULT_TTL_HOURS", "2"))
    paginator = ec2.get_paginator("describe_instances")
    reservations: list[dict[str, Any]] = []
    for page in paginator.paginate(
        Filters=[
            {"Name": f"tag:{TAG_LAB}", "Values": ["true"]},
            {
                "Name": "instance-state-name",
                "Values": ["pending", "running", "stopping", "stopped"],
            },
        ]
    ):
        reservations.extend(page["Reservations"])

    expired = expired_instances(reservations, datetime.now(UTC), default_ttl)
    if expired:
        LOG.warning("terminating expired lab instances: %s", expired)
        ec2.terminate_instances(InstanceIds=expired)
    else:
        LOG.info("no expired lab instances")
    return {"terminated": expired}
