"""Static checks for Step Functions definitions (the AWS API validation runs at deploy time)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

WORKFLOWS = Path(__file__).resolve().parents[1] / "workflows"
SAMPLE_VARS = {
    "resolver_arn": "arn:aws:lambda:us-east-2:111111111111:function:patchgate-resolver",
    "topic_arn": "arn:aws:sns:us-east-2:111111111111:patchgate-notifications",
    "ssm_wait_attempts": "20",
    "locks_table": "patchgate-locks",
    "rollout_arn": "arn:aws:lambda:us-east-2:111111111111:function:patchgate-rollout",
    "approvals_arn": "arn:aws:lambda:us-east-2:111111111111:function:patchgate-approvals",
    "bootstrap_arn": "arn:aws:states:us-east-2:111111111111:stateMachine:patchgate-bootstrap",
    "region": "us-east-2",
}


def render(path: Path) -> dict[str, Any]:
    text = path.read_text()
    for key, value in SAMPLE_VARS.items():
        text = text.replace("${" + key + "}", value)
    assert "${" not in text, "unrendered template variable"
    document: dict[str, Any] = json.loads(text)
    return document


def scopes(definition: dict[str, Any]) -> list[dict[str, Any]]:
    """The top-level state machine plus every Map/Parallel branch (each is its own scope)."""
    found = [definition]
    for state in definition["States"].values():
        for branch in [
            state.get("ItemProcessor"),
            state.get("Iterator"),
            *state.get("Branches", []),
        ]:
            if branch:
                found += scopes(branch)
    return found


def all_states(definition: dict[str, Any]) -> dict[str, Any]:
    return {name: s for scope in scopes(definition) for name, s in scope["States"].items()}


@pytest.mark.parametrize("path", sorted(WORKFLOWS.glob("*.asl.json")), ids=lambda p: p.name)
def test_every_transition_targets_an_existing_state(path: Path) -> None:
    for scope in scopes(render(path)):
        states: dict[str, Any] = scope["States"]
        assert scope["StartAt"] in states
        targets = []
        for state in states.values():
            targets.append(state.get("Next"))
            targets.append(state.get("Default"))
            targets += [c.get("Next") for c in state.get("Choices", [])]
            targets += [c.get("Next") for c in state.get("Catch", [])]
        missing = {t for t in targets if t and t not in states}
        assert not missing, f"{path.name}: {missing}"


@pytest.mark.parametrize("path", sorted(WORKFLOWS.glob("*.asl.json")), ids=lambda p: p.name)
def test_spoke_api_calls_use_cross_account_credentials(path: Path) -> None:
    spoke_services = ("aws-sdk:ec2:", "aws-sdk:ssm:")  # act on instances in workload accounts
    for name, state in all_states(render(path)).items():
        resource = str(state.get("Resource", ""))
        if any(service in resource for service in spoke_services):
            assert "Credentials" in state, f"{name} would run in the hub account"
        elif "aws-sdk:dynamodb:" in resource:
            assert "Credentials" not in state, f"{name}: hub tables must use the hub's role"


def test_rollout_lambda_steps_retry_transient_errors() -> None:
    states = all_states(render(WORKFLOWS / "rollout.asl.json"))
    lambda_steps = [
        n for n, s in states.items() if "patchgate-rollout" in str(s.get("Resource", ""))
    ]
    assert len(lambda_steps) == 5
    assert all(states[n].get("Retry") for n in lambda_steps)


def test_rings_without_approval_release_with_no_decisions() -> None:
    rings = render(WORKFLOWS / "rollout.asl.json")["States"]["Rings"]["ItemProcessor"]["States"]
    no_approval = rings[rings["Approval?"]["Default"]]
    assert no_approval == {
        "Type": "Pass",
        "Result": [],
        "ResultPath": "$.decisions",
        "Next": "Release",
    }


def test_approval_waits_at_most_the_policy_timeout_and_expiry_is_not_approval() -> None:
    states = all_states(render(WORKFLOWS / "rollout.asl.json"))
    request = states["RequestApproval"]
    assert request["Resource"].endswith("lambda:invoke.waitForTaskToken")
    assert request["TimeoutSecondsPath"] == "$.timeoutSeconds"
    assert states["Expired"]["Parameters"]["decision"] == {"approved": False, "outcome": "expired"}


def test_bootstrap_started_by_rollout_skips_the_per_instance_email() -> None:
    states = render(WORKFLOWS / "bootstrap.asl.json")["States"]
    assert states["WhichEvent"]["Choices"][0]["Next"] == "InitFromRollout"
    assert states["InitFromRollout"]["Parameters"]["quiet"] is True
    assert states["TagSuccess"]["Next"] == "IsQuiet"


@pytest.mark.parametrize("path", sorted(WORKFLOWS.glob("*.asl.json")), ids=lambda p: p.name)
def test_selected_results_never_replace_the_state_input(path: Path) -> None:
    """Found in the lab: a ResultSelector without ResultPath replaced the input, and the next
    state's JSONPath failed with States.Runtime, which no Catch (not even States.ALL) handles."""
    for name, state in all_states(render(path)).items():
        if "ResultSelector" in state and state.get("Type") == "Task":
            assert "ResultPath" in state, f"{path.name}: {name} would replace its input"
