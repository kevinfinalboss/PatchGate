variable "enable_windows" {
  type    = bool
  default = true
}

variable "ttl_hours" {
  type    = number
  default = 2
}

module "lab" {
  source         = "../../modules/lab"
  enable_windows = var.enable_windows
  ttl_hours      = var.ttl_hours
}

output "instance_ids" {
  value = module.lab.instance_ids
}
