# Configuration store (ADR 0008): catalog documents in DynamoDB, managed with patchgate-cli,
# the console or IaC. Every change is recorded in the history table by the audit function.

locals {
  seed = jsondecode(file("${local.repo_root}/providers/aws/config/seed.json"))
}

resource "aws_dynamodb_table" "config" {
  name                        = "patchgate-config"
  billing_mode                = "PAY_PER_REQUEST"
  hash_key                    = "kind"
  range_key                   = "name"
  deletion_protection_enabled = var.protect_config
  stream_enabled              = true
  stream_view_type            = "NEW_AND_OLD_IMAGES"

  attribute {
    name = "kind"
    type = "S"
  }
  attribute {
    name = "name"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled = true
  }
}

resource "aws_dynamodb_table" "history" {
  name                        = "patchgate-config-history"
  billing_mode                = "PAY_PER_REQUEST"
  hash_key                    = "id"
  range_key                   = "changeId"
  deletion_protection_enabled = var.protect_config

  attribute {
    name = "id"
    type = "S"
  }
  attribute {
    name = "changeId"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled = true
  }
}

# Built-in content shipped with Patchgate. Owned by releases: re-applied on every deploy.
# Customise by copying to a new name (managedBy = user items are never touched by Terraform).
resource "aws_dynamodb_table_item" "seed" {
  for_each   = { for doc in local.seed : "${doc.kind}#${doc.name}" => doc }
  table_name = aws_dynamodb_table.config.name
  hash_key   = aws_dynamodb_table.config.hash_key
  range_key  = aws_dynamodb_table.config.range_key

  item = jsonencode({
    kind      = { S = each.value.kind }
    name      = { S = each.value.name }
    document  = { S = each.value.document }
    version   = { N = "1" }
    managedBy = { S = "patchgate" }
    updatedBy = { S = "terraform (built-in catalog)" }
    updatedAt = { S = "release" }
  })
}

# ------------------------------------------------------------------ shared layer

data "archive_file" "layer" {
  type        = "zip"
  source_dir  = "${local.repo_root}/providers/aws/functions/layer/.build"
  output_path = "${path.root}/.build/patchgate-core-layer.zip"
}

resource "aws_lambda_layer_version" "core" {
  layer_name               = "patchgate-core"
  description              = "Patchgate plan library, JSON Schemas and pinned dependencies."
  filename                 = data.archive_file.layer.output_path
  source_code_hash         = data.archive_file.layer.output_base64sha256
  compatible_runtimes      = ["python3.12"]
  compatible_architectures = ["x86_64"]
}
# Revision of each gated document (profiles, app sets) released to each ring and approver group
# (ADR 0010). Written by patchgate-cli, the audit function (safety net) and rollouts.
resource "aws_dynamodb_table" "releases" {
  name                        = "patchgate-releases"
  billing_mode                = "PAY_PER_REQUEST"
  hash_key                    = "id"
  deletion_protection_enabled = var.protect_config

  attribute {
    name = "id"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled = true
  }
}

# ------------------------------------------------------------------ audit trail

data "archive_file" "audit" {
  type        = "zip"
  output_path = "${path.root}/.build/config-audit.zip"
  source {
    content  = file("${local.repo_root}/providers/aws/functions/config_audit/handler.py")
    filename = "handler.py"
  }
}

resource "aws_iam_role" "audit" {
  name               = "patchgate-hub-config-audit"
  assume_role_policy = data.aws_iam_policy_document.lambda_trust.json
}

data "aws_iam_policy_document" "audit" {
  statement {
    sid       = "ReadConfigStream"
    actions   = ["dynamodb:DescribeStream", "dynamodb:GetRecords", "dynamodb:GetShardIterator", "dynamodb:ListStreams"]
    resources = [aws_dynamodb_table.config.stream_arn]
  }
  statement {
    sid       = "WriteHistory"
    actions   = ["dynamodb:PutItem"]
    resources = [aws_dynamodb_table.history.arn]
  }
  statement {
    sid       = "ReadRolloutPolicy"
    actions   = ["dynamodb:GetItem"]
    resources = [aws_dynamodb_table.config.arn]
  }
  statement {
    sid       = "PinPreviousRevision"
    actions   = ["dynamodb:PutItem"]
    resources = [aws_dynamodb_table.releases.arn]
  }
  statement {
    sid       = "Alert"
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.notifications.arn]
  }
  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.audit.arn}:*"]
  }
}

resource "aws_iam_role_policy" "audit" {
  name   = "patchgate-config-audit"
  role   = aws_iam_role.audit.id
  policy = data.aws_iam_policy_document.audit.json
}

resource "aws_cloudwatch_log_group" "audit" {
  name              = "/aws/lambda/patchgate-config-audit"
  retention_in_days = var.log_retention_days
}

resource "aws_lambda_function" "audit" {
  function_name    = "patchgate-config-audit"
  description      = "Records configuration changes and alerts on invalid documents."
  role             = aws_iam_role.audit.arn
  runtime          = "python3.12"
  handler          = "handler.handler"
  filename         = data.archive_file.audit.output_path
  source_code_hash = data.archive_file.audit.output_base64sha256
  layers           = [aws_lambda_layer_version.core.arn]
  timeout          = 30

  environment {
    variables = {
      PATCHGATE_HISTORY_TABLE  = aws_dynamodb_table.history.name
      PATCHGATE_ALERT_TOPIC    = aws_sns_topic.notifications.arn
      PATCHGATE_CONFIG_TABLE   = aws_dynamodb_table.config.name
      PATCHGATE_RELEASES_TABLE = aws_dynamodb_table.releases.name
    }
  }

  depends_on = [aws_cloudwatch_log_group.audit, aws_iam_role_policy.audit]
}

resource "aws_lambda_event_source_mapping" "audit" {
  event_source_arn                   = aws_dynamodb_table.config.stream_arn
  function_name                      = aws_lambda_function.audit.arn
  starting_position                  = "TRIM_HORIZON"
  batch_size                         = 25
  maximum_retry_attempts             = 3
  bisect_batch_on_function_error     = true
  maximum_batching_window_in_seconds = 5
}

# One Patchgate run per instance at a time (launch and tag-change events arrive together).
resource "aws_dynamodb_table" "locks" {
  name         = "patchgate-locks"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "lockId"

  attribute {
    name = "lockId"
    type = "S"
  }

  ttl {
    attribute_name = "expiresAt"
    enabled        = true
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled = true
  }
}
