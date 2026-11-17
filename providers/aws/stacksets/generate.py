"""Generate the AWS spoke artifacts from core/ (run via `make spoke-artifacts`).

Outputs (committed; CI fails if they are stale):
  providers/aws/ssm/Patchgate-Bootstrap-{Linux,Windows}.json  SSM document content
  providers/aws/stacksets/spoke.template.json                 CloudFormation for the StackSet

The Terraform spoke module (single-account mode) and the StackSet (multi-account mode)
both consume the same generated SSM documents, so the two deployment paths cannot drift.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
SSM_DIR = ROOT / "providers" / "aws" / "ssm"
TEMPLATE = ROOT / "providers" / "aws" / "stacksets" / "spoke.template.json"
MAX_TEMPLATE_BYTES = 51_200  # CloudFormation inline TemplateBody limit

HUB_ROLE_NAMES = ["patchgate-hub-orchestrator", "patchgate-hub-resolver"]
LOG_GROUPS = {
    "BootstrapLogGroup": "/patchgate/bootstrap",
    "InstanceLogGroup": "/patchgate/instances",
}

PLAN_PARAMETER = {
    "Plan": {
        "type": "String",
        "description": "Install plan produced by the Patchgate catalog (gz1:<base64>).",
        "allowedPattern": "^(gz1:)?[A-Za-z0-9+/=]+$",
        "maxChars": 4000,
    }
}


def linux_document(installer: str) -> dict[str, Any]:
    return {
        "schemaVersion": "2.2",
        "description": "Patchgate bootstrap for Linux: applies an install plan with the core installer.",
        "parameters": PLAN_PARAMETER,
        "mainSteps": [
            {
                "action": "aws:runShellScript",
                "name": "applyPlan",
                "precondition": {"StringEquals": ["platformType", "Linux"]},
                "inputs": {
                    "timeoutSeconds": "1800",
                    "runCommand": [
                        "set -euo pipefail",
                        "install -d -m 0700 /opt/patchgate",
                        "cat > /opt/patchgate/patchgate_install.py <<'PATCHGATE_EOF'\n"
                        f"{installer}\nPATCHGATE_EOF",
                        "python3 /opt/patchgate/patchgate_install.py --plan-b64 '{{ Plan }}'",
                    ],
                },
            }
        ],
    }


def windows_document(installer: str) -> dict[str, Any]:
    return {
        "schemaVersion": "2.2",
        "description": "Patchgate bootstrap for Windows: applies an install plan with the core installer.",
        "parameters": PLAN_PARAMETER,
        "mainSteps": [
            {
                "action": "aws:runPowerShellScript",
                "name": "applyPlan",
                "precondition": {"StringEquals": ["platformType", "Windows"]},
                "inputs": {
                    "timeoutSeconds": "1800",
                    "runCommand": [
                        "$ErrorActionPreference = 'Stop'",
                        "$dir = Join-Path $env:ProgramData 'Patchgate\\bin'",
                        "New-Item -ItemType Directory -Force -Path $dir | Out-Null",
                        f"$installer = @'\n{installer}\n'@",
                        "Set-Content -Path (Join-Path $dir 'Install-PatchgatePlan.ps1') "
                        "-Value $installer -Encoding UTF8",
                        "& (Join-Path $dir 'Install-PatchgatePlan.ps1') -PlanBase64 '{{ Plan }}'",
                        "exit $LASTEXITCODE",
                    ],
                },
            }
        ],
    }


def documents() -> dict[str, dict[str, Any]]:
    scripts = ROOT / "core" / "scripts"
    linux = (scripts / "linux" / "patchgate_install.py").read_text()
    windows = (scripts / "windows" / "Install-PatchgatePlan.ps1").read_text().replace("\r\n", "\n")
    return {
        "Patchgate-Bootstrap-Linux": linux_document(linux),
        "Patchgate-Bootstrap-Windows": windows_document(windows),
    }


def sub(text: str) -> dict[str, str]:
    return {"Fn::Sub": text}


def template(docs: dict[str, dict[str, Any]]) -> dict[str, Any]:
    doc_arns = [
        sub(f"arn:${{AWS::Partition}}:ssm:${{AWS::Region}}:${{AWS::AccountId}}:document/{name}")
        for name in docs
    ]
    instance_arn = sub("arn:${AWS::Partition}:ec2:*:${AWS::AccountId}:instance/*")
    resources: dict[str, Any] = {
        "ExecutionRole": {
            "Type": "AWS::IAM::Role",
            "Properties": {
                "RoleName": "Patchgate-Execution",
                "Description": "Assumed by the Patchgate hub to bootstrap and patch managed instances.",
                "MaxSessionDuration": 3600,
                "AssumeRolePolicyDocument": {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Action": ["sts:AssumeRole", "sts:TagSession"],
                            "Principal": {
                                "AWS": [
                                    sub(f"arn:${{AWS::Partition}}:iam::${{HubAccountId}}:role/{n}")
                                    for n in HUB_ROLE_NAMES
                                ]
                            },
                        }
                    ],
                },
                "Policies": [
                    {
                        "PolicyName": "patchgate-execution",
                        "PolicyDocument": {
                            "Version": "2012-10-17",
                            "Statement": [
                                {
                                    "Sid": "Describe",
                                    "Effect": "Allow",
                                    "Action": [
                                        "ec2:DescribeInstances",
                                        "ssm:DescribeInstanceInformation",
                                    ],
                                    "Resource": "*",
                                },
                                {
                                    "Sid": "TagOnlyPatchgateKeys",
                                    "Effect": "Allow",
                                    "Action": "ec2:CreateTags",
                                    "Resource": instance_arn,
                                    "Condition": {
                                        "ForAllValues:StringLike": {"aws:TagKeys": ["Patchgate:*"]}
                                    },
                                },
                                {
                                    "Sid": "RunPatchgateDocuments",
                                    "Effect": "Allow",
                                    "Action": "ssm:SendCommand",
                                    "Resource": doc_arns,
                                },
                                {
                                    "Sid": "RunOnManagedInstancesOnly",
                                    "Effect": "Allow",
                                    "Action": "ssm:SendCommand",
                                    "Resource": instance_arn,
                                    "Condition": {
                                        "StringEquals": {
                                            "ssm:resourceTag/Patchgate:Managed": "true"
                                        }
                                    },
                                },
                                {
                                    "Sid": "ReadCommandResults",
                                    "Effect": "Allow",
                                    "Action": [
                                        "ssm:GetCommandInvocation",
                                        "ssm:ListCommandInvocations",
                                    ],
                                    "Resource": "*",
                                },
                            ],
                        },
                    }
                ],
            },
        },
        "ForwarderRole": {
            "Type": "AWS::IAM::Role",
            "Properties": {
                "RoleName": "patchgate-event-forwarder",
                "AssumeRolePolicyDocument": {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Action": "sts:AssumeRole",
                            "Principal": {"Service": "events.amazonaws.com"},
                            "Condition": {
                                "StringEquals": {"aws:SourceAccount": {"Ref": "AWS::AccountId"}}
                            },
                        }
                    ],
                },
                "Policies": [
                    {
                        "PolicyName": "put-events-to-hub",
                        "PolicyDocument": {
                            "Version": "2012-10-17",
                            "Statement": [
                                {
                                    "Effect": "Allow",
                                    "Action": "events:PutEvents",
                                    "Resource": {"Ref": "HubEventBusArn"},
                                }
                            ],
                        },
                    }
                ],
            },
        },
        "ForwardEc2Running": {
            "Type": "AWS::Events::Rule",
            "Properties": {
                "Name": "patchgate-forward-ec2-running",
                "Description": "Forward EC2 running events to the Patchgate hub.",
                "EventPattern": {
                    "source": ["aws.ec2"],
                    "detail-type": ["EC2 Instance State-change Notification"],
                    "detail": {"state": ["running"]},
                },
                "Targets": [
                    {
                        "Id": "hub",
                        "Arn": {"Ref": "HubEventBusArn"},
                        "RoleArn": {"Fn::GetAtt": ["ForwarderRole", "Arn"]},
                    }
                ],
            },
        },
        "InstanceRole": {
            "Type": "AWS::IAM::Role",
            "Properties": {
                "RoleName": "Patchgate-Instance",
                "AssumeRolePolicyDocument": {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Action": "sts:AssumeRole",
                            "Principal": {"Service": "ec2.amazonaws.com"},
                        }
                    ],
                },
                "ManagedPolicyArns": [
                    sub("arn:${AWS::Partition}:iam::aws:policy/AmazonSSMManagedInstanceCore"),
                    sub("arn:${AWS::Partition}:iam::aws:policy/CloudWatchAgentServerPolicy"),
                ],
            },
        },
        "InstanceProfile": {
            "Type": "AWS::IAM::InstanceProfile",
            "Properties": {
                "InstanceProfileName": "Patchgate-Instance",
                "Roles": [{"Ref": "InstanceRole"}],
            },
        },
    }
    for logical, name in LOG_GROUPS.items():
        resources[logical] = {
            "Type": "AWS::Logs::LogGroup",
            "Properties": {"LogGroupName": name, "RetentionInDays": {"Ref": "LogRetentionDays"}},
        }
    for name, content in docs.items():
        logical = name.replace("-", "")
        resources[logical] = {
            "Type": "AWS::SSM::Document",
            "Properties": {
                "Name": name,
                "DocumentType": "Command",
                "DocumentFormat": "JSON",
                "UpdateMethod": "NewVersion",
                "Content": content,
            },
        }
    return {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Description": "Patchgate spoke (generated by providers/aws/stacksets/generate.py - do not edit)",
        "Parameters": {
            "HubAccountId": {"Type": "String", "AllowedPattern": "^\\d{12}$"},
            "HubEventBusArn": {"Type": "String"},
            "LogRetentionDays": {"Type": "Number", "Default": 14},
        },
        "Resources": resources,
        "Outputs": {
            "ExecutionRoleArn": {"Value": {"Fn::GetAtt": ["ExecutionRole", "Arn"]}},
            "InstanceProfileName": {"Value": {"Ref": "InstanceProfile"}},
        },
    }


def render() -> dict[Path, str]:
    docs = documents()
    outputs = {
        SSM_DIR / f"{name}.json": json.dumps(content, indent=2) + "\n"
        for name, content in docs.items()
    }
    body = json.dumps(template(docs), separators=(",", ":"))
    if len(body.encode()) > MAX_TEMPLATE_BYTES:
        raise SystemExit(f"template is {len(body.encode())} bytes (max {MAX_TEMPLATE_BYTES})")
    outputs[TEMPLATE] = body + "\n"
    return outputs


def main() -> int:
    check = "--check" in sys.argv
    stale = []
    for path, text in render().items():
        if check:
            if not path.exists() or path.read_text() != text:
                stale.append(str(path.relative_to(ROOT)))
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
            print(f"wrote {path.relative_to(ROOT)} ({len(text.encode())} bytes)")
    if stale:
        print("stale generated files (run `make spoke-artifacts`):", *stale, sep="\n  ")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
