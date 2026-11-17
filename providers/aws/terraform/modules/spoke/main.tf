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
    resources = [for name, d in aws_ssm_document.bootstrap : d.arn if name != "Patchgate-Drift"]
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

resource "aws_ssm_document" "bootstrap" {
  # Content is generated from core/ by providers/aws/stacksets/generate.py and shared with
  # the StackSet template, so single-account and multi-account deployments stay identical.
  for_each = toset(["Patchgate-Bootstrap-Linux", "Patchgate-Bootstrap-Windows", "Patchgate-Drift"])

  name            = each.value
  document_type   = "Command"
  document_format = "JSON"
  content         = file("${local.repo_root}/providers/aws/ssm/${each.value}.json")
}

# ------------------------------------------------------------------ drift

resource "aws_ssm_association" "drift" {
  association_name            = "patchgate-drift"
  name                        = aws_ssm_document.bootstrap["Patchgate-Drift"].name
  schedule_expression         = "rate(30 minutes)" # the executor skips runs until the profile's interval is due
  apply_only_at_cron_interval = true
  max_concurrency             = "50%"
  max_errors                  = "100%"

  targets {
    key    = "tag:Patchgate:Managed"
    values = ["true"]
  }
}

resource "aws_cloudwatch_event_rule" "drift" {
  name          = "patchgate-forward-drift"
  description   = "Forward Patchgate drift findings to the hub."
  event_pattern = jsonencode({ source = ["io.patchgate"] })
}

resource "aws_cloudwatch_event_target" "drift" {
  rule     = aws_cloudwatch_event_rule.drift.name
  arn      = var.hub_event_bus_arn
  role_arn = aws_iam_role.forwarder.arn
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

data "aws_region" "current" {}

resource "aws_iam_role_policy" "instance_drift_events" {
  name = "publish-drift-events"
  role = aws_iam_role.instance.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "events:PutEvents"
      Resource  = "arn:${local.partition}:events:${data.aws_region.current.region}:${local.account_id}:event-bus/default"
      Condition = { StringEquals = { "events:source" = "io.patchgate" } }
    }]
  })
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
