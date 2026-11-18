"""Configuration store: catalog documents in DynamoDB (one item per document).

Table layout: partition key `kind` (Agent, Profile, ...), sort key `name`. Each item holds the full
document as JSON plus bookkeeping: `version` (optimistic locking), `updatedAt`, `updatedBy` and
`managedBy` ("patchgate" for built-in content seeded on deploy, "user" for everything else).

Gated kinds (profiles and app sets, ADR 0010) are served per ring and approver group: the
releases table records which revision (content digest) each unit received, and older revisions
are read back from the history table.
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
from patchgate.rollout import ALL_UNITS, Placement, revision_for, revision_of

BUILT_IN = "patchgate"
USER = "user"
# Provider-side kinds kept in the same table (never in Git: they hold account IDs).
PROVIDER_KINDS = ("Account",)


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
    raw: str = ""  # the stored JSON string; its digest is the revision

    @property
    def revision(self) -> str:
        return str(revision_of(self.raw))


def _record(item: dict[str, Any]) -> Record:
    return Record(
        raw=str(item["document"]),
        document=json.loads(item["document"]),
        version=int(item.get("version", 0)),
        updated_at=str(item.get("updatedAt", "")),
        updated_by=str(item.get("updatedBy", "")),
        managed_by=str(item.get("managedBy", USER)),
    )


class ReleaseStore:
    """Per gated document: {unit: revision} plus "*" (ADR 0010). No item = served as stored."""

    def __init__(self, table_name: str, session: boto3.Session | None = None) -> None:
        self.table = (session or boto3.Session()).resource("dynamodb").Table(table_name)

    def get(self, kind: str, name: str) -> dict[str, str]:
        item = self.table.get_item(Key={"id": f"{kind}#{name}"}, ConsistentRead=True).get("Item")
        return {str(k): str(v) for k, v in (item or {}).get("released", {}).items()}

    def pin(self, kind: str, name: str, revision: str, by: str) -> bool:
        """Freeze the revision every unit gets before a gated document changes. Returns False
        when releases already exist (a rollout owns them)."""
        try:
            self.table.put_item(
                Item={
                    "id": f"{kind}#{name}",
                    "released": {ALL_UNITS: revision},
                    "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "updatedBy": by,
                },
                ConditionExpression="attribute_not_exists(id)",
            )
            return True
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise

    def put(self, kind: str, name: str, released: dict[str, str], by: str) -> None:
        """Replace the releases of a document (one rollout stage writes at a time)."""
        self.table.put_item(
            Item={
                "id": f"{kind}#{name}",
                "released": released,
                "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "updatedBy": by,
            }
        )


class ConfigStore:
    def __init__(
        self,
        table_name: str,
        session: boto3.Session | None = None,
        *,
        history_table: str | None = None,
        releases: ReleaseStore | None = None,
        gated: Iterable[str] = (),
    ) -> None:
        resource = (session or boto3.Session()).resource("dynamodb")
        self.table = resource.Table(table_name)
        self.history = resource.Table(history_table) if history_table else None
        self.releases = releases
        self.gated = frozenset(gated)

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

    def document_at(self, kind: str, name: str, revision: str) -> Record | None:
        """A document as it was at a revision (current item first, then the history table)."""
        current = self.get(kind, name)
        if current is not None and current.revision == revision:
            return current
        if self.history is None:
            return None
        kwargs: dict[str, Any] = {"KeyConditionExpression": Key("id").eq(f"{kind}#{name}")}
        while True:
            page = self.history.query(**kwargs)
            for item in page.get("Items", []):
                raw = str(item.get("document", ""))
                if raw and revision_of(raw) == revision:
                    return Record(
                        document=json.loads(raw),
                        version=int(item.get("version", 0)),
                        updated_at=str(item.get("changeId", "")),
                        updated_by=str(item.get("updatedBy", "")),
                        managed_by=str(item.get("managedBy", USER)),
                        raw=raw,
                    )
            if "LastEvaluatedKey" not in page:
                return None
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    def get_for(self, kind: str, name: str, placement: Placement | None) -> Record | None:
        """The revision released to the instance's ring and approver group (gated kinds)."""
        record = self.get(kind, name)
        if record is None or placement is None or kind not in self.gated or not self.releases:
            return record
        revision = revision_for(self.releases.get(kind, name), placement)
        if revision is None or revision == record.revision:
            return record
        released = self.document_at(kind, name, revision)
        if released is None:
            raise StoreError(f"{kind}/{name}: released revision {revision[:19]} is not in history")
        return released

    def library_for(
        self, profile_name: str, appsets: Iterable[str] = (), placement: Placement | None = None
    ) -> Library:
        """Load only the documents a profile (plus requested app sets) needs. With a placement,
        gated documents are the revisions released to that ring and approver group."""
        library = Library()
        profile = self.get_for("Profile", profile_name, placement)
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
        for set_name in dict.fromkeys(wanted):
            appset = self.get_for("AppSet", set_name, placement)
            if appset:
                library.add(appset.document)
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
        if kind not in KINDS and kind not in PROVIDER_KINDS:
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
