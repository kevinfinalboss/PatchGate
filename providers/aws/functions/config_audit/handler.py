"""Audit trail for the configuration table (DynamoDB Streams consumer).

Every insert, update and delete of a catalog document is written to the history table with the
previous and new document, version and author, so changes can be reviewed and rolled back.
New documents are validated: an invalid document (for example edited by hand in the console)
triggers an alert. The resolver refuses invalid documents regardless.

Safety net for gated kinds (ADR 0010): when a profile or app set changes without releases
recorded (edited in the console or re-seeded by a deploy instead of through patchgate-cli), the
previous revision is pinned for every ring so the change waits for a rollout.

Every content change of a gated document then starts the patchgate-rollout workflow.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import boto3

from patchgate.rollout import GATED_KINDS, revision_of
from patchgate_aws.store import ConfigStore, ReleaseStore
from patchgate_aws.validation import validate

LOG = logging.getLogger()
LOG.setLevel(logging.INFO)

HISTORY_TABLE = os.environ.get("PATCHGATE_HISTORY_TABLE", "patchgate-config-history")
ALERT_TOPIC = os.environ.get("PATCHGATE_ALERT_TOPIC", "")
CONFIG_TABLE = os.environ.get("PATCHGATE_CONFIG_TABLE", "patchgate-config")
RELEASES_TABLE = os.environ.get("PATCHGATE_RELEASES_TABLE", "patchgate-releases")
ROLLOUT_POLICY = os.environ.get("PATCHGATE_ROLLOUT_POLICY", "default")
ROLLOUT_STATE_MACHINE = os.environ.get("PATCHGATE_ROLLOUT_STATE_MACHINE", "")


def gated_kinds() -> tuple[str, ...]:
    policy = ConfigStore(CONFIG_TABLE).get("RolloutPolicy", ROLLOUT_POLICY)
    if policy is None:
        return ()
    return tuple(policy.document.get("spec", {}).get("kinds", GATED_KINDS))


def start_rollout(kind: str, name: str, new_document: str, changed_at: float) -> None:
    if not ROLLOUT_STATE_MACHINE:
        return
    try:
        invalid = validate(json.loads(new_document))
    except json.JSONDecodeError:
        invalid = ["not JSON"]
    if invalid:
        LOG.warning("%s/%s is invalid; no rollout (an alert is sent)", kind, name)
        return
    revision = revision_of(new_document)
    # Execution names: kind-name prefix (used to find older rollouts) + revision + time
    prefix = f"{kind}-{name}"[:48]
    execution = f"{prefix}-{revision[7:19]}-{int(changed_at)}"
    try:
        boto3.client("stepfunctions").start_execution(
            stateMachineArn=ROLLOUT_STATE_MACHINE,
            name=execution,
            input=json.dumps({"kind": kind, "name": name, "revision": revision}),
        )
        LOG.info("rollout %s started for %s/%s", execution, kind, name)
    except boto3.client("stepfunctions").exceptions.ExecutionAlreadyExists:
        LOG.info("rollout %s already started (stream retry)", execution)


def pin_previous(kind: str, name: str, old_document: str) -> None:
    pinned = ReleaseStore(RELEASES_TABLE).pin(
        kind, name, revision_of(old_document), "config-audit (changed outside patchgate-cli)"
    )
    if pinned:
        LOG.warning("%s/%s changed outside patchgate-cli; previous revision pinned", kind, name)


def _image(record: dict[str, Any], key: str) -> dict[str, str]:
    """Flatten a DynamoDB stream image ({"S": ...} / {"N": ...}) into plain strings."""
    image = record["dynamodb"].get(key) or {}
    return {name: next(iter(value.values())) for name, value in image.items()}


def handler(event: dict[str, Any], context: Any = None) -> dict[str, int]:
    history = boto3.resource("dynamodb").Table(HISTORY_TABLE)
    invalid = 0
    gated: tuple[str, ...] | None = None
    for record in event.get("Records", []):
        new, old = _image(record, "NewImage"), _image(record, "OldImage")
        current = new or old
        kind, name = current["kind"], current["name"]
        version = new.get("version") or old.get("version") or "0"
        changed_at = float(record["dynamodb"].get("ApproximateCreationDateTime", 0))
        history.put_item(
            Item={
                "id": f"{kind}#{name}",
                "changeId": f"{changed_at:.0f}#{version}",
                "event": record["eventName"],
                "version": int(version),
                "updatedBy": new.get("updatedBy", "") or "unknown (direct table edit?)",
                "managedBy": new.get("managedBy", old.get("managedBy", "")),
                "document": new.get("document", ""),
                "previous": old.get("document", ""),
            }
        )
        if record["eventName"] == "MODIFY" and new.get("document") != old.get("document"):
            gated = gated_kinds() if gated is None else gated
            if kind in gated and old.get("document"):
                pin_previous(kind, name, old["document"])
                start_rollout(kind, name, new["document"], changed_at)
        if record["eventName"] != "REMOVE":
            try:
                errors = validate(json.loads(new["document"]), f"{kind}/{name}")
            except (KeyError, json.JSONDecodeError) as exc:
                errors = [f"{kind}/{name}: document is not valid JSON ({exc})"]
            if errors:
                invalid += 1
                LOG.error("invalid configuration written: %s", errors)
                if ALERT_TOPIC:
                    boto3.client("sns").publish(
                        TopicArn=ALERT_TOPIC,
                        Subject="[Patchgate] Invalid configuration written"[:100],
                        Message=(
                            f"{kind}/{name} (version {version}, by "
                            f"{new.get('updatedBy') or 'unknown'}) is invalid and will be refused "
                            "by the resolver until fixed:\n\n" + "\n".join(errors)
                        ),
                    )
    return {"records": len(event.get("Records", [])), "invalid": invalid}
