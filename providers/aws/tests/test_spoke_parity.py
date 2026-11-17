"""The spoke exists twice (Terraform module for single-account, StackSet for multi-account).

These tests keep both definitions aligned so an instance behaves the same in either mode.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

AWS = Path(__file__).resolve().parents[1]
TEMPLATE = json.loads((AWS / "stacksets" / "spoke.template.json").read_text())
MODULE = (AWS / "terraform" / "modules" / "spoke" / "main.tf").read_text()


def template_names(resource_type: str, key: str) -> set[str]:
    return {
        r["Properties"][key] for r in TEMPLATE["Resources"].values() if r["Type"] == resource_type
    }


def module_names(resource_type: str) -> set[str]:
    pattern = rf'resource "{resource_type}" "\w+" \{{.*?\n  name\s+= "([^"]+)"'
    return set(re.findall(pattern, MODULE, flags=re.S))


def test_iam_role_names_match() -> None:
    assert template_names("AWS::IAM::Role", "RoleName") == module_names("aws_iam_role")


def test_event_rule_names_match() -> None:
    assert template_names("AWS::Events::Rule", "Name") == module_names("aws_cloudwatch_event_rule")


def test_ssm_documents_and_log_groups_match() -> None:
    assert template_names("AWS::SSM::Document", "Name") == set(
        re.findall(r'"(Patchgate-Bootstrap-\w+)"', MODULE)
    )
    assert template_names("AWS::Logs::LogGroup", "LogGroupName") == set(
        re.findall(r'"(/patchgate/[a-z]+)"', MODULE)
    )


def test_execution_role_permissions_match() -> None:
    role: dict[str, Any] = TEMPLATE["Resources"]["ExecutionRole"]["Properties"]
    sids = {s["Sid"] for s in role["Policies"][0]["PolicyDocument"]["Statement"]}
    assert sids == set(re.findall(r'sid\s+= "(\w+)"', MODULE.split("forwarder_trust")[0]))


def test_generated_artifacts_are_up_to_date() -> None:
    result = subprocess.run(  # noqa: S603
        [sys.executable, str(AWS / "stacksets" / "generate.py"), "--check"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout
