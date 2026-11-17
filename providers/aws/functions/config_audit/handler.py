"""Audit trail for the configuration table (DynamoDB Streams consumer).

Every insert, update and delete of a catalog document is written to the history table with the
previous and new document, version and author, so changes can be reviewed and rolled back.
New documents are validated: an invalid document (for example edited by hand in the console)
triggers an alert. The resolver refuses invalid documents regardless.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import boto3

from patchgate.schema import validate_document

LOG = logging.getLogger()
LOG.setLevel(logging.INFO)

HISTORY_TABLE = os.environ.get("PATCHGATE_HISTORY_TABLE", "patchgate-config-history")
ALERT_TOPIC = os.environ.get("PATCHGATE_ALERT_TOPIC", "")


def _image(record: dict[str, Any], key: str) -> dict[str, str]:
    """Flatten a DynamoDB stream image ({"S": ...} / {"N": ...}) into plain strings."""
    image = record["dynamodb"].get(key) or {}
    return {name: next(iter(value.values())) for name, value in image.items()}


def handler(event: dict[str, Any], context: Any = None) -> dict[str, int]:
    history = boto3.resource("dynamodb").Table(HISTORY_TABLE)
    invalid = 0
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
        if record["eventName"] != "REMOVE":
            try:
                errors = validate_document(json.loads(new["document"]), f"{kind}/{name}")
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
