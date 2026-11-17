"""patchgate-cli: manage the Patchgate configuration table and instance assignments.

Inspired by the AWS Instance Scheduler CLI: configuration lives in DynamoDB, instances only carry
tags. Every write is validated against the JSON Schemas and the other documents first.

Examples:
    patchgate-cli list appsets
    patchgate-cli describe profile web-windows
    patchgate-cli apply -f my-appset.yaml
    patchgate-cli copy profile web-windows web-windows-api
    patchgate-cli history appset dotnet8
    patchgate-cli rollback appset dotnet8 --version 2
    patchgate-cli instance show i-0123456789abcdef0
    patchgate-cli instance appsets i-0123456789abcdef0 --add dotnet8
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import boto3
import yaml
from boto3.dynamodb.conditions import Key

from patchgate.plan import KINDS, CatalogError, Document, Library
from patchgate.schema import validate_document
from patchgate_aws.store import BUILT_IN, ConfigStore, Record, VersionConflictError

KIND_ALIASES = {
    "agent": "Agent",
    "agents": "Agent",
    "profile": "Profile",
    "profiles": "Profile",
    "control": "Control",
    "controls": "Control",
    "baseline": "Baseline",
    "baselines": "Baseline",
    "appset": "AppSet",
    "appsets": "AppSet",
}
TAG_PROFILE = "Patchgate:Profile"
TAG_APPSETS = "Patchgate:AppSets"
DEFAULT_PROFILES = {"linux": "base-linux", "windows": "base-windows"}


class CliError(Exception):
    pass


def kind_of(value: str) -> str:
    kind = KIND_ALIASES.get(value.lower())
    if kind is None:
        raise CliError(f"unknown kind {value!r} (use: {', '.join(sorted(set(KIND_ALIASES)))})")
    return kind


class Cli:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.session = boto3.Session(profile_name=args.profile, region_name=args.region)
        # Multi-account: the configuration lives in the hub, instances in workload accounts.
        self.config_session = boto3.Session(
            profile_name=args.config_profile or args.profile, region_name=args.region
        )
        self.store = ConfigStore(args.table, self.config_session)
        self.out = sys.stdout

    def print(self, text: str = "") -> None:
        self.out.write(text + "\n")

    def caller(self) -> str:
        try:
            return str(self.config_session.client("sts").get_caller_identity()["Arn"])
        except Exception:  # attribution is best effort
            return "unknown"

    # ------------------------------------------------------------ validation

    def full_library(self) -> Library:
        library = Library()
        for kind in KINDS:
            for record in self.store.list(kind):
                library.add(record.document)
        return library

    def validate(self, doc: Document) -> None:
        errors = validate_document(doc, f"{doc.get('kind')}/{doc.get('metadata', {}).get('name')}")
        if errors:
            raise CliError("invalid document:\n  " + "\n  ".join(errors))
        library = self.full_library()
        library.add(doc)
        problems = library.reference_errors()
        if problems:
            raise CliError("inconsistent with existing configuration:\n  " + "\n  ".join(problems))

    def protect_built_in(self, record: Record | None, action: str) -> None:
        if record and record.managed_by == BUILT_IN and not self.args.force:
            raise CliError(
                f"{record.document['kind']}/{record.document['metadata']['name']} is built-in "
                f"(managed by Patchgate releases); {action} would be reverted on the next deploy. "
                "Use 'copy' to customise it, or --force."
            )

    # ------------------------------------------------------------ commands

    def cmd_list(self) -> int:
        kind = kind_of(self.args.kind)
        records = sorted(self.store.list(kind), key=lambda r: r.document["metadata"]["name"])
        self.print(f"{'NAME':32} {'VERSION':>7}  {'MANAGED BY':10}  {'UPDATED':20}  UPDATED BY")
        for r in records:
            name = r.document["metadata"]["name"]
            self.print(
                f"{name:32} {r.version:>7}  {r.managed_by:10}  {r.updated_at:20}  {r.updated_by}"
            )
        return 0

    def cmd_describe(self) -> int:
        kind = kind_of(self.args.kind)
        record = self.store.get(kind, self.args.name)
        if record is None:
            raise CliError(f"{kind}/{self.args.name} not found")
        self.print(
            f"# version {record.version}, managed by {record.managed_by}, "
            f"updated {record.updated_at} by {record.updated_by}"
        )
        self.print(yaml.safe_dump(record.document, sort_keys=False).rstrip())
        return 0

    def cmd_apply(self) -> int:
        docs = [d for d in yaml.safe_load_all(Path(self.args.file).read_text()) if d]
        for doc in docs:
            self.validate(doc)
            kind, name = doc["kind"], doc["metadata"]["name"]
            current = self.store.get(kind, name)
            self.protect_built_in(current, "changing it")
            if current and current.document == doc:
                self.print(f"{kind}/{name}: unchanged (version {current.version})")
                continue
            if self.args.dry_run:
                self.print(f"{kind}/{name}: valid, would be {'updated' if current else 'created'}")
                continue
            managed_by = current.managed_by if current else "user"
            version = self.store.put(doc, self.caller(), managed_by=managed_by)
            self.print(f"{kind}/{name}: {'updated' if current else 'created'} (version {version})")
        return 0

    def cmd_copy(self) -> int:
        kind = kind_of(self.args.kind)
        source = self.store.get(kind, self.args.name)
        if source is None:
            raise CliError(f"{kind}/{self.args.name} not found")
        if self.store.get(kind, self.args.new_name):
            raise CliError(f"{kind}/{self.args.new_name} already exists")
        doc = json.loads(json.dumps(source.document))
        doc["metadata"]["name"] = self.args.new_name
        self.validate(doc)
        version = self.store.put(doc, self.caller())
        self.print(
            f"{kind}/{self.args.new_name}: created from {self.args.name} (version {version})"
        )
        return 0

    def cmd_delete(self) -> int:
        kind = kind_of(self.args.kind)
        record = self.store.get(kind, self.args.name)
        if record is None:
            raise CliError(f"{kind}/{self.args.name} not found")
        self.protect_built_in(record, "deleting it")
        library = self.full_library()
        library.bucket(kind).pop(self.args.name, None)
        users = sorted({e.split("'")[1] for e in library.reference_errors() if self.args.name in e})
        if users and not self.args.force:
            raise CliError(f"{kind}/{self.args.name} is still referenced by: {', '.join(users)}")
        self.store.delete(kind, self.args.name)
        self.print(f"{kind}/{self.args.name}: deleted (history keeps every version)")
        return 0

    def history_items(self, kind: str, name: str) -> list[dict[str, Any]]:
        table = self.config_session.resource("dynamodb").Table(self.args.history_table)
        items: list[dict[str, Any]] = table.query(
            KeyConditionExpression=Key("id").eq(f"{kind}#{name}")
        ).get("Items", [])
        return sorted(items, key=lambda i: str(i["changeId"]))

    def cmd_history(self) -> int:
        kind = kind_of(self.args.kind)
        items = self.history_items(kind, self.args.name)
        if not items:
            raise CliError(f"no history for {kind}/{self.args.name}")
        self.print(f"{'VERSION':>7}  {'EVENT':7}  {'CHANGE ID':22}  UPDATED BY")
        for item in items:
            self.print(
                f"{int(item['version']):>7}  {item['event']:7}  {item['changeId']:22}  "
                f"{item.get('updatedBy', '')}"
            )
        return 0

    def cmd_rollback(self) -> int:
        kind = kind_of(self.args.kind)
        items = [
            i
            for i in self.history_items(kind, self.args.name)
            if int(i["version"]) == self.args.version and i.get("document")
        ]
        if not items:
            raise CliError(f"version {self.args.version} of {kind}/{self.args.name} not found")
        doc = json.loads(items[-1]["document"])
        current = self.store.get(kind, self.args.name)
        self.protect_built_in(current, "rolling it back")
        self.validate(doc)
        version = self.store.put(doc, f"{self.caller()} (rollback to v{self.args.version})")
        self.print(f"{kind}/{self.args.name}: restored version {self.args.version} as {version}")
        return 0

    # ------------------------------------------------------------ instances

    def instance(self) -> tuple[Any, dict[str, Any], dict[str, str]]:
        ec2 = self.session.client("ec2")
        reservations = ec2.describe_instances(InstanceIds=[self.args.instance_id])["Reservations"]
        if not reservations:
            raise CliError(f"instance {self.args.instance_id} not found")
        instance = reservations[0]["Instances"][0]
        return ec2, instance, {t["Key"]: t["Value"] for t in instance.get("Tags", [])}

    def cmd_instance_show(self) -> int:
        _, instance, tags = self.instance()
        self.print(f"instance   {instance['InstanceId']} ({instance['State']['Name']})")
        for key in sorted(k for k in tags if k.startswith("Patchgate:")):
            self.print(f"{key:30} {tags[key]}")
        return 0

    def cmd_instance_appsets(self) -> int:
        ec2, instance, tags = self.instance()
        current = [s for s in tags.get(TAG_APPSETS, "").split(",") if s.strip()]
        wanted = list(current)
        if self.args.set is not None:
            wanted = [s.strip() for s in self.args.set.split(",") if s.strip()]
        for name in self.args.add or []:
            if name not in wanted:
                wanted.append(name)
        for name in self.args.remove or []:
            wanted = [s for s in wanted if s != name]
        platform = "windows" if instance.get("Platform", "").lower() == "windows" else "linux"
        profile = tags.get(TAG_PROFILE, DEFAULT_PROFILES[platform])
        library = self.store.library_for(profile, wanted)
        try:
            library.compile(profile, wanted)
        except (CatalogError, KeyError) as exc:
            raise CliError(f"cannot assign {wanted} with profile {profile}: {exc}") from exc
        if wanted == current:
            self.print(f"{TAG_APPSETS} unchanged: {','.join(current) or '(none)'}")
            return 0
        if wanted:
            ec2.create_tags(
                Resources=[instance["InstanceId"]],
                Tags=[{"Key": TAG_APPSETS, "Value": ",".join(wanted)}],
            )
        else:
            ec2.delete_tags(Resources=[instance["InstanceId"]], Tags=[{"Key": TAG_APPSETS}])
        self.print(
            f"{TAG_APPSETS}: {','.join(current) or '(none)'} -> {','.join(wanted) or '(none)'}; "
            "Patchgate will reconcile the instance shortly"
        )
        return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="patchgate-cli", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)  # fmt: skip
    p.add_argument("--table", default="patchgate-config")
    p.add_argument("--history-table", default="patchgate-config-history")
    p.add_argument("--profile", help="AWS CLI profile (instances; also config unless overridden)")
    p.add_argument("--config-profile", help="AWS CLI profile for the hub configuration table")
    p.add_argument("--region")
    p.add_argument("--force", action="store_true", help="override built-in/reference protection")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("list", help="list documents of a kind")
    s.add_argument("kind")
    s = sub.add_parser("describe", help="show a document")
    s.add_argument("kind")
    s.add_argument("name")
    s = sub.add_parser("apply", help="create or update documents from a YAML file")
    s.add_argument("-f", "--file", required=True)
    s.add_argument("--dry-run", action="store_true")
    s = sub.add_parser("copy", help="copy a document under a new name (to customise built-ins)")
    s.add_argument("kind")
    s.add_argument("name")
    s.add_argument("new_name")
    s = sub.add_parser("delete", help="delete a document")
    s.add_argument("kind")
    s.add_argument("name")
    s = sub.add_parser("history", help="show every recorded version of a document")
    s.add_argument("kind")
    s.add_argument("name")
    s = sub.add_parser("rollback", help="restore a previous version of a document")
    s.add_argument("kind")
    s.add_argument("name")
    s.add_argument("--version", type=int, required=True)

    instance = sub.add_parser("instance", help="inspect or assign app sets to an instance")
    isub = instance.add_subparsers(dest="instance_command", required=True)
    s = isub.add_parser("show")
    s.add_argument("instance_id")
    s = isub.add_parser("appsets", help="change the instance's app sets (validated first)")
    s.add_argument("instance_id")
    s.add_argument("--add", action="append")
    s.add_argument("--remove", action="append")
    s.add_argument("--set", help="replace with a comma-separated list ('' clears)")
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    cli = Cli(args)
    command = args.command if args.command != "instance" else f"instance_{args.instance_command}"
    try:
        return int(getattr(cli, f"cmd_{command}")())
    except (CliError, VersionConflictError) as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
