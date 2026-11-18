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
# The template is uploaded to S3 (TemplateURL limit: 1 MB); stay far below it.
MAX_TEMPLATE_BYTES = 460_800

HUB_ROLE_NAMES = [
    "patchgate-hub-orchestrator",
    "patchgate-hub-resolver",
    "patchgate-hub-rollout-fn",
]
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


def linux_document(installer: str, ansible_lock: str) -> dict[str, Any]:
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
                        "cat > /opt/patchgate/ansible-requirements.txt <<'PATCHGATE_EOF'\n"
                        f"{ansible_lock}\nPATCHGATE_EOF",
                        "python3 /opt/patchgate/patchgate_install.py --plan-b64 '{{ Plan }}'",
                    ],
                },
            }
        ],
    }


def windows_document(installer: str, dsc_module: str) -> dict[str, Any]:
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
                        f"$dscModule = @'\n{dsc_module}\n'@",
                        "Set-Content -Path (Join-Path $dir 'PatchgateDsc.psm1') "
                        "-Value $dscModule -Encoding UTF8",
                        "& (Join-Path $dir 'Install-PatchgatePlan.ps1') -PlanBase64 '{{ Plan }}'",
                        "exit $LASTEXITCODE",
                    ],
                },
            }
        ],
    }


# Changing these tags on a running instance re-plans it (Patchgate's own Bootstrap* tags do not).
TRIGGER_TAG_KEYS = ["Patchgate:Managed", "Patchgate:Profile", "Patchgate:AppSets"]

DRIFT_DOCUMENT = "Patchgate-Drift"
# Every 30 minutes; the executor skips runs until the profile's interval is due.
DRIFT_SCHEDULE = "cron(0/30 * * * ? *)"
DRIFT_SOURCE = "io.patchgate"
DRIFT_DETAIL_TYPE = "Patchgate Drift Detected"

LINUX_DRIFT = f"""set -uo pipefail
INSTALLER=/opt/patchgate/patchgate_install.py
EVENT_FILE=/var/lib/patchgate/drift-event.json
if [ ! -f "$INSTALLER" ]; then echo "not bootstrapped by Patchgate; skipping"; exit 0; fi
if [ "{{{{ Force }}}}" = "true" ]; then python3 "$INSTALLER" --drift --force; else python3 "$INSTALLER" --drift; fi
rc=$?
if [ -f "$EVENT_FILE" ]; then
  TOKEN=$(curl -sf -X PUT http://169.254.169.254/latest/api/token -H 'X-aws-ec2-metadata-token-ttl-seconds: 60')
  md() {{ curl -sf -H "X-aws-ec2-metadata-token: $TOKEN" "http://169.254.169.254/latest/meta-data/$1"; }}
  REGION=$(md placement/region)
  IID=$(md instance-id)
  ENTRIES=$(mktemp)
  python3 - "$EVENT_FILE" "$IID" > "$ENTRIES" <<'PY'
import json, sys
detail = json.load(open(sys.argv[1]))
detail["instanceId"] = sys.argv[2]
print(json.dumps([{{"Source": "{DRIFT_SOURCE}", "DetailType": "{DRIFT_DETAIL_TYPE}", "Detail": json.dumps(detail)}}]))
PY
  if [ "$(aws events put-events --region "$REGION" --entries "file://$ENTRIES" --query FailedEntryCount --output text)" = "0" ]; then
    rm -f "$EVENT_FILE"; echo "drift event published"
  else
    echo "failed to publish drift event" >&2
  fi
  rm -f "$ENTRIES"
fi
exit $rc
"""

WINDOWS_DRIFT = f"""$installer = Join-Path $env:ProgramData 'Patchgate\\bin\\Install-PatchgatePlan.ps1'
$eventFile = Join-Path $env:ProgramData 'Patchgate\\drift-event.json'
if (-not (Test-Path $installer)) {{ 'not bootstrapped by Patchgate; skipping'; exit 0 }}
if ('{{{{ Force }}}}' -eq 'true') {{ & $installer -Drift -Force }} else {{ & $installer -Drift }}
$rc = $LASTEXITCODE
if (Test-Path $eventFile) {{
  $token = Invoke-RestMethod -Method Put -Uri 'http://169.254.169.254/latest/api/token' -Headers @{{ 'X-aws-ec2-metadata-token-ttl-seconds' = '60' }}
  $h = @{{ 'X-aws-ec2-metadata-token' = $token }}
  $region = Invoke-RestMethod -Headers $h -Uri 'http://169.254.169.254/latest/meta-data/placement/region'
  $iid = Invoke-RestMethod -Headers $h -Uri 'http://169.254.169.254/latest/meta-data/instance-id'
  $detail = Get-Content -Raw -Path $eventFile | ConvertFrom-Json
  $detail | Add-Member -NotePropertyName instanceId -NotePropertyValue $iid -Force
  $detailJson = $detail | ConvertTo-Json -Depth 8 -Compress
  $published = $false
  $aws = (Get-Command aws.exe -ErrorAction SilentlyContinue).Source
  if (-not $aws -and (Test-Path 'C:\\Program Files\\Amazon\\AWSCLIV2\\aws.exe')) {{ $aws = 'C:\\Program Files\\Amazon\\AWSCLIV2\\aws.exe' }}
  if ($aws) {{
    $entries = '[' + (@{{ Source = '{DRIFT_SOURCE}'; DetailType = '{DRIFT_DETAIL_TYPE}'; Detail = $detailJson }} | ConvertTo-Json -Compress) + ']'
    $tmp = Join-Path $env:TEMP 'patchgate-entries.json'
    [IO.File]::WriteAllText($tmp, $entries, (New-Object Text.UTF8Encoding $false))
    $failed = & $aws events put-events --region $region --entries "file://$tmp" --query FailedEntryCount --output text
    $published = ($LASTEXITCODE -eq 0 -and "$failed".Trim() -eq '0')
    Remove-Item -Path $tmp -Force
  }}
  elseif (Get-Command Write-EVBEvent -ErrorAction SilentlyContinue) {{
    $entry = New-Object Amazon.EventBridge.Model.PutEventsRequestEntry
    $entry.Source = '{DRIFT_SOURCE}'; $entry.DetailType = '{DRIFT_DETAIL_TYPE}'; $entry.Detail = $detailJson
    $published = ((Write-EVBEvent -Entry $entry -Region $region).FailedEntryCount -eq 0)
  }}
  if ($published) {{ Remove-Item -Path $eventFile -Force; 'drift event published' }} else {{ Write-Warning 'failed to publish drift event' }}
}}
exit $rc
"""


def drift_document() -> dict[str, Any]:
    return {
        "schemaVersion": "2.2",
        "description": "Patchgate drift check: re-validates agents, apps and hardening, publishes findings.",
        "parameters": {
            "Force": {
                "type": "String",
                "description": "true runs now, ignoring the interval and maintenance window (reconcile).",
                "allowedValues": ["false", "true"],
                "default": "false",
            }
        },
        "mainSteps": [
            {
                "action": "aws:runShellScript",
                "name": "driftLinux",
                "precondition": {"StringEquals": ["platformType", "Linux"]},
                "inputs": {"timeoutSeconds": "1800", "runCommand": [LINUX_DRIFT]},
            },
            {
                "action": "aws:runPowerShellScript",
                "name": "driftWindows",
                "precondition": {"StringEquals": ["platformType", "Windows"]},
                "inputs": {"timeoutSeconds": "1800", "runCommand": [WINDOWS_DRIFT]},
            },
        ],
    }


def documents() -> dict[str, dict[str, Any]]:
    scripts = ROOT / "core" / "scripts"
    linux = (scripts / "linux" / "patchgate_install.py").read_text()
    ansible_lock = (scripts / "linux" / "ansible-requirements.txt").read_text()
    windows = (scripts / "windows" / "Install-PatchgatePlan.ps1").read_text().replace("\r\n", "\n")
    dsc_module = (scripts / "windows" / "PatchgateDsc.psm1").read_text().replace("\r\n", "\n")
    return {
        "Patchgate-Bootstrap-Linux": linux_document(linux, ansible_lock),
        "Patchgate-Bootstrap-Windows": windows_document(windows, dsc_module),
        DRIFT_DOCUMENT: drift_document(),
    }


def sub(text: str) -> dict[str, str]:
    return {"Fn::Sub": text}


def template(docs: dict[str, dict[str, Any]]) -> dict[str, Any]:
    doc_arns = [
        sub(f"arn:${{AWS::Partition}}:ssm:${{AWS::Region}}:${{AWS::AccountId}}:document/{name}")
        for name in docs
        if name.startswith("Patchgate-Bootstrap-")  # the hub only runs bootstrap documents
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
        "ForwardTagChanges": {
            "Type": "AWS::Events::Rule",
            "Properties": {
                "Name": "patchgate-forward-tag-changes",
                "Description": "Forward Patchgate tag changes on instances to the hub.",
                "EventPattern": {
                    "source": ["aws.tag"],
                    "detail-type": ["Tag Change on Resource"],
                    "detail": {
                        "service": ["ec2"],
                        "resource-type": ["instance"],
                        "changed-tag-keys": TRIGGER_TAG_KEYS,
                    },
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
        "ForwardDriftEvents": {
            "Type": "AWS::Events::Rule",
            "Properties": {
                "Name": "patchgate-forward-drift",
                "Description": "Forward Patchgate drift findings to the hub.",
                "EventPattern": {"source": [DRIFT_SOURCE]},
                "Targets": [
                    {
                        "Id": "hub",
                        "Arn": {"Ref": "HubEventBusArn"},
                        "RoleArn": {"Fn::GetAtt": ["ForwarderRole", "Arn"]},
                    }
                ],
            },
        },
        "DriftAssociation": {
            "Type": "AWS::SSM::Association",
            "Properties": {
                "AssociationName": "patchgate-drift",
                "Name": {"Ref": DRIFT_DOCUMENT.replace("-", "")},
                "Targets": [{"Key": "tag:Patchgate:Managed", "Values": ["true"]}],
                "ScheduleExpression": DRIFT_SCHEDULE,
                "ApplyOnlyAtCronInterval": True,
                "MaxConcurrency": "50%",
                "MaxErrors": "100%",
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
                "Policies": [
                    {
                        "PolicyName": "publish-drift-events",
                        "PolicyDocument": {
                            "Version": "2012-10-17",
                            "Statement": [
                                {
                                    "Effect": "Allow",
                                    "Action": "events:PutEvents",
                                    "Resource": sub(
                                        "arn:${AWS::Partition}:events:${AWS::Region}:${AWS::AccountId}:event-bus/default"
                                    ),
                                    "Condition": {"StringEquals": {"events:source": DRIFT_SOURCE}},
                                }
                            ],
                        },
                    }
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
