"""Configuration store: catalog documents in DynamoDB (one item per document).

Table layout: partition key `kind` (Agent, Profile, ...), sort key `name`. Each item holds the full
document as JSON plus bookkeeping: `version` (optimistic locking), `updatedAt`, `updatedBy` and
`managedBy` ("patchgate" for built-in content seeded on deploy, "user" for everything else).
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from patchgate.plan import BASELINE_ENGINES, KINDS, Document, Library

BUILT_IN = "patchgate"
USER = "user"


class StoreError(Exception):
    pass


class VersionConflictError(StoreError):
    """The item changed since it was read (optimistic locking)."""


@dataclass
class Record:
    document: Document
    version: int
    updated_at: str
    updated_by: str
    managed_by: str


def _record(item: dict[str, Any]) -> Record:
    return Record(
        document=json.loads(item["document"]),
        version=int(item.get("version", 0)),
        updated_at=str(item.get("updatedAt", "")),
        updated_by=str(item.get("updatedBy", "")),
        managed_by=str(item.get("managedBy", USER)),
    )


class ConfigStore:
    def __init__(self, table_name: str, session: boto3.Session | None = None) -> None:
        self.table = (session or boto3.Session()).resource("dynamodb").Table(table_name)

    # ------------------------------------------------------------ reads

    def get(self, kind: str, name: str) -> Record | None:
        item = self.table.get_item(Key={"kind": kind, "name": name}, ConsistentRead=True).get(
            "Item"
        )
        return _record(item) if item else None

    def list(self, kind: str) -> list[Record]:
        records: list[Record] = []
        kwargs: dict[str, Any] = {"KeyConditionExpression": Key("kind").eq(kind)}
        while True:
            page = self.table.query(**kwargs)
            records.extend(_record(item) for item in page.get("Items", []))
            if "LastEvaluatedKey" not in page:
                return records
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    def get_many(self, kind: str, names: Iterable[str]) -> dict[str, Record]:
        found: dict[str, Record] = {}
        for name in dict.fromkeys(names):
            record = self.get(kind, name)
            if record:
                found[name] = record
        return found

    def library_for(self, profile_name: str, appsets: Iterable[str] = ()) -> Library:
        """Load only the documents a profile (plus requested app sets) needs."""
        library = Library()
        profile = self.get("Profile", profile_name)
        if profile is None:
            return library
        library.add(profile.document)
        spec = profile.document["spec"]
        for record in self.get_many("Agent", spec.get("agents", [])).values():
            library.add(record.document)
        hardening = spec.get("hardening") or {}
        if hardening.get("engine") in BASELINE_ENGINES and hardening.get("baseline"):
            baseline = self.get("Baseline", hardening["baseline"])
            if baseline:
                library.add(baseline.document)
        elif hardening:
            for record in self.list("Control"):
                library.add(record.document)
        # Allowed sets are loaded too, so references in the profile are validated on every use.
        wanted = [*spec.get("allowedAppSets", []), *appsets]
        for record in self.get_many("AppSet", wanted).values():
            library.add(record.document)
        return library

    # ------------------------------------------------------------ writes

    def put(
        self,
        document: Document,
        updated_by: str,
        *,
        managed_by: str = USER,
        expected_version: int | None = None,
    ) -> int:
        """Create or replace a document. With expected_version, fail if it changed meanwhile."""
        kind, name = document["kind"], document["metadata"]["name"]
        if kind not in KINDS:
            raise StoreError(f"unknown kind {kind}")
        current = self.get(kind, name)
        version = (current.version if current else 0) + 1
        condition = "attribute_not_exists(#k)"
        values: dict[str, Any] = {}
        if current is not None:
            condition = "#v = :expected"
            values[":expected"] = current.version if expected_version is None else expected_version
        try:
            self.table.put_item(
                Item={
                    "kind": kind,
                    "name": name,
                    "document": json.dumps(document, sort_keys=True),
                    "version": version,
                    "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "updatedBy": updated_by,
                    "managedBy": managed_by,
                },
                ConditionExpression=condition,
                ExpressionAttributeNames={"#v": "version"} if current else {"#k": "kind"},
                **({"ExpressionAttributeValues": values} if values else {}),
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise VersionConflictError(f"{kind}/{name} changed since it was read") from exc
            raise
        return version

    def delete(self, kind: str, name: str) -> bool:
        response = self.table.delete_item(Key={"kind": kind, "name": name}, ReturnValues="ALL_OLD")
        return "Attributes" in response
