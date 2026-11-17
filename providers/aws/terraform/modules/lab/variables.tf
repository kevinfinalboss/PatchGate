variable "instance_profile_name" {
  description = "Instance profile created by the spoke module."
  type        = string
  default     = "Patchgate-Instance"
}

variable "ttl_hours" {
  description = "Hours before the reaper terminates a lab instance."
  type        = number
  default     = 2
}

variable "enable_linux" {
  type    = bool
  default = true
}

variable "enable_windows" {
  type    = bool
  default = true
}

variable "linux_instance_type" {
  type    = string
  default = "t3.micro"
}

variable "windows_instance_type" {
  type    = string
  default = "t3.medium"
}

variable "linux_profile" {
  description = "Catalog profile for the Linux instance."
  type        = string
  default     = "base-linux"
}

variable "windows_profile" {
  description = "Catalog profile for the Windows instance."
  type        = string
  default     = "web-windows"
}
