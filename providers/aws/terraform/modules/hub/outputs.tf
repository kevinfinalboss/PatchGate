output "event_bus_arn" {
  description = "Pass to the spoke module as hub_event_bus_arn."
  value       = aws_cloudwatch_event_bus.hub.arn
}

output "account_id" {
  value = local.account_id
}

output "state_machine_arn" {
  value = aws_sfn_state_machine.bootstrap.arn
}

output "notifications_topic_arn" {
  value = aws_sns_topic.notifications.arn
}

output "config_table" {
  description = "Configuration table used by patchgate-cli."
  value       = aws_dynamodb_table.config.name
}

output "history_table" {
  value = aws_dynamodb_table.history.name
}

output "rollout_state_machine_arn" {
  value = aws_sfn_state_machine.rollout.arn
}

output "approval_api_url" {
  description = "Base URL of the approval links."
  value       = aws_apigatewayv2_api.approvals.api_endpoint
}
