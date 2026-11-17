# Cost guardrails for the whole organization (consolidated billing in the management account).

resource "aws_budgets_budget" "monthly" {
  name         = "patchgate-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.budget_limit_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 40
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.alert_email]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 80
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.alert_email]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.alert_email]
  }
}

# Kill switch: when actual monthly spend reaches the limit, attach an SCP to the
# Workloads OU that blocks new compute. Detach the policy manually to recover.
resource "aws_budgets_budget" "kill_switch" {
  name         = "patchgate-kill-switch"
  budget_type  = "COST"
  limit_amount = tostring(var.kill_switch_limit_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"
}

resource "aws_organizations_policy" "deny_new_workloads" {
  name        = "patchgate-deny-new-workloads"
  description = "Attached by the Patchgate budget kill switch. Blocks new compute in workload accounts."
  type        = "SERVICE_CONTROL_POLICY"

  content = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid    = "DenyNewCompute"
      Effect = "Deny"
      Action = [
        "ec2:RunInstances",
        "ec2:StartInstances",
        "ec2:CreateNatGateway",
        "ec2:AllocateAddress",
        "imagebuilder:StartImagePipelineExecution",
      ]
      Resource = "*"
    }]
  })
}

data "aws_iam_policy_document" "budget_action_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["budgets.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}

resource "aws_iam_role" "budget_action" {
  name               = "patchgate-budget-action"
  assume_role_policy = data.aws_iam_policy_document.budget_action_trust.json
}

data "aws_iam_policy_document" "budget_action" {
  statement {
    sid     = "AttachKillSwitchScpToWorkloads"
    actions = ["organizations:AttachPolicy", "organizations:DetachPolicy"]
    resources = [
      aws_organizations_policy.deny_new_workloads.arn,
      aws_organizations_organizational_unit.workloads.arn,
    ]
  }
  statement {
    sid       = "DescribeKillSwitchScp"
    actions   = ["organizations:DescribePolicy", "organizations:ListTargetsForPolicy"]
    resources = [aws_organizations_policy.deny_new_workloads.arn]
  }
}

resource "aws_iam_role_policy" "budget_action" {
  name   = "attach-kill-switch-scp"
  role   = aws_iam_role.budget_action.id
  policy = data.aws_iam_policy_document.budget_action.json
}

resource "aws_budgets_budget_action" "kill_switch" {
  budget_name        = aws_budgets_budget.kill_switch.name
  action_type        = "APPLY_SCP_POLICY"
  approval_model     = "AUTOMATIC"
  notification_type  = "ACTUAL"
  execution_role_arn = aws_iam_role.budget_action.arn

  action_threshold {
    action_threshold_type  = "PERCENTAGE"
    action_threshold_value = 100
  }

  definition {
    scp_action_definition {
      policy_id  = aws_organizations_policy.deny_new_workloads.id
      target_ids = [aws_organizations_organizational_unit.workloads.id]
    }
  }

  subscriber {
    address           = var.alert_email
    subscription_type = "EMAIL"
  }
}
