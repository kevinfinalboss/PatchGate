# Hub: Patchgate control plane.
# - Central event bus that accepts events from accounts in the organization
# - Bootstrap workflow (Step Functions) started by EC2 "running" events
# - Resolver Lambda that assumes Patchgate-Execution in spokes and picks the install plan
# - SNS topic for notifications

terraform {
  required_version = ">= 1.10"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.6"
    }
  }
}

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  partition  = data.aws_partition.current.partition
  repo_root  = "${path.module}/../../../../.."

  # Patchgate-Execution in any account of the organization, so accounts onboarded by the
  # StackSet work without changing the hub. aws:ResourceOrgID keeps it inside the org, and
  # each spoke's trust policy only accepts these exact hub roles.
  # In single-account mode (no organization_id) only this account's role is assumable.
  single_account             = var.organization_id == null
  execution_role_arn_pattern = "arn:${local.partition}:iam::${local.single_account ? local.account_id : "*"}:role/${var.execution_role_name}"
}

# ------------------------------------------------------------------ event bus

resource "aws_cloudwatch_event_bus" "hub" {
  name = "patchgate-hub"
}

data "aws_iam_policy_document" "bus" {
  count = local.single_account ? 0 : 1

  statement {
    sid       = "AllowOrganizationAccounts"
    actions   = ["events:PutEvents"]
    resources = [aws_cloudwatch_event_bus.hub.arn]
    principals {
      type        = "AWS"
      identifiers = ["*"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:PrincipalOrgID"
      values   = [var.organization_id]
    }
  }
}

# Cross-account delivery needs a bus policy; same-account delivery does not.
resource "aws_cloudwatch_event_bus_policy" "hub" {
  count          = local.single_account ? 0 : 1
  event_bus_name = aws_cloudwatch_event_bus.hub.name
  policy         = data.aws_iam_policy_document.bus[0].json
}

resource "aws_cloudwatch_event_rule" "ec2_running" {
  name           = "patchgate-bootstrap-on-running"
  event_bus_name = aws_cloudwatch_event_bus.hub.name
  event_pattern = jsonencode({
    source        = ["aws.ec2"]
    "detail-type" = ["EC2 Instance State-change Notification"]
    detail        = { state = ["running"] }
  })
}

data "aws_iam_policy_document" "events_trust" {
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

resource "aws_iam_role" "events" {
  name               = "patchgate-hub-events"
  assume_role_policy = data.aws_iam_policy_document.events_trust.json
}

resource "aws_iam_role_policy" "events" {
  name = "start-workflows"
  role = aws_iam_role.events.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "states:StartExecution"
      Resource = [aws_sfn_state_machine.bootstrap.arn, aws_sfn_state_machine.notify_drift.arn]
    }]
  })
}

resource "aws_cloudwatch_event_target" "bootstrap" {
  rule           = aws_cloudwatch_event_rule.ec2_running.name
  event_bus_name = aws_cloudwatch_event_bus.hub.name
  arn            = aws_sfn_state_machine.bootstrap.arn
  role_arn       = aws_iam_role.events.arn
}

# ------------------------------------------------------------------ notifications

resource "aws_sns_topic" "notifications" {
  name              = "patchgate-notifications"
  kms_master_key_id = "alias/aws/sns"
}

resource "aws_sns_topic_subscription" "email" {
  count     = var.notification_email == null ? 0 : 1
  topic_arn = aws_sns_topic.notifications.arn
  protocol  = "email"
  endpoint  = var.notification_email
}

# ------------------------------------------------------------------ resolver lambda

data "archive_file" "resolver" {
  type        = "zip"
  output_path = "${path.root}/.build/resolver.zip"

  source {
    content  = file("${local.repo_root}/providers/aws/functions/resolver/handler.py")
    filename = "handler.py"
  }
}

data "aws_iam_policy_document" "lambda_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "resolver" {
  name               = "patchgate-hub-resolver"
  assume_role_policy = data.aws_iam_policy_document.lambda_trust.json
}

data "aws_iam_policy_document" "resolver" {
  statement {
    sid       = "AssumeExecutionRoleInOrganization"
    actions   = ["sts:AssumeRole"]
    resources = [local.execution_role_arn_pattern]
    dynamic "condition" {
      for_each = local.single_account ? [] : [var.organization_id]
      content {
        test     = "StringEquals"
        variable = "aws:ResourceOrgID"
        values   = [condition.value]
      }
    }
  }
  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.resolver.arn}:*"]
  }
  statement {
    sid       = "ReadConfiguration"
    actions   = ["dynamodb:GetItem", "dynamodb:Query"]
    resources = [aws_dynamodb_table.config.arn]
  }
}

resource "aws_iam_role_policy" "resolver" {
  name   = "patchgate-resolver"
  role   = aws_iam_role.resolver.id
  policy = data.aws_iam_policy_document.resolver.json
}

resource "aws_cloudwatch_log_group" "resolver" {
  name              = "/aws/lambda/patchgate-resolver"
  retention_in_days = var.log_retention_days
}

resource "aws_lambda_function" "resolver" {
  function_name    = "patchgate-resolver"
  description      = "Resolves an instance into its Patchgate install plan."
  role             = aws_iam_role.resolver.arn
  runtime          = "python3.12"
  handler          = "handler.handler"
  filename         = data.archive_file.resolver.output_path
  source_code_hash = data.archive_file.resolver.output_base64sha256
  timeout          = 30
  memory_size      = 256
  layers           = [aws_lambda_layer_version.core.arn]

  environment {
    variables = {
      EXECUTION_ROLE_NAME    = var.execution_role_name
      PATCHGATE_CONFIG_TABLE = aws_dynamodb_table.config.name
    }
  }

  depends_on = [aws_cloudwatch_log_group.resolver, aws_iam_role_policy.resolver]
}

# ------------------------------------------------------------------ bootstrap workflow

resource "aws_iam_role" "orchestrator" {
  name = "patchgate-hub-orchestrator"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "states.amazonaws.com" }
      Condition = { StringEquals = { "aws:SourceAccount" = local.account_id } }
    }]
  })
}

data "aws_iam_policy_document" "orchestrator" {
  statement {
    sid       = "InvokeResolver"
    actions   = ["lambda:InvokeFunction"]
    resources = [aws_lambda_function.resolver.arn]
  }
  statement {
    sid       = "AssumeExecutionRoleInOrganization"
    actions   = ["sts:AssumeRole"]
    resources = [local.execution_role_arn_pattern]
    dynamic "condition" {
      for_each = local.single_account ? [] : [var.organization_id]
      content {
        test     = "StringEquals"
        variable = "aws:ResourceOrgID"
        values   = [condition.value]
      }
    }
  }
  statement {
    sid       = "Notify"
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.notifications.arn]
  }
  statement {
    sid       = "XRayTracing"
    actions   = ["xray:PutTraceSegments", "xray:PutTelemetryRecords", "xray:GetSamplingRules", "xray:GetSamplingTargets"]
    resources = ["*"]
  }
  statement {
    sid = "ExecutionLogs"
    actions = [
      "logs:CreateLogDelivery", "logs:GetLogDelivery", "logs:UpdateLogDelivery",
      "logs:DeleteLogDelivery", "logs:ListLogDeliveries", "logs:PutResourcePolicy",
      "logs:DescribeResourcePolicies", "logs:DescribeLogGroups",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "orchestrator" {
  name   = "patchgate-orchestrator"
  role   = aws_iam_role.orchestrator.id
  policy = data.aws_iam_policy_document.orchestrator.json
}

resource "aws_cloudwatch_log_group" "workflow" {
  name              = "/aws/vendedlogs/states/patchgate-bootstrap"
  retention_in_days = var.log_retention_days
}

resource "aws_sfn_state_machine" "bootstrap" {
  name     = "patchgate-bootstrap"
  role_arn = aws_iam_role.orchestrator.arn
  definition = templatefile("${local.repo_root}/providers/aws/workflows/bootstrap.asl.json", {
    resolver_arn      = aws_lambda_function.resolver.arn
    topic_arn         = aws_sns_topic.notifications.arn
    ssm_wait_attempts = var.ssm_wait_attempts
  })

  tracing_configuration {
    enabled = true
  }

  logging_configuration {
    log_destination        = "${aws_cloudwatch_log_group.workflow.arn}:*"
    include_execution_data = true
    level                  = "ERROR"
  }

  depends_on = [aws_iam_role_policy.orchestrator]
}

# ------------------------------------------------------------------ drift notifications

resource "aws_cloudwatch_event_rule" "drift" {
  name           = "patchgate-drift-detected"
  description    = "Drift findings published by managed instances."
  event_bus_name = aws_cloudwatch_event_bus.hub.name
  event_pattern = jsonencode({
    source        = ["io.patchgate"]
    "detail-type" = ["Patchgate Drift Detected"]
  })
}

resource "aws_cloudwatch_event_target" "drift" {
  rule           = aws_cloudwatch_event_rule.drift.name
  event_bus_name = aws_cloudwatch_event_bus.hub.name
  arn            = aws_sfn_state_machine.notify_drift.arn
  role_arn       = aws_iam_role.events.arn
}

resource "aws_cloudwatch_log_group" "notify_drift" {
  name              = "/aws/vendedlogs/states/patchgate-notify-drift"
  retention_in_days = var.log_retention_days
}

resource "aws_sfn_state_machine" "notify_drift" {
  name     = "patchgate-notify-drift"
  role_arn = aws_iam_role.orchestrator.arn
  definition = templatefile("${local.repo_root}/providers/aws/workflows/notify-drift.asl.json", {
    topic_arn = aws_sns_topic.notifications.arn
  })

  tracing_configuration {
    enabled = true
  }

  logging_configuration {
    log_destination        = "${aws_cloudwatch_log_group.notify_drift.arn}:*"
    include_execution_data = true
    level                  = "ERROR"
  }

  depends_on = [aws_iam_role_policy.orchestrator]
}
