# The organization itself is pre-existing and intentionally NOT managed here:
# importing aws_organizations_organization would let Terraform rewrite enabled
# policy types and trusted services owned by the AWS setup experience.
data "aws_organizations_organization" "this" {}

data "aws_caller_identity" "current" {}

locals {
  root_id = data.aws_organizations_organization.this.roots[0].id

  ou_ids = {
    security  = aws_organizations_organizational_unit.security.id
    workloads = aws_organizations_organizational_unit.workloads.id
  }
}

resource "aws_organizations_organizational_unit" "security" {
  name      = "Security"
  parent_id = local.root_id
}

resource "aws_organizations_organizational_unit" "workloads" {
  name      = "Workloads"
  parent_id = local.root_id
}

resource "aws_organizations_account" "this" {
  for_each = var.accounts

  name                       = each.value.name
  email                      = each.value.email
  parent_id                  = local.ou_ids[each.value.ou]
  role_name                  = "OrganizationAccountAccessRole"
  iam_user_access_to_billing = "DENY"

  # Closing accounts is slow and rate-limited; never do it from a terraform destroy.
  close_on_deletion = false

  lifecycle {
    prevent_destroy = true
    ignore_changes  = [role_name, iam_user_access_to_billing]
  }
}
