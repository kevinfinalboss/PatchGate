# Spoke: the minimal footprint Patchgate needs in each workload account.
# - Patchgate-Execution role, assumable only by the hub's orchestrator and resolver roles
# - EventBridge rule forwarding EC2 "running" events to the hub bus
# - SSM documents embedding the core installers
# - Instance profile for managed instances and log groups

terraform {
  required_version = ">= 1.10"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  partition  = data.aws_partition.current.partition
  repo_root  = "${path.module}/../../../../.."

  hub_role_arns = [
    for name in var.hub_role_names : "arn:${local.partition}:iam::${var.hub_account_id}:role/${name}"
  ]
}

# ------------------------------------------------------------------ execution role

data "aws_iam_policy_document" "execution_trust" {
  statement {
    actions = ["sts:AssumeRole", "sts:TagSession"]
    principals {
      type        = "AWS"
      identifiers = local.hub_role_arns
    }
  }
}

resource "aws_iam_role" "execution" {
  name                 = "Patchgate-Execution"
  description          = "Assumed by the Patchgate hub to bootstrap and patch managed instances."
  assume_role_policy   = data.aws_iam_policy_document.execution_trust.json
  max_session_duration = 3600
}

data "aws_iam_policy_document" "execution" {
  statement {
    sid       = "Describe"
    actions   = ["ec2:DescribeInstances", "ssm:DescribeInstanceInformation"]
    resources = ["*"]
  }

  statement {
    sid       = "TagOnlyPatchgateKeys"
    actions   = ["ec2:CreateTags"]
    resources = ["arn:${local.partition}:ec2:*:${local.account_id}:instance/*"]
    condition {
      test     = "ForAllValues:StringLike"
      variable = "aws:TagKeys"
      values   = ["Patchgate:*"]
    }
  }

  statement {
    sid       = "RunPatchgateDocuments"
    actions   = ["ssm:SendCommand"]
    resources = [for d in aws_ssm_document.bootstrap : d.arn]
  }

  statement {
    sid       = "RunOnManagedInstancesOnly"
    actions   = ["ssm:SendCommand"]
    resources = ["arn:${local.partition}:ec2:*:${local.account_id}:instance/*"]
    condition {
      test     = "StringEquals"
      variable = "ssm:resourceTag/Patchgate:Managed"
      values   = ["true"]
    }
  }

  statement {
    sid       = "ReadCommandResults"
    actions   = ["ssm:GetCommandInvocation", "ssm:ListCommandInvocations"]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "execution" {
  name   = "patchgate-execution"
  role   = aws_iam_role.execution.id
  policy = data.aws_iam_policy_document.execution.json
}

# ------------------------------------------------------------------ event forwarding

resource "aws_cloudwatch_event_rule" "ec2_running" {
  name        = "patchgate-forward-ec2-running"
  description = "Forward EC2 running events to the Patchgate hub."
  event_pattern = jsonencode({
    source        = ["aws.ec2"]
    "detail-type" = ["EC2 Instance State-change Notification"]
    detail        = { state = ["running"] }
  })
}

data "aws_iam_policy_document" "forwarder_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["events.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

resource "aws_iam_role" "forwarder" {
  name               = "patchgate-event-forwarder"
  assume_role_policy = data.aws_iam_policy_document.forwarder_trust.json
}

resource "aws_iam_role_policy" "forwarder" {
  name = "put-events-to-hub"
  role = aws_iam_role.forwarder.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "events:PutEvents"
      Resource = var.hub_event_bus_arn
    }]
  })
}

resource "aws_cloudwatch_event_target" "hub" {
  rule     = aws_cloudwatch_event_rule.ec2_running.name
  arn      = var.hub_event_bus_arn
  role_arn = aws_iam_role.forwarder.arn
}

# ------------------------------------------------------------------ SSM documents

locals {
  plan_parameter = {
    Plan = {
      type           = "String"
      description    = "Install plan produced by the Patchgate catalog (gz1:<base64>)."
      allowedPattern = "^(gz1:)?[A-Za-z0-9+/=]{1,4000}$"
    }
  }

  linux_installer   = file("${local.repo_root}/core/scripts/linux/patchgate_install.py")
  windows_installer = file("${local.repo_root}/core/scripts/windows/Install-PatchgatePlan.ps1")
}

resource "aws_ssm_document" "bootstrap" {
  for_each = {
    linux = {
      name = "Patchgate-Bootstrap-Linux"
      content = jsonencode({
        schemaVersion = "2.2"
        description   = "Patchgate bootstrap for Linux: applies an install plan with the core installer."
        parameters    = local.plan_parameter
        mainSteps = [{
          action       = "aws:runShellScript"
          name         = "applyPlan"
          precondition = { StringEquals = ["platformType", "Linux"] }
          inputs = {
            timeoutSeconds = "1800"
            runCommand = [
              "set -euo pipefail",
              "install -d -m 0700 /opt/patchgate",
              "cat > /opt/patchgate/patchgate_install.py <<'PATCHGATE_EOF'\n${local.linux_installer}\nPATCHGATE_EOF",
              "python3 /opt/patchgate/patchgate_install.py --plan-b64 '{{ Plan }}'",
            ]
          }
        }]
      })
    }
    windows = {
      name = "Patchgate-Bootstrap-Windows"
      content = jsonencode({
        schemaVersion = "2.2"
        description   = "Patchgate bootstrap for Windows: applies an install plan with the core installer."
        parameters    = local.plan_parameter
        mainSteps = [{
          action       = "aws:runPowerShellScript"
          name         = "applyPlan"
          precondition = { StringEquals = ["platformType", "Windows"] }
          inputs = {
            timeoutSeconds = "1800"
            runCommand = [
              "$ErrorActionPreference = 'Stop'",
              "$dir = Join-Path $env:ProgramData 'Patchgate\\bin'",
              "New-Item -ItemType Directory -Force -Path $dir | Out-Null",
              "$installer = @'\n${local.windows_installer}\n'@",
              "Set-Content -Path (Join-Path $dir 'Install-PatchgatePlan.ps1') -Value $installer -Encoding UTF8",
              "& (Join-Path $dir 'Install-PatchgatePlan.ps1') -PlanBase64 '{{ Plan }}'",
              "exit $LASTEXITCODE",
            ]
          }
        }]
      })
    }
  }

  name            = each.value.name
  document_type   = "Command"
  document_format = "JSON"
  content         = each.value.content
}

# ------------------------------------------------------------------ instance profile & logs

data "aws_iam_policy_document" "instance_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "instance" {
  name               = "Patchgate-Instance"
  assume_role_policy = data.aws_iam_policy_document.instance_trust.json
}

resource "aws_iam_role_policy_attachment" "instance" {
  for_each = toset([
    "arn:${local.partition}:iam::aws:policy/AmazonSSMManagedInstanceCore",
    "arn:${local.partition}:iam::aws:policy/CloudWatchAgentServerPolicy",
  ])
  role       = aws_iam_role.instance.name
  policy_arn = each.value
}

resource "aws_iam_instance_profile" "instance" {
  name = "Patchgate-Instance"
  role = aws_iam_role.instance.name
}

resource "aws_cloudwatch_log_group" "this" {
  for_each          = toset(["/patchgate/bootstrap", "/patchgate/instances"])
  name              = each.value
  retention_in_days = var.log_retention_days
}
