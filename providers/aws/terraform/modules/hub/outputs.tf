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
