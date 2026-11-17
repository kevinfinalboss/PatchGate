# One workspace per spoke account: terraform workspace select dev|prod
variable "hub_account_id" {
  type = string
}

variable "hub_event_bus_arn" {
  type = string
}

module "spoke" {
  source            = "../../modules/spoke"
  hub_account_id    = var.hub_account_id
  hub_event_bus_arn = var.hub_event_bus_arn
}

output "instance_profile_name" {
  value = module.spoke.instance_profile_name
}
