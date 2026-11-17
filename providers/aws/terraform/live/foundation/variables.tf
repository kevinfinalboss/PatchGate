variable "region" {
  description = "Home region for Patchgate."
  type        = string
  default     = "us-east-1"
}

variable "accounts" {
  description = "Member accounts to create. Keys are stable identifiers; emails must be unique per AWS account."
  type = map(object({
    name  = string
    email = string
    ou    = string
  }))

  validation {
    condition     = alltrue([for a in values(var.accounts) : contains(["security", "workloads"], a.ou)])
    error_message = "Each account ou must be \"security\" or \"workloads\"."
  }
}

variable "alert_email" {
  description = "Email that receives budget alerts and kill-switch notifications."
  type        = string
}

variable "budget_limit_usd" {
  description = "Monthly budget used for early warning alerts."
  type        = number
  default     = 15
}

variable "kill_switch_limit_usd" {
  description = "Monthly actual spend at which new workloads are blocked in the Workloads OU."
  type        = number
  default     = 25
}

variable "cloudtrail_retention_days" {
  description = "Days to keep organization CloudTrail logs."
  type        = number
  default     = 90
}
