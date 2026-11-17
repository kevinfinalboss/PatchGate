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
}


def render(path: Path) -> dict[str, Any]:
    text = path.read_text()
    for key, value in SAMPLE_VARS.items():
        text = text.replace("${" + key + "}", value)
    assert "${" not in text, "unrendered template variable"
    document: dict[str, Any] = json.loads(text)
    return document


@pytest.mark.parametrize("path", sorted(WORKFLOWS.glob("*.asl.json")), ids=lambda p: p.name)
def test_every_transition_targets_an_existing_state(path: Path) -> None:
    definition = render(path)
    states: dict[str, Any] = definition["States"]
    assert definition["StartAt"] in states
    targets = []
    for state in states.values():
        targets.append(state.get("Next"))
        targets.append(state.get("Default"))
        targets += [c.get("Next") for c in state.get("Choices", [])]
        targets += [c.get("Next") for c in state.get("Catch", [])]
    missing = {t for t in targets if t and t not in states}
    assert not missing


@pytest.mark.parametrize("path", sorted(WORKFLOWS.glob("*.asl.json")), ids=lambda p: p.name)
def test_spoke_api_calls_use_cross_account_credentials(path: Path) -> None:
    for name, state in render(path)["States"].items():
        if str(state.get("Resource", "")).startswith("arn:aws:states:::aws-sdk:"):
            assert "Credentials" in state, f"{name} would run in the hub account"
