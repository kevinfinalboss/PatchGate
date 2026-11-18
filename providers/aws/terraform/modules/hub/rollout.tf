# Approval engine (ADR 0010): rollout workflow, approval requests, approval API and email.
#
# Flow: a profile or app set changes -> the config audit function starts patchgate-rollout ->
# ring by ring, approver groups are asked by email (SES) -> approved groups get the revision and
# their instances re-plan through the bootstrap workflow -> failures revert the ring.

data "aws_region" "current" {}

locals {
  region = data.aws_region.current.region
  # Built from the fixed name so the Lambda policies do not depend on the state machine (which
  # depends on the Lambdas).
  rollout_state_machine_arn = "arn:${local.partition}:states:${local.region}:${local.account_id}:stateMachine:patchgate-rollout"
  rollout_executions_arn    = "arn:${local.partition}:states:${local.region}:${local.account_id}:execution:patchgate-rollout:*"
  approval_sender           = coalesce(var.approval_sender_email, var.notification_email, "unset@example.invalid")
  signing_key_parameter     = "/patchgate/approvals/signing-key"
}

# ------------------------------------------------------------------ approvals table

# One item per approval request: who was asked, who decided what and when (audit evidence).
# Holds the Step Functions task token, which never leaves the hub.
resource "aws_dynamodb_table" "approvals" {
  name                        = "patchgate-approvals"
  billing_mode                = "PAY_PER_REQUEST"
  hash_key                    = "requestId"
  deletion_protection_enabled = var.protect_config

  attribute {
    name = "requestId"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled = true
  }
}

# ------------------------------------------------------------------ email (SES)

# Sender identity. AWS emails a verification link once; until it is clicked nothing is sent.
# In the SES sandbox every approver address must also be a verified identity.
resource "aws_sesv2_email_identity" "approvals" {
  email_identity = local.approval_sender
}

# ------------------------------------------------------------------ rollout function

data "archive_file" "rollout" {
  type        = "zip"
  output_path = "${path.root}/.build/rollout.zip"
  source {
    content  = file("${local.repo_root}/providers/aws/functions/rollout/handler.py")
    filename = "handler.py"
  }
}

resource "aws_iam_role" "rollout" {
  name               = "patchgate-hub-rollout-fn"
  assume_role_policy = data.aws_iam_policy_document.lambda_trust.json
}

data "aws_iam_policy_document" "rollout" {
  statement {
    sid       = "ReadConfiguration"
    actions   = ["dynamodb:GetItem", "dynamodb:Query"]
    resources = [aws_dynamodb_table.config.arn, aws_dynamodb_table.history.arn]
  }
  statement {
    sid       = "WriteReleases"
    actions   = ["dynamodb:GetItem", "dynamodb:PutItem"]
    resources = [aws_dynamodb_table.releases.arn]
  }
  statement {
    sid       = "DiscoverInstancesInOrganization"
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
    sid       = "FindOlderRollouts"
    actions   = ["states:ListExecutions"]
    resources = [local.rollout_state_machine_arn]
  }
  statement {
    sid       = "StopSupersededRollouts"
    actions   = ["states:DescribeExecution", "states:StopExecution"]
    resources = [local.rollout_executions_arn]
  }
  statement {
    sid       = "Notify"
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.notifications.arn]
  }
  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.rollout_fn.arn}:*"]
  }
}

resource "aws_iam_role_policy" "rollout" {
  name   = "patchgate-rollout"
  role   = aws_iam_role.rollout.id
  policy = data.aws_iam_policy_document.rollout.json
}

resource "aws_cloudwatch_log_group" "rollout_fn" {
  name              = "/aws/lambda/patchgate-rollout"
  retention_in_days = var.log_retention_days
}

resource "aws_lambda_function" "rollout" {
  function_name    = "patchgate-rollout"
  description      = "Plans, releases and evaluates rollouts of profile and app set changes."
  role             = aws_iam_role.rollout.arn
  runtime          = "python3.12"
  handler          = "handler.handler"
  filename         = data.archive_file.rollout.output_path
  source_code_hash = data.archive_file.rollout.output_base64sha256
  timeout          = 120
  memory_size      = 256
  layers           = [aws_lambda_layer_version.core.arn]

  environment {
    variables = {
      EXECUTION_ROLE_NAME         = var.execution_role_name
      PATCHGATE_CONFIG_TABLE      = aws_dynamodb_table.config.name
      PATCHGATE_HISTORY_TABLE     = aws_dynamodb_table.history.name
      PATCHGATE_RELEASES_TABLE    = aws_dynamodb_table.releases.name
      PATCHGATE_DEFAULT_APPROVERS = var.default_approver_group
      PATCHGATE_TOPIC_ARN         = aws_sns_topic.notifications.arn
    }
  }

  depends_on = [aws_cloudwatch_log_group.rollout_fn, aws_iam_role_policy.rollout]
}

# ------------------------------------------------------------------ approvals function + API

data "archive_file" "approvals" {
  type        = "zip"
  output_path = "${path.root}/.build/approvals.zip"
  source {
    content  = file("${local.repo_root}/providers/aws/functions/approvals/handler.py")
    filename = "handler.py"
  }
}

resource "aws_iam_role" "approvals" {
  name               = "patchgate-hub-approvals"
  assume_role_policy = data.aws_iam_policy_document.lambda_trust.json
}

data "aws_iam_policy_document" "approvals" {
  statement {
    sid       = "Requests"
    actions   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem"]
    resources = [aws_dynamodb_table.approvals.arn]
  }
  statement {
    sid       = "ReadChange"
    actions   = ["dynamodb:GetItem", "dynamodb:Query"]
    resources = [aws_dynamodb_table.config.arn, aws_dynamodb_table.history.arn, aws_dynamodb_table.releases.arn]
  }
  statement {
    sid       = "SigningKey"
    actions   = ["ssm:GetParameter", "ssm:PutParameter"]
    resources = ["arn:${local.partition}:ssm:${local.region}:${local.account_id}:parameter${local.signing_key_parameter}"]
  }
  statement {
    sid       = "SendApprovalEmail"
    actions   = ["ses:SendEmail"]
    resources = [aws_sesv2_email_identity.approvals.arn]
  }
  statement {
    sid       = "ResumeRollout"
    actions   = ["states:SendTaskSuccess"]
    resources = [local.rollout_state_machine_arn]
  }
  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.approvals.arn}:*"]
  }
}

resource "aws_iam_role_policy" "approvals" {
  name   = "patchgate-approvals"
  role   = aws_iam_role.approvals.id
  policy = data.aws_iam_policy_document.approvals.json
}

resource "aws_cloudwatch_log_group" "approvals" {
  name              = "/aws/lambda/patchgate-approvals"
  retention_in_days = var.log_retention_days
}

resource "aws_lambda_function" "approvals" {
  function_name    = "patchgate-approvals"
  description      = "Sends approval requests and serves the approval page."
  role             = aws_iam_role.approvals.arn
  runtime          = "python3.12"
  handler          = "handler.handler"
  filename         = data.archive_file.approvals.output_path
  source_code_hash = data.archive_file.approvals.output_base64sha256
  timeout          = 30
  memory_size      = 256
  layers           = [aws_lambda_layer_version.core.arn]

  environment {
    variables = {
      PATCHGATE_CONFIG_TABLE          = aws_dynamodb_table.config.name
      PATCHGATE_HISTORY_TABLE         = aws_dynamodb_table.history.name
      PATCHGATE_RELEASES_TABLE        = aws_dynamodb_table.releases.name
      PATCHGATE_APPROVALS_TABLE       = aws_dynamodb_table.approvals.name
      PATCHGATE_SIGNING_KEY_PARAMETER = local.signing_key_parameter
      PATCHGATE_APPROVAL_SENDER       = local.approval_sender
      PATCHGATE_APPROVAL_API_URL      = aws_apigatewayv2_api.approvals.api_endpoint
    }
  }

  depends_on = [aws_cloudwatch_log_group.approvals, aws_iam_role_policy.approvals]
}

# Public endpoint behind the approval links. Requests are authorised by the signed token in the
# link, not by the API, and throttled at the stage. Only GET/POST /approve exist.
resource "aws_apigatewayv2_api" "approvals" {
  name          = "patchgate-approvals"
  protocol_type = "HTTP"
  description   = "Patchgate approval links (signed, single-use, expiring tokens)."
}

resource "aws_apigatewayv2_integration" "approvals" {
  api_id                 = aws_apigatewayv2_api.approvals.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.approvals.invoke_arn
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "approve" {
  #checkov:skip=CKV_AWS_309:Approvers have no AWS identity; the signed, expiring token in the link authorises each request
  for_each  = toset(["GET /approve", "POST /approve"])
  api_id    = aws_apigatewayv2_api.approvals.id
  route_key = each.value
  target    = "integrations/${aws_apigatewayv2_integration.approvals.id}"
}

resource "aws_cloudwatch_log_group" "approvals_api" {
  name              = "/aws/apigateway/patchgate-approvals"
  retention_in_days = var.log_retention_days
}

resource "aws_apigatewayv2_stage" "approvals" {
  api_id      = aws_apigatewayv2_api.approvals.id
  name        = "$default"
  auto_deploy = true

  default_route_settings {
    throttling_burst_limit = 10
    throttling_rate_limit  = 2
  }

  # The query string (the token) is not logged.
  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.approvals_api.arn
    format = jsonencode({
      requestId = "$context.requestId"
      ip        = "$context.identity.sourceIp"
      time      = "$context.requestTime"
      route     = "$context.routeKey"
      status    = "$context.status"
    })
  }
}

resource "aws_lambda_permission" "approvals_api" {
  statement_id  = "AllowApprovalApi"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.approvals.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.approvals.execution_arn}/*/*/approve"
}

# ------------------------------------------------------------------ rollout workflow

resource "aws_iam_role" "rollout_workflow" {
  name = "patchgate-hub-rollout"
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

data "aws_iam_policy_document" "rollout_workflow" {
  statement {
    sid       = "InvokeSteps"
    actions   = ["lambda:InvokeFunction"]
    resources = [aws_lambda_function.rollout.arn, aws_lambda_function.approvals.arn]
  }
  statement {
    sid       = "RunBootstrap"
    actions   = ["states:StartExecution"]
    resources = [aws_sfn_state_machine.bootstrap.arn]
  }
  statement {
    sid       = "WaitForBootstrap"
    actions   = ["states:DescribeExecution", "states:StopExecution"]
    resources = ["arn:${local.partition}:states:${local.region}:${local.account_id}:execution:${aws_sfn_state_machine.bootstrap.name}:*"]
  }
  statement {
    sid       = "SyncIntegrationRule"
    actions   = ["events:PutTargets", "events:PutRule", "events:DescribeRule"]
    resources = ["arn:${local.partition}:events:${local.region}:${local.account_id}:rule/StepFunctionsGetEventsForStepFunctionsExecutionRule"]
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

resource "aws_iam_role_policy" "rollout_workflow" {
  name   = "patchgate-rollout-workflow"
  role   = aws_iam_role.rollout_workflow.id
  policy = data.aws_iam_policy_document.rollout_workflow.json
}

resource "aws_cloudwatch_log_group" "rollout_workflow" {
  name              = "/aws/vendedlogs/states/patchgate-rollout"
  retention_in_days = var.log_retention_days
}

resource "aws_sfn_state_machine" "rollout" {
  name     = "patchgate-rollout"
  role_arn = aws_iam_role.rollout_workflow.arn
  definition = templatefile("${local.repo_root}/providers/aws/workflows/rollout.asl.json", {
    rollout_arn   = aws_lambda_function.rollout.arn
    approvals_arn = aws_lambda_function.approvals.arn
    bootstrap_arn = aws_sfn_state_machine.bootstrap.arn
    region        = local.region
  })

  tracing_configuration {
    enabled = true
  }

  logging_configuration {
    log_destination        = "${aws_cloudwatch_log_group.rollout_workflow.arn}:*"
    include_execution_data = true
    level                  = "ERROR"
  }

  depends_on = [aws_iam_role_policy.rollout_workflow]
}
