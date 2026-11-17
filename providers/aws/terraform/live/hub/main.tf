variable "organization_id" {
  type = string
}

variable "spoke_account_ids" {
  type = list(string)
}

variable "notification_email" {
  type    = string
  default = null
}

module "hub" {
  source             = "../../modules/hub"
  organization_id    = var.organization_id
  spoke_account_ids  = var.spoke_account_ids
  notification_email = var.notification_email
}

output "event_bus_arn" {
  value = module.hub.event_bus_arn
}

output "hub_account_id" {
  value = module.hub.account_id
}

output "state_machine_arn" {
  value = module.hub.state_machine_arn
}
