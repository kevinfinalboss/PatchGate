# Spoke for every account in the Workloads OU, deployed by a service-managed StackSet.
# Accounts created in (or moved into) the OU are onboarded automatically; accounts that
# leave the OU have the spoke removed.

resource "aws_cloudformation_stack_set" "spoke" {
  name             = "patchgate-spoke"
  description      = "Patchgate spoke: execution role, event forwarding, SSM documents, instance profile."
  permission_model = "SERVICE_MANAGED"
  capabilities     = ["CAPABILITY_NAMED_IAM"]
  template_body    = file("${path.module}/../../../stacksets/spoke.template.json")

  parameters = {
    HubAccountId   = var.hub_account_id
    HubEventBusArn = var.hub_event_bus_arn
  }

  auto_deployment {
    enabled                          = true
    retain_stacks_on_account_removal = false
  }

  managed_execution {
    active = true
  }

  operation_preferences {
    failure_tolerance_count = 0
    max_concurrent_count    = 2
    region_concurrency_type = "PARALLEL"
  }

  lifecycle {
    ignore_changes = [administration_role_arn]
  }
}

resource "aws_cloudformation_stack_set_instance" "workloads" {
  stack_set_name            = aws_cloudformation_stack_set.spoke.name
  stack_set_instance_region = var.workload_region

  deployment_targets {
    organizational_unit_ids = [aws_organizations_organizational_unit.workloads.id]
  }

  retain_stack = false
}
