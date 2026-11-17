output "execution_role_arn" {
  value = aws_iam_role.execution.arn
}

output "instance_profile_name" {
  value = aws_iam_instance_profile.instance.name
}

output "document_names" {
  value = [for d in aws_ssm_document.bootstrap : d.name]
}
