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
    patchgate-cli instance reconcile i-0123456789abcdef0
    patchgate-cli account set 123456789012 --ring 1 --approvers platform --allow payments
    patchgate-cli rollout list
    patchgate-cli rollout status AppSet-dotnet8-1a2b3c4d5e6f-1763481600
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

import boto3
import yaml
from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError

from patchgate.plan import KINDS, CatalogError, Document, Library
from patchgate.rollout import GATED_KINDS, policy_rings
from patchgate_aws.store import BUILT_IN, ConfigStore, Record, ReleaseStore, VersionConflictError
from patchgate_aws.validation import validate as validate_any

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
    "rolloutpolicy": "RolloutPolicy",
    "rolloutpolicies": "RolloutPolicy",
    "approvergroup": "ApproverGroup",
    "approvergroups": "ApproverGroup",
    "account": "Account",
    "accounts": "Account",
}
TAG_PROFILE = "Patchgate:Profile"
TAG_APPSETS = "Patchgate:AppSets"
TAG_MANAGED = "Patchgate:Managed"
DRIFT_DOCUMENT = "Patchgate-Drift"
# Steps of the drift document; output must be read per step (it is empty otherwise).
DRIFT_STEPS = {"linux": "driftLinux", "windows": "driftWindows"}
COMMAND_DONE = {"Success", "Failed", "Cancelled", "TimedOut", "Undeliverable", "Terminated"}
POLL_SECONDS = 5.0
OUTPUT_TAIL_LINES = 20
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
        self.releases = ReleaseStore(args.releases_table, self.config_session)
        self.store = ConfigStore(
            args.table,
            self.config_session,
            history_table=args.history_table,
            releases=self.releases,
        )
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
        errors = validate_any(doc, f"{doc.get('kind')}/{doc.get('metadata', {}).get('name')}")
        if errors:
            raise CliError("invalid document:\n  " + "\n  ".join(errors))
        if doc["kind"] == "Account":
            self.validate_account(doc)
            return
        library = self.full_library()
        library.add(doc)
        problems = library.reference_errors()
        if problems:
            raise CliError("inconsistent with existing configuration:\n  " + "\n  ".join(problems))

    def rollout_policy(self) -> Document | None:
        record = self.store.get("RolloutPolicy", self.args.rollout_policy)
        return record.document if record else None

    def validate_account(self, doc: Document) -> None:
        spec = doc["spec"]
        problems = [
            f"unknown approver group '{group}'"
            for group in dict.fromkeys([spec["approvers"], *spec.get("allowedApprovers", [])])
            if self.store.get("ApproverGroup", group) is None
        ]
        policy = self.rollout_policy()
        if policy is not None and spec["ring"] not in policy_rings(policy):
            problems.append(
                f"ring {spec['ring']} is not in rollout policy '{self.args.rollout_policy}' "
                f"(rings {policy_rings(policy)})"
            )
        if problems:
            raise CliError("inconsistent with existing configuration:\n  " + "\n  ".join(problems))

    def gated(self, kind: str) -> bool:
        policy = self.rollout_policy()
        return policy is not None and kind in policy["spec"].get("kinds", GATED_KINDS)

    def pin_before_change(self, current: Record | None) -> bool:
        """Gated documents: instances keep the current revision until a rollout releases the
        new one (ADR 0010). Returns True when the change is gated."""
        if current is None or not self.gated(current.document["kind"]):
            return False
        kind, name = current.document["kind"], current.document["metadata"]["name"]
        self.releases.pin(kind, name, current.revision, self.caller())
        return True

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
            gated = self.pin_before_change(current)
            version = self.store.put(doc, self.caller(), managed_by=managed_by)
            self.print(f"{kind}/{name}: {'updated' if current else 'created'} (version {version})")
            if gated and current is not None:
                self.print(
                    f"  pending: instances keep version {current.version} until the rollout "
                    "releases it ring by ring (patchgate-cli rollout list)"
                )
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
        gated = self.pin_before_change(current)
        version = self.store.put(doc, f"{self.caller()} (rollback to v{self.args.version})")
        self.print(f"{kind}/{self.args.name}: restored version {self.args.version} as {version}")
        if gated:
            self.print("  pending: instances keep their version until a rollout releases it")
        return 0

    # ------------------------------------------------------------ rollouts

    def rollout_machine(self) -> str:
        account = self.config_session.client("sts").get_caller_identity()["Account"]
        region = self.config_session.region_name
        return f"arn:aws:states:{region}:{account}:stateMachine:{self.args.rollout_state_machine}"

    def rollout_execution(self) -> str:
        machine = self.rollout_machine()
        return machine.replace(":stateMachine:", ":execution:") + f":{self.args.execution}"

    def cmd_rollout_list(self) -> int:
        sfn = self.config_session.client("stepfunctions")
        executions = sfn.list_executions(stateMachineArn=self.rollout_machine(), maxResults=20)
        self.print(f"{'ROLLOUT':64} {'STATUS':10} STARTED")
        for e in executions["executions"]:
            self.print(f"{e['name']:64} {e['status']:10} {e['startDate']:%Y-%m-%d %H:%M}")
        return 0

    def cmd_rollout_status(self) -> int:
        sfn = self.config_session.client("stepfunctions")
        arn = self.rollout_execution()
        try:
            execution = sfn.describe_execution(executionArn=arn)
        except sfn.exceptions.ExecutionDoesNotExist as exc:
            raise CliError(f"rollout {self.args.execution} not found") from exc
        detail = json.loads(execution["input"])
        self.print(
            f"{detail['kind']}/{detail['name']}  {detail['revision'][:19]}  {execution['status']}"
        )
        table = self.config_session.resource("dynamodb").Table(self.args.approvals_table)
        requests: list[dict[str, Any]] = []
        kwargs: dict[str, Any] = {"FilterExpression": Attr("executionArn").eq(arn)}
        while True:
            page = table.scan(**kwargs)
            requests += page.get("Items", [])
            if "LastEvaluatedKey" not in page:
                break
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
        for item in sorted(requests, key=lambda i: str(i["unit"])):
            decided = ", ".join(
                f"{d['member']} {'approved' if d['approve'] else 'rejected'}"
                for d in item.get("decisions", [])
            )
            self.print(
                f"  approval {item['unit']:24} {item['status']:9} "
                f"{len(item.get('instances', []))} instance(s)"
                + (f"  ({decided})" if decided else "")
            )
        if not requests:
            self.print("  no approval requests (yet)")
        return 0

    def cmd_rollout_start(self) -> int:
        """Roll out the current revision again (after a failure, a cancel or new instances)."""
        kind = kind_of(self.args.kind)
        if not self.gated(kind):
            raise CliError(f"{kind} changes are not rolled out (rollout policy kinds)")
        record = self.store.get(kind, self.args.name)
        if record is None:
            raise CliError(f"{kind}/{self.args.name} not found")
        name = f"{kind}-{self.args.name}"[:48] + f"-{record.revision[7:19]}-{int(time.time())}"
        self.config_session.client("stepfunctions").start_execution(
            stateMachineArn=self.rollout_machine(),
            name=name,
            input=json.dumps({"kind": kind, "name": self.args.name, "revision": record.revision}),
        )
        self.print(f"rollout {name} started for {kind}/{self.args.name} version {record.version}")
        return 0

    def cmd_rollout_cancel(self) -> int:
        sfn = self.config_session.client("stepfunctions")
        sfn.stop_execution(
            executionArn=self.rollout_execution(),
            error="Cancelled",
            cause=f"cancelled with patchgate-cli by {self.caller()}",
        )
        self.print(
            f"rollout {self.args.execution} cancelled; groups already released keep the new "
            "revision, the others keep the previous one"
        )
        return 0

    # ------------------------------------------------------------ accounts

    def cmd_account_set(self) -> int:
        current = self.store.get("Account", self.args.account_id)
        spec: dict[str, Any] = {"ring": self.args.ring, "approvers": self.args.approvers}
        if self.args.allow:
            spec["allowedApprovers"] = [g.strip() for g in self.args.allow.split(",") if g.strip()]
        if self.args.allow_ring_tag:
            spec["allowRingTag"] = True
        doc: Document = {
            "apiVersion": "patchgate.io/v1alpha1",
            "kind": "Account",
            "metadata": {"name": self.args.account_id},
            "spec": spec,
        }
        if self.args.description:
            doc["metadata"]["description"] = self.args.description
        self.validate(doc)
        if current and current.document == doc:
            self.print(f"Account/{self.args.account_id}: unchanged")
            return 0
        version = self.store.put(doc, self.caller())
        self.print(
            f"Account/{self.args.account_id}: ring {spec['ring']}, approvers {spec['approvers']}"
            + (f" (also allowed: {', '.join(spec['allowedApprovers'])})" if self.args.allow else "")
            + f" (version {version})"
        )
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

    def cmd_instance_reconcile(self) -> int:
        _, instance, tags = self.instance()
        instance_id = instance["InstanceId"]
        if tags.get(TAG_MANAGED) != "true":
            raise CliError(f"instance {instance_id} is not managed by Patchgate ({TAG_MANAGED})")
        if instance["State"]["Name"] != "running":
            raise CliError(f"instance {instance_id} is {instance['State']['Name']}, not running")
        ssm = self.session.client("ssm")
        command = ssm.send_command(
            InstanceIds=[instance_id],
            DocumentName=DRIFT_DOCUMENT,
            Parameters={"Force": ["true"]},
            Comment="patchgate-cli instance reconcile",
        )["Command"]
        self.print(f"reconcile started on {instance_id} (command {command['CommandId']})")
        if self.args.no_wait:
            return 0
        invocation = self.wait_for_command(ssm, command["CommandId"], instance_id)
        platform = "windows" if instance.get("Platform", "").lower() == "windows" else "linux"
        output = step_output(ssm, command["CommandId"], instance_id, DRIFT_STEPS[platform])
        for line in output.rstrip().splitlines()[-OUTPUT_TAIL_LINES:]:
            self.print(f"  {line}")
        status = invocation["Status"]
        self.print(f"reconcile {status.lower()} on {instance_id}")
        return 0 if status == "Success" else 1

    def wait_for_command(self, ssm: Any, command_id: str, instance_id: str) -> dict[str, Any]:
        deadline = time.monotonic() + self.args.timeout
        while True:
            try:
                invocation: dict[str, Any] = ssm.get_command_invocation(
                    CommandId=command_id, InstanceId=instance_id
                )
                if invocation["Status"] in COMMAND_DONE:
                    return invocation
            except ClientError as exc:  # not registered yet right after send_command
                if exc.response["Error"]["Code"] != "InvocationDoesNotExist":
                    raise
            if time.monotonic() >= deadline:
                raise CliError(
                    f"command {command_id} still running after {self.args.timeout}s; "
                    "check it in the SSM console"
                )
            time.sleep(POLL_SECONDS)


def step_output(ssm: Any, command_id: str, instance_id: str, step: str) -> str:
    """Output of one document step (the invocation-level output is empty with several steps)."""
    try:
        invocation = ssm.get_command_invocation(
            CommandId=command_id, InstanceId=instance_id, PluginName=step
        )
    except ClientError:
        return ""
    return str(invocation.get("StandardOutputContent", ""))


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="patchgate-cli", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)  # fmt: skip
    p.add_argument("--table", default="patchgate-config")
    p.add_argument("--history-table", default="patchgate-config-history")
    p.add_argument("--releases-table", default="patchgate-releases")
    p.add_argument("--rollout-policy", default="default")
    p.add_argument("--rollout-state-machine", default="patchgate-rollout")
    p.add_argument("--approvals-table", default="patchgate-approvals")
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

    account = sub.add_parser("account", help="register an account's ring and approver groups")
    asub = account.add_subparsers(dest="account_command", required=True)
    s = asub.add_parser("set", help="create or replace the account's rollout settings")
    s.add_argument("account_id")
    s.add_argument("--ring", type=int, required=True)
    s.add_argument("--approvers", required=True, help="approver group of the account")
    s.add_argument("--allow", help="comma-separated groups instances may pick by tag")
    s.add_argument("--allow-ring-tag", action="store_true", help="honour Patchgate:Ring")
    s.add_argument("--description")

    rollout = sub.add_parser(
        "rollout", help="follow or cancel rollouts of profile and app set changes"
    )
    rsub = rollout.add_subparsers(dest="rollout_command", required=True)
    rsub.add_parser("list", help="recent rollouts")
    s = rsub.add_parser("status", help="a rollout and its approval requests")
    s.add_argument("execution")
    s = rsub.add_parser("start", help="roll out the current revision again (e.g. after a failure)")
    s.add_argument("kind")
    s.add_argument("name")
    s = rsub.add_parser("cancel", help="stop a rollout (pending approvals are discarded)")
    s.add_argument("execution")

    instance = sub.add_parser("instance", help="inspect, assign app sets or reconcile instances")
    isub = instance.add_subparsers(dest="instance_command", required=True)
    s = isub.add_parser("show")
    s.add_argument("instance_id")
    s = isub.add_parser("appsets", help="change the instance's app sets (validated first)")
    s.add_argument("instance_id")
    s.add_argument("--add", action="append")
    s.add_argument("--remove", action="append")
    s.add_argument("--set", help="replace with a comma-separated list ('' clears)")
    s = isub.add_parser(
        "reconcile",
        help="run the drift check now (ignores the profile's interval and maintenance window)",
    )
    s.add_argument("instance_id")
    s.add_argument("--no-wait", action="store_true", help="return once the command is sent")
    s.add_argument("--timeout", type=int, default=1800, help="seconds to wait (default 1800)")
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    cli = Cli(args)
    command = args.command
    if command in ("instance", "account", "rollout"):
        command = f"{command}_{getattr(args, command + '_command')}"
    try:
        return int(getattr(cli, f"cmd_{command}")())
    except (CliError, VersionConflictError) as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
