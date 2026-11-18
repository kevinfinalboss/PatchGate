"""Approval requests and the approval page against mocked AWS (moto)."""

from __future__ import annotations

import base64
import importlib.util
import json
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import boto3
import pytest
from moto import mock_aws

HANDLER = Path(__file__).resolve().parents[1] / "functions" / "approvals" / "handler.py"
CATALOG = Path(__file__).resolve().parents[3] / "core" / "catalog"
REGION = "us-east-2"
SENDER = "patchgate@example.com"
EXECUTION = "arn:aws:states:us-east-2:123456789012:execution:patchgate-rollout:x"


@pytest.fixture
def approvals(monkeypatch: pytest.MonkeyPatch) -> Iterator[ModuleType]:
    for key, value in {
        "AWS_REGION": REGION,
        "AWS_DEFAULT_REGION": REGION,
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "PATCHGATE_APPROVAL_SENDER": SENDER,
        "PATCHGATE_APPROVAL_API_URL": "https://approve.example.com",
    }.items():
        monkeypatch.setenv(key, value)
    with mock_aws():
        for name, keys in (
            ("patchgate-config", [("kind", "HASH"), ("name", "RANGE")]),
            ("patchgate-config-history", [("id", "HASH"), ("changeId", "RANGE")]),
            ("patchgate-releases", [("id", "HASH")]),
            ("patchgate-approvals", [("requestId", "HASH")]),
        ):
            boto3.client("dynamodb").create_table(
                TableName=name,
                BillingMode="PAY_PER_REQUEST",
                AttributeDefinitions=[{"AttributeName": k, "AttributeType": "S"} for k, _ in keys],
                KeySchema=[{"AttributeName": k, "KeyType": t} for k, t in keys],
            )
        boto3.client("sesv2").create_email_identity(EmailIdentity=SENDER)
        from patchgate.catalog import Catalog
        from patchgate_aws.store import BUILT_IN, ConfigStore

        store = ConfigStore("patchgate-config")
        for doc in Catalog.load(CATALOG).documents():
            store.put(doc, "seed", managed_by=BUILT_IN)
        store.put(
            {
                "apiVersion": "patchgate.io/v1alpha1",
                "kind": "ApproverGroup",
                "metadata": {"name": "platform", "description": "test"},
                "spec": {"members": ["ana@example.com", "joao@example.com"], "minApprovals": 2},
            },
            "test",
        )
        spec = importlib.util.spec_from_file_location("approvals_handler", HANDLER)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules["approvals_handler"] = module
        spec.loader.exec_module(module)
        yield module


@pytest.fixture
def resumed(approvals: ModuleType, monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    calls: list[tuple[str, str]] = []

    def fake_resume(item: dict[str, Any], state: str, member: str) -> bool:
        calls.append((state, member))
        return True

    monkeypatch.setattr(approvals, "resume", fake_resume)
    return calls


def request(approvals: ModuleType, timeout: int = 3600) -> tuple[str, dict[str, str]]:
    """Creates a request; returns its id and each member's link token (captured from SES)."""
    from patchgate_aws.store import ConfigStore

    revision = ConfigStore("patchgate-config").get("AppSet", "devtools")
    assert revision is not None
    sent: list[dict[str, Any]] = []
    original = getattr(approvals, "send_email")  # noqa: B009

    def capture(
        member: str, event: dict[str, Any], unit: str, count: int, link: str, exp: int
    ) -> None:
        sent.append({"member": member, "link": link})
        original(member, event, unit, count, link, exp)

    setattr(approvals, "send_email", capture)  # noqa: B010
    try:
        result = approvals.handler(
            {
                "action": "request",
                "taskToken": "token-from-step-functions",
                "executionArn": EXECUTION,
                "kind": "AppSet",
                "name": "devtools",
                "version": 2,
                "revision": revision.revision,
                "timeoutSeconds": timeout,
                "unit": {
                    "unit": "1:platform",
                    "group": "platform",
                    "instances": [{"instanceId": "i-0abc", "accountId": "123456789012"}],
                },
            }
        )
    finally:
        setattr(approvals, "send_email", original)  # noqa: B010
    tokens = {s["member"]: parse_qs(urlparse(s["link"]).query)["t"][0] for s in sent}
    return result["requestId"], tokens


def get(approvals: ModuleType, token: str) -> dict[str, Any]:
    return dict(
        approvals.handler(
            {"requestContext": {"http": {"method": "GET"}}, "queryStringParameters": {"t": token}}
        )
    )


def post(approvals: ModuleType, token: str, decision: str, encoded: bool = False) -> dict[str, Any]:
    body = urlencode({"t": token, "decision": decision})
    return dict(
        approvals.handler(
            {
                "requestContext": {"http": {"method": "POST"}},
                "body": base64.b64encode(body.encode()).decode() if encoded else body,
                "isBase64Encoded": encoded,
            }
        )
    )


def item(request_id: str) -> dict[str, Any]:
    return dict(
        boto3.resource("dynamodb")
        .Table("patchgate-approvals")
        .get_item(Key={"requestId": request_id})["Item"]
    )


def test_request_emails_each_member_a_link_of_their_own(approvals: ModuleType) -> None:
    request_id, tokens = request(approvals)
    assert set(tokens) == {"ana@example.com", "joao@example.com"}
    assert tokens["ana@example.com"] != tokens["joao@example.com"]
    stored = item(request_id)
    assert stored["status"] == "pending" and stored["taskToken"] == "token-from-step-functions"
    assert "taskToken" not in json.dumps(tokens)  # the task token never leaves the hub
    key = boto3.client("ssm").get_parameter(Name=approvals.KEY_PARAMETER, WithDecryption=True)
    assert key["Parameter"]["Type"] == "SecureString"


def test_opening_the_link_decides_nothing(approvals: ModuleType, resumed: list[Any]) -> None:
    request_id, tokens = request(approvals)
    page = get(approvals, tokens["ana@example.com"])
    assert page["statusCode"] == 200
    assert "Approve and update these instances" in page["body"] and "i-0abc" in page["body"]
    assert item(request_id)["decisions"] == [] and resumed == []
    headers = page["headers"]
    assert headers["Referrer-Policy"] == "no-referrer" and headers["Cache-Control"] == "no-store"
    assert "default-src 'none'" in headers["Content-Security-Policy"]


def test_quorum_then_the_workflow_resumes_once(approvals: ModuleType, resumed: list[Any]) -> None:
    request_id, tokens = request(approvals)
    first = post(approvals, tokens["ana@example.com"], "approve", encoded=True)
    assert "Approval recorded" in first["body"] and resumed == []
    again = post(approvals, tokens["ana@example.com"], "approve")
    assert "already decided" in again["body"]
    done = post(approvals, tokens["joao@example.com"], "approve")
    assert "Approved" in done["body"]
    assert resumed == [("approved", "joao@example.com")]
    assert item(request_id)["status"] == "approved"
    late = post(approvals, tokens["joao@example.com"], "reject")
    assert resumed == [("approved", "joao@example.com")] and "Approved" in late["body"]


def test_any_rejection_rejects(approvals: ModuleType, resumed: list[Any]) -> None:
    request_id, tokens = request(approvals)
    post(approvals, tokens["joao@example.com"], "reject")
    assert resumed == [("rejected", "joao@example.com")]
    assert item(request_id)["status"] == "rejected"


@pytest.mark.parametrize("mutate", [lambda t: t[:-3] + "abc", lambda t: "x" + t, lambda t: ""])
def test_tampered_links_are_refused(approvals: ModuleType, mutate: Any) -> None:
    _, tokens = request(approvals)
    page = get(approvals, mutate(tokens["ana@example.com"]))
    assert page["statusCode"] in (403, 410) and "can't be used" in page["body"]


def test_expired_links_are_refused(approvals: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    _, tokens = request(approvals, timeout=60)
    monkeypatch.setattr(approvals.time, "time", lambda: 10_000_000_000)
    assert get(approvals, tokens["ana@example.com"])["statusCode"] == 410


def test_links_from_another_key_are_refused(approvals: ModuleType) -> None:
    from patchgate import approval

    forged = approval.sign(
        b"f" * 32, approval.Claims("0" * 32, "ana@example.com", int(time.time()) + 600)
    )
    assert get(approvals, forged)["statusCode"] == 403


def test_a_superseded_rollout_cancels_the_request(
    approvals: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(approvals, "resume", lambda item, state, member: False)
    request_id, tokens = request(approvals)
    page = post(approvals, tokens["joao@example.com"], "reject")
    assert "No longer needed" in page["body"]
    assert item(request_id)["status"] == "cancelled"


def test_page_content_is_escaped(approvals: ModuleType, resumed: list[Any]) -> None:
    request_id, tokens = request(approvals)
    boto3.resource("dynamodb").Table("patchgate-approvals").update_item(
        Key={"requestId": request_id},
        UpdateExpression="SET diff = :d",
        ExpressionAttributeValues={":d": "+<script>alert(1)</script>"},
    )
    body = get(approvals, tokens["ana@example.com"])["body"]
    assert "<script>" not in body and "&lt;script&gt;" in body


def test_unknown_decision_is_refused(approvals: ModuleType, resumed: list[Any]) -> None:
    _, tokens = request(approvals)
    assert post(approvals, tokens["ana@example.com"], "maybe")["statusCode"] == 400
