variable "enable_linux" {
  type    = bool
  default = true
}

variable "enable_windows" {
  type    = bool
  default = true
}

variable "ttl_hours" {
  type    = number
  default = 2
}

variable "linux_profile" {
  type    = string
  default = "hardened-linux"
}

variable "linux_instance_type" {
  type    = string
  default = "t3.micro"
}

variable "windows_profile" {
  type    = string
  default = "hardened-windows"
}

variable "linux_appsets" {
  type    = string
  default = ""
}

variable "windows_appsets" {
  type    = string
  default = ""
}

module "lab" {
  source              = "../../modules/lab"
  enable_linux        = var.enable_linux
  enable_windows      = var.enable_windows
  ttl_hours           = var.ttl_hours
  linux_profile       = var.linux_profile
  linux_instance_type = var.linux_instance_type
  windows_profile     = var.windows_profile
  linux_appsets       = var.linux_appsets
  windows_appsets     = var.windows_appsets
}

output "instance_ids" {
  value = module.lab.instance_ids
}
