output "organization_id" {
  value = data.aws_organizations_organization.this.id
}

output "ou_ids" {
  value = local.ou_ids
}

output "account_ids" {
  description = "Member account IDs keyed by the identifiers in var.accounts."
  value       = { for k, a in aws_organizations_account.this : k => a.id }
}

output "cloudtrail_arn" {
  value = aws_cloudtrail.org.arn
}
