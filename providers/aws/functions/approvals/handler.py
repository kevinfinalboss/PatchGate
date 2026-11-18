"""Approval requests for the patchgate-rollout workflow (ADR 0010).

Two entry points:

- Step Functions (`action: request`, invoked with a task token): records the request, then emails
  every member of the approver group a signed link of their own (SES).
- HTTP API (`GET /approve?t=...` and `POST /approve`): GET shows what would change and asks for a
  decision; only the POST decides, so mail scanners that open links decide nothing. Decisions are
  recorded once per member; when the group's quorum is reached (or anyone rejects) the workflow
  is resumed with SendTaskSuccess.

The signing key is created by this function on first use and kept as a SecureString parameter, so
it never passes through Terraform state.
"""

from __future__ import annotations

import base64
import difflib
import html
import json
import logging
import os
import secrets
import time
import uuid
from typing import Any
from urllib.parse import parse_qs

import boto3
from botocore.exceptions import ClientError

from patchgate import approval
from patchgate.rollout import Decision, Placement, approval_state, revision_for
from patchgate_aws.store import ConfigStore, ReleaseStore

LOG = logging.getLogger()
LOG.setLevel(logging.INFO)

CONFIG_TABLE = os.environ.get("PATCHGATE_CONFIG_TABLE", "patchgate-config")
HISTORY_TABLE = os.environ.get("PATCHGATE_HISTORY_TABLE", "patchgate-config-history")
RELEASES_TABLE = os.environ.get("PATCHGATE_RELEASES_TABLE", "patchgate-releases")
APPROVALS_TABLE = os.environ.get("PATCHGATE_APPROVALS_TABLE", "patchgate-approvals")
KEY_PARAMETER = os.environ.get(
    "PATCHGATE_SIGNING_KEY_PARAMETER", "/patchgate/approvals/signing-key"
)
SENDER = os.environ.get("PATCHGATE_APPROVAL_SENDER", "")
API_URL = os.environ.get("PATCHGATE_APPROVAL_API_URL", "").rstrip("/")
MAX_DIFF_LINES = 80
MAX_LISTED_INSTANCES = 50


class ApprovalError(Exception):
    pass


# ------------------------------------------------------------------ signing key


def signing_key() -> bytes:
    ssm = boto3.client("ssm")
    try:
        value = ssm.get_parameter(Name=KEY_PARAMETER, WithDecryption=True)["Parameter"]["Value"]
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ParameterNotFound":
            raise
        value = base64.b64encode(secrets.token_bytes(32)).decode()
        try:
            ssm.put_parameter(
                Name=KEY_PARAMETER,
                Value=value,
                Type="SecureString",
                Overwrite=False,
                Description="Patchgate approval link signing key (created by the approvals function).",
            )
        except ClientError as put_exc:  # another invocation created it first
            if put_exc.response["Error"]["Code"] != "ParameterAlreadyExists":
                raise
            value = ssm.get_parameter(Name=KEY_PARAMETER, WithDecryption=True)["Parameter"]["Value"]
    return base64.b64decode(value)


def table() -> Any:
    return boto3.resource("dynamodb").Table(APPROVALS_TABLE)


# ------------------------------------------------------------------ request (Step Functions)


def change_diff(kind: str, name: str, revision: str, unit: str) -> str:
    """What the unit's instances would get, as a YAML diff against what they have now."""
    releases = ReleaseStore(RELEASES_TABLE)
    store = ConfigStore(CONFIG_TABLE, history_table=HISTORY_TABLE, releases=releases)
    ring, group = unit.split(":", 1)
    current_revision = revision_for(releases.get(kind, name), Placement(int(ring), group))
    new = store.document_at(kind, name, revision)
    old = store.document_at(kind, name, current_revision) if current_revision else None
    if new is None:
        return "(the proposed revision is no longer available)"

    def lines(record: Any) -> list[str]:
        if record is None:
            return []
        return json.dumps(record.document["spec"], indent=2, sort_keys=True).splitlines()

    diff = list(
        difflib.unified_diff(lines(old), lines(new), "approved", "proposed", lineterm="", n=2)
    )
    if len(diff) > MAX_DIFF_LINES:
        diff = [*diff[:MAX_DIFF_LINES], f"... {len(diff) - MAX_DIFF_LINES} more lines"]
    return "\n".join(diff) or "(no change in spec)"


def request(event: dict[str, Any]) -> dict[str, Any]:
    unit = event["unit"]
    group_name = unit["group"]
    group = ConfigStore(CONFIG_TABLE).get("ApproverGroup", group_name)
    if group is None:
        raise ApprovalError(f"approver group {group_name!r} does not exist")
    spec = group.document["spec"]
    now = int(time.time())
    expires_at = now + int(event["timeoutSeconds"])
    request_id = uuid.uuid4().hex
    instances = [i["instanceId"] for i in unit["instances"]]
    diff = change_diff(event["kind"], event["name"], event["revision"], unit["unit"])
    table().put_item(
        Item={
            "requestId": request_id,
            "status": "pending",
            "taskToken": event["taskToken"],
            "executionArn": event["executionArn"],
            "kind": event["kind"],
            "name": event["name"],
            "version": int(event.get("version") or 0),
            "revision": event["revision"],
            "unit": unit["unit"],
            "group": group_name,
            "members": [m.lower() for m in spec["members"]],
            "minApprovals": spec.get("minApprovals", 1),
            "instances": instances,
            "diff": diff,
            "decisions": [],
            "createdAt": now,
            "expiresAt": expires_at,
        }
    )
    key = signing_key()
    for member in spec["members"]:
        token = approval.sign(key, approval.Claims(request_id, member, expires_at))
        send_email(
            member, event, unit["unit"], len(instances), f"{API_URL}/approve?t={token}", expires_at
        )
    LOG.info("approval %s requested from %s for %s", request_id, group_name, unit["unit"])
    return {"requestId": request_id}


def send_email(
    member: str, event: dict[str, Any], unit: str, count: int, link: str, expires_at: int
) -> None:
    subject = (
        f"[Patchgate] Approve {event['kind']}/{event['name']} for {count} instance(s) ({unit})"
    )
    deadline = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(expires_at))
    text = (
        f"{event['kind']}/{event['name']} has a new version waiting for your group ({unit}).\n"
        f"It would be applied to {count} instance(s).\n\n"
        f"Review the change and decide here (the page asks before recording anything):\n{link}\n\n"
        f"The link is yours only, works once and expires {deadline}. "
        "If nobody decides by then, the change is not applied to these instances."
    )
    boto3.client("sesv2").send_email(
        FromEmailAddress=SENDER,
        Destination={"ToAddresses": [member]},
        Content={"Simple": {"Subject": {"Data": subject}, "Body": {"Text": {"Data": text}}}},
    )


# ------------------------------------------------------------------ decide (HTTP)


def resume(item: dict[str, Any], state: str, member: str) -> bool:
    """Resume the workflow once. False when the rollout no longer waits (superseded, stopped)."""
    try:
        boto3.client("stepfunctions").send_task_success(
            taskToken=item["taskToken"],
            output=json.dumps(
                {"approved": state == "approved", "outcome": state, "decidedBy": member}
            ),
        )
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] in ("TaskTimedOut", "InvalidToken", "TaskDoesNotExist"):
            return False
        raise


def decide(claims: approval.Claims, approve: bool) -> tuple[str, dict[str, Any]]:
    """Record one member's decision. Returns (outcome for the page, request item)."""
    item = table().get_item(Key={"requestId": claims.request_id}, ConsistentRead=True).get("Item")
    if item is None:
        raise ApprovalError("This approval request does not exist.")
    if claims.member not in item["members"]:
        raise ApprovalError("This link is not for a member of the approver group.")
    if item["status"] != "pending":
        return "closed", item
    try:
        updated = table().update_item(
            Key={"requestId": claims.request_id},
            # one write: the decision and the member's "already decided" mark, so two clicks
            # racing each other cannot both pass the condition
            UpdateExpression="SET decisions = list_append(decisions, :d) ADD decidedBy :ms",
            ConditionExpression="#s = :pending AND NOT contains(decidedBy, :m)",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":d": [{"member": claims.member, "approve": approve, "at": int(time.time())}],
                ":ms": {claims.member},
                ":pending": "pending",
                ":m": claims.member,
            },
            ReturnValues="ALL_NEW",
        )["Attributes"]
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return "already", item
        raise
    decisions = [Decision(d["member"], bool(d["approve"])) for d in updated["decisions"]]
    group = {"spec": {"members": updated["members"], "minApprovals": int(updated["minApprovals"])}}
    state = approval_state(decisions, group)
    if state == "pending":
        return "recorded", updated
    try:
        table().update_item(
            Key={"requestId": claims.request_id},
            UpdateExpression="SET #s = :state, decidedAt = :now",
            ConditionExpression="#s = :pending",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":state": state,
                ":now": int(time.time()),
                ":pending": "pending",
            },
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return "closed", updated
        raise
    if not resume(updated, state, claims.member):
        table().update_item(
            Key={"requestId": claims.request_id},
            UpdateExpression="SET #s = :c",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":c": "cancelled"},
        )
        return "cancelled", updated
    return state, updated


# ------------------------------------------------------------------ pages

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>{title}</title>
<style>
:root {{ --bg:#f3f5f4; --paper:#fff; --ink:#14201d; --muted:#56635f; --line:#d7dedb; --teal:#0f766e;
  --teal-ink:#fff; --stop:#9f1239; --add:#e7f6ef; --del:#fbecef; color-scheme: light dark; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#0e1513; --paper:#151e1c; --ink:#e5ecea; --muted:#9aa8a4;
  --line:#2a3633; --teal:#2dd4bf; --teal-ink:#04201d; --stop:#fb7185; --add:#10302a; --del:#3a1520; }} }}
* {{ box-sizing:border-box }}
body {{ margin:0; background:var(--bg); color:var(--ink); font:16px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif; }}
main {{ max-width:680px; margin:0 auto; padding:40px 16px 64px; }}
.mark {{ display:flex; align-items:center; gap:8px; color:var(--muted); font-size:14px; margin-bottom:28px; }}
.mark svg {{ width:20px; height:20px }}
h1 {{ font-size:28px; line-height:1.2; letter-spacing:-.01em; margin:0 0 10px; font-weight:650; }}
p {{ margin:0 0 14px; color:var(--muted); }}
.sheet {{ background:var(--paper); border:1px solid var(--line); border-radius:14px; padding:20px; margin:22px 0; }}
.sheet h2 {{ font-size:15px; margin:0 0 10px; font-weight:600; }}
dl {{ display:grid; grid-template-columns:max-content 1fr; gap:6px 16px; margin:0; font-size:15px; }}
dt {{ color:var(--muted) }} dd {{ margin:0; overflow-wrap:anywhere }}
pre {{ margin:0; overflow-x:auto; font:13px/1.5 ui-monospace,Menlo,Consolas,monospace; }}
pre span {{ display:block; padding:0 8px; border-radius:4px; }}
.add {{ background:var(--add) }} .del {{ background:var(--del) }}
.actions {{ display:flex; flex-wrap:wrap; gap:10px; margin-top:24px; }}
button {{ font:inherit; font-weight:600; padding:12px 20px; border-radius:10px; cursor:pointer; border:1px solid var(--line);
  background:var(--paper); color:var(--ink); }}
button.approve {{ background:var(--teal); border-color:var(--teal); color:var(--teal-ink); }}
button.reject {{ color:var(--stop); }}
button:focus-visible {{ outline:3px solid var(--teal); outline-offset:2px; }}
.note {{ font-size:14px }}
</style></head>
<body><main>
<div class="mark"><svg viewBox="0 0 32 32" aria-hidden="true"><rect width="32" height="32" rx="7" fill="#0f766e"/><path d="M9 16.5l4.5 4.5L23 11.5" stroke="#fff" stroke-width="3.2" fill="none" stroke-linecap="round" stroke-linejoin="round"/></svg>Patchgate change approval</div>
{body}
</main></body></html>"""


def diff_html(diff: str) -> str:
    out = []
    for line in diff.splitlines():
        css = (
            "add"
            if line.startswith("+") and not line.startswith("+++")
            else ("del" if line.startswith("-") and not line.startswith("---") else "")
        )
        out.append(f'<span class="{css}">{html.escape(line) or "&nbsp;"}</span>')
    return "\n".join(out)


def details(item: dict[str, Any]) -> str:
    instances = list(item.get("instances", []))
    shown = ", ".join(html.escape(i) for i in instances[:MAX_LISTED_INSTANCES])
    more = (
        f" and {len(instances) - MAX_LISTED_INSTANCES} more"
        if len(instances) > MAX_LISTED_INSTANCES
        else ""
    )
    approvals = sum(1 for d in item.get("decisions", []) if d.get("approve"))
    return f"""
<div class="sheet"><h2>What changes</h2><pre>{diff_html(str(item.get("diff", "")))}</pre></div>
<div class="sheet"><dl>
<dt>Document</dt><dd>{html.escape(item["kind"])}/{html.escape(item["name"])}, version {int(item.get("version", 0))}</dd>
<dt>Your group</dt><dd>{html.escape(item["group"])} (ring {html.escape(item["unit"].split(":", 1)[0])})</dd>
<dt>Instances</dt><dd>{len(instances)}: {shown}{more}</dd>
<dt>Approvals</dt><dd>{approvals} of {int(item.get("minApprovals", 1))} needed; any rejection stops it</dd>
<dt>Decide by</dt><dd>{time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(int(item["expiresAt"])))}</dd>
</dl></div>"""


def page(title: str, body: str, status: int = 200) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "text/html; charset=utf-8",
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; "
            "form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
        },
        "body": PAGE.format(title=html.escape(title), body=body),
    }


def problem(message: str, status: int = 400) -> dict[str, Any]:
    return page(
        "Link not valid",
        f"<h1>This link can't be used</h1><p>{html.escape(message)}</p>"
        "<p>Ask for a new approval request if the change still needs a decision.</p>",
        status,
    )


CLOSED = {
    "approved": "It was approved, and the instances of this group are being updated.",
    "rejected": "It was rejected, so these instances keep the version they have.",
    "cancelled": "The rollout no longer waits for it (a newer change replaced it, or it was stopped).",
}


def http(event: dict[str, Any]) -> dict[str, Any]:
    method = event.get("requestContext", {}).get("http", {}).get("method", "GET")
    if method == "POST":
        raw = event.get("body") or ""
        if event.get("isBase64Encoded"):
            raw = base64.b64decode(raw).decode()
        form = {k: v[0] for k, v in parse_qs(raw, max_num_fields=4).items()}
    else:
        form = {k: v for k, v in (event.get("queryStringParameters") or {}).items()}
    try:
        claims = approval.verify(signing_key(), form.get("t", ""), time.time())
    except approval.TokenError as exc:
        return problem(f"The link is not valid ({exc}).", 403 if "signature" in str(exc) else 410)
    try:
        if method == "POST":
            if form.get("decision") not in ("approve", "reject"):
                return problem("Choose approve or reject.")
            outcome, item = decide(claims, form["decision"] == "approve")
        else:
            item = (
                table()
                .get_item(Key={"requestId": claims.request_id}, ConsistentRead=True)
                .get("Item")
            )
            if item is None or claims.member not in item["members"]:
                return problem("This approval request does not exist for you.", 404)
            outcome = "ask" if item["status"] == "pending" else "closed"
    except ApprovalError as exc:
        return problem(str(exc), 404)

    name = f"{item['kind']}/{item['name']}"
    if outcome == "ask":
        token = html.escape(form["t"], quote=True)
        body = (
            f"<h1>Approve the new version of {html.escape(name)}?</h1>"
            f"<p>You are deciding for your group's instances only. Nothing is recorded until you choose.</p>"
            f"{details(item)}"
            f'<form method="post" action=""><input type="hidden" name="t" value="{token}">'
            '<div class="actions"><button class="approve" name="decision" value="approve">Approve and update these instances</button>'
            '<button class="reject" name="decision" value="reject">Reject</button></div></form>'
        )
        return page(f"Approve {name}", body)
    if outcome == "recorded":
        body = (
            f"<h1>Approval recorded</h1><p>Thanks, {html.escape(claims.member)}. The change still needs "
            f"approval from more members of {html.escape(item['group'])}.</p>{details(item)}"
        )
        return page("Approval recorded", body)
    if outcome == "already":
        return page(
            "Already decided",
            "<h1>You already decided on this change</h1><p>Each member decides once.</p>",
        )
    state = outcome if outcome in CLOSED else str(item.get("status"))
    verb = {"approved": "Approved", "rejected": "Rejected", "cancelled": "No longer needed"}.get(
        state, "Closed"
    )
    return page(
        verb,
        f"<h1>{verb}: {html.escape(name)}</h1><p>{CLOSED.get(state, 'This request is closed.')}</p>",
    )


def handler(event: dict[str, Any], context: Any = None) -> Any:
    if event.get("action") == "request":
        return request(event)
    if "requestContext" in event:
        return http(event)
    raise ApprovalError("unsupported event")
