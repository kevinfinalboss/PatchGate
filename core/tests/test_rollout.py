"""Rollout rules and approval tokens (pure logic, no cloud)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from patchgate import approval, rollout
from patchgate.catalog import Catalog, CatalogError
from patchgate.rollout import AccountSettings, Decision, Placement, Target
from patchgate.schema import validate_document

REAL_CATALOG = Path(__file__).resolve().parents[1] / "catalog"
DEV, PROD = "111111111111", "222222222222"


def policy(**spec: Any) -> dict[str, Any]:
    return {
        "apiVersion": "patchgate.io/v1alpha1",
        "kind": "RolloutPolicy",
        "metadata": {"name": "p", "description": "test"},
        "spec": {
            "rings": [
                {"ring": 0, "approval": "none", "soak": "10m"},
                {"ring": 1, "approval": "required"},
            ],
            **spec,
        },
    }


def group(name: str = "platform", *members: str, min_approvals: int = 1) -> dict[str, Any]:
    return {
        "apiVersion": "patchgate.io/v1alpha1",
        "kind": "ApproverGroup",
        "metadata": {"name": name, "description": "test"},
        "spec": {"members": list(members or ["ana@example.com"]), "minApprovals": min_approvals},
    }


ACCOUNTS = {
    DEV: AccountSettings(ring=0, approvers="platform"),
    PROD: AccountSettings(ring=1, approvers="platform", allowed_approvers=frozenset({"payments"})),
}


def target(instance: str, account: str = PROD, **kwargs: Any) -> Target:
    return Target(instance_id=instance, account=account, profile="web", **kwargs)


# ---------------------------------------------------------------- schemas and catalog


def test_default_policy_is_valid_and_gates_appsets_and_profiles() -> None:
    catalog = Catalog.load(REAL_CATALOG)
    spec = catalog.policies["default"]["spec"]
    assert spec["kinds"] == ["AppSet", "Profile"]
    assert [r["ring"] for r in spec["rings"]] == [0, 1]
    assert spec["onFailure"] == "revert"


@pytest.mark.parametrize(
    "doc",
    [
        policy(rings=[]),
        policy(rings=[{"ring": 10}]),
        policy(rings=[{"ring": 0, "soak": "10s"}]),
        policy(approvalTimeout="0h"),
        policy(approvalTimeout="169h"),
        policy(onFailure="ignore"),
        policy(kinds=["Agent"]),
        group("g", "not-an-email"),
        group("g", "a@example.com", min_approvals=0),
    ],
)
def test_invalid_documents_are_rejected(doc: dict[str, Any]) -> None:
    assert validate_document(doc)


def test_rings_must_ascend_and_quorum_must_be_reachable(tmp_path: Path) -> None:
    import yaml

    (tmp_path / "policies").mkdir()
    bad_policy = policy(rings=[{"ring": 1}, {"ring": 0}])
    bad_policy["metadata"]["name"] = "bad"
    (tmp_path / "policies" / "bad.yaml").write_text(yaml.safe_dump(bad_policy))
    (tmp_path / "groups").mkdir()
    (tmp_path / "groups" / "g.yaml").write_text(
        yaml.safe_dump(group("g", "a@example.com", min_approvals=2))
    )
    with pytest.raises(CatalogError) as exc:
        Catalog.load(tmp_path)
    assert any("ascending" in e for e in exc.value.errors)
    assert any("minApprovals" in e for e in exc.value.errors)


@pytest.mark.parametrize(("text", "seconds"), [("0m", 0), ("10m", 600), ("72h", 259200)])
def test_durations(text: str, seconds: int) -> None:
    assert rollout.duration_seconds(text) == seconds


# ---------------------------------------------------------------- placement


def test_ring_and_group_come_from_the_account() -> None:
    assert rollout.place(target("i-1", DEV), ACCOUNTS, policy(), "hub") == Placement(0, "platform")
    assert rollout.place(target("i-2"), ACCOUNTS, policy(), "hub").unit == "1:platform"


def test_instance_tag_overrides_group_when_the_account_allows_it() -> None:
    placement = rollout.place(target("i-1", approvers_tag="payments"), ACCOUNTS, policy(), "hub")
    assert placement == Placement(1, "payments")


def test_disallowed_group_tag_is_refused_with_a_warning() -> None:
    placement = rollout.place(target("i-1", approvers_tag="myself"), ACCOUNTS, policy(), "hub")
    assert placement.group == "platform"
    assert "not allowed" in placement.warnings[0]


def test_unregistered_account_goes_to_the_last_ring() -> None:
    placement = rollout.place(target("i-1", "333333333333"), ACCOUNTS, policy(), "hub")
    assert (placement.ring, placement.group) == (1, "hub")
    assert "not registered" in placement.warnings[0]


def test_ring_tag_only_where_the_account_allows_it() -> None:
    single = {DEV: AccountSettings(ring=1, approvers="platform", allow_ring_tag=True)}
    assert rollout.place(target("i-1", DEV, ring_tag="0"), single, policy(), "hub").ring == 0
    refused = rollout.place(target("i-1", DEV, ring_tag="0"), ACCOUNTS, policy(), "hub")
    assert refused.ring == 0  # DEV account ring, the tag is ignored
    assert "does not allow" in refused.warnings[0]
    unknown = rollout.place(target("i-1", DEV, ring_tag="7"), single, policy(), "hub")
    assert unknown.ring == 1 and "not a policy ring" in unknown.warnings[0]


# ---------------------------------------------------------------- planning


def test_plan_groups_affected_instances_by_ring_and_group() -> None:
    targets = [
        target("i-dev", DEV, appsets=("dotnet8",)),
        target("i-p1", appsets=("dotnet8",)),
        target("i-p2", appsets=("dotnet8", "nodejs-lts"), approvers_tag="payments"),
        target("i-other", appsets=("nodejs-lts",)),
    ]
    plan = rollout.plan_rollout("AppSet", "dotnet8", "r5", targets, ACCOUNTS, policy(), "hub")
    assert [(s.ring, s.approval, s.soak_seconds) for s in plan.stages] == [
        (0, False, 600),
        (1, True, 0),
    ]
    assert plan.stages[0].units == {"0:platform": ["i-dev"]}
    assert plan.stages[1].units == {"1:platform": ["i-p1"], "1:payments": ["i-p2"]}
    assert plan.summary() == "ring 0: 1 (platform 1) | ring 1: 2 (payments 1, platform 1)"
    assert plan.covers_every_ring


def test_profile_changes_affect_instances_using_the_profile() -> None:
    targets = [target("i-1"), Target("i-2", PROD, "other")]
    plan = rollout.plan_rollout("Profile", "web", "r2", targets, ACCOUNTS, policy(), "hub")
    assert plan.stages[1].units == {"1:platform": ["i-1"]}
    assert not plan.covers_every_ring  # nobody in ring 0


def test_kinds_outside_the_policy_are_not_rolled_out() -> None:
    with pytest.raises(rollout.RolloutError):
        rollout.plan_rollout("Profile", "web", "r2", [], ACCOUNTS, policy(kinds=["AppSet"]), "hub")


# ---------------------------------------------------------------- released versions


def test_revision_is_the_digest_of_the_stored_document() -> None:
    assert rollout.revision_of('{"a": 1}') == rollout.revision_of('{"a": 1}')
    assert rollout.revision_of('{"a": 1}') != rollout.revision_of('{"a": 2}')
    assert rollout.revision_of("x").startswith("sha256:")


def test_instances_get_their_units_release_or_the_revision_released_everywhere() -> None:
    released = {"*": "r4", "0:platform": "r5"}
    assert rollout.revision_for(released, Placement(0, "platform")) == "r5"
    assert rollout.revision_for(released, Placement(1, "platform")) == "r4"


def test_full_rollout_releases_the_version_to_everyone() -> None:
    targets = [target("i-dev", DEV, appsets=("s",)), target("i-p", appsets=("s",))]
    plan = rollout.plan_rollout("AppSet", "s", "r5", targets, ACCOUNTS, policy(), "hub")
    released = {"*": "r4"}
    for unit in ("0:platform", "1:platform"):
        released = rollout.release(released, unit, "r5")
    assert rollout.complete(released, "r5", plan) == {"*": "r5"}


def test_rejection_keeps_the_previous_version_for_that_unit_and_new_instances() -> None:
    targets = [
        target("i-dev", DEV, appsets=("s",)),
        target("i-p1", appsets=("s",)),
        target("i-p2", appsets=("s",), approvers_tag="payments"),
    ]
    plan = rollout.plan_rollout("AppSet", "s", "r5", targets, ACCOUNTS, policy(), "hub")
    released = rollout.release(rollout.release({"*": "r4"}, "0:platform", "r5"), "1:platform", "r5")
    final = rollout.complete(released, "r5", plan)  # payments rejected
    assert final == {"*": "r4", "0:platform": "r5", "1:platform": "r5"}
    assert rollout.revision_for(final, Placement(1, "payments")) == "r4"


def test_revert_restores_what_the_unit_had_before() -> None:
    before = {"*": "r4", "1:payments": "r3"}
    during = rollout.release(rollout.release(before, "0:platform", "r5"), "1:payments", "r5")
    assert rollout.revert(during, "0:platform", before) == {"*": "r4", "1:payments": "r5"}
    assert rollout.revert(during, "1:payments", before)["1:payments"] == "r3"


# ---------------------------------------------------------------- approval rules


def test_quorum_counts_distinct_members_and_any_rejection_rejects() -> None:
    team = group("g", "ana@example.com", "joao@example.com", min_approvals=2)
    ana, joao = Decision("ana@example.com", True), Decision("JOAO@example.com", True)
    assert rollout.approval_state([ana], team) == "pending"
    assert rollout.approval_state([ana, ana], team) == "pending"
    assert rollout.approval_state([ana, joao], team) == "approved"
    assert rollout.approval_state([ana, Decision("joao@example.com", False)], team) == "rejected"
    assert rollout.approval_state([Decision("eve@example.com", True), ana], team) == "pending"


# ---------------------------------------------------------------- approval tokens

SECRET = b"s" * 32
CLAIMS = approval.Claims("req-0123456789", "Ana@Example.com", 2_000)


def test_token_round_trip_normalises_the_member() -> None:
    token = approval.sign(SECRET, CLAIMS)
    assert approval.verify(SECRET, token, now=1_000) == approval.Claims(
        "req-0123456789", "ana@example.com", 2_000
    )


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda t: t[:-2] + ("AA" if not t.endswith("AA") else "BB"), "signature"),
        (lambda t: "x" + t, "signature"),
        (lambda t: t.replace(".", ""), "malformed"),
        (lambda t: t + ".x", "malformed"),
    ],
)
def test_tampered_tokens_are_refused(mutate: Any, message: str) -> None:
    token = approval.sign(SECRET, CLAIMS)
    with pytest.raises(approval.TokenError, match=message):
        approval.verify(SECRET, mutate(token), now=1_000)


def test_expired_and_foreign_tokens_are_refused() -> None:
    token = approval.sign(SECRET, CLAIMS)
    with pytest.raises(approval.TokenError, match="expired"):
        approval.verify(SECRET, token, now=2_000)
    with pytest.raises(approval.TokenError, match="signature"):
        approval.verify(b"o" * 32, token, now=1_000)


def test_short_secrets_and_bad_request_ids_are_refused() -> None:
    with pytest.raises(approval.TokenError):
        approval.sign(b"short", CLAIMS)
    with pytest.raises(approval.TokenError):
        approval.sign(SECRET, approval.Claims("bad id!", "a@example.com", 2_000))
