variable "hub_account_id" {
  description = "Account ID of the Patchgate hub."
  type        = string

  validation {
    condition     = can(regex("^\\d{12}$", var.hub_account_id))
    error_message = "hub_account_id must be a 12-digit AWS account ID."
  }
}

variable "hub_event_bus_arn" {
  description = "ARN of the hub's central event bus."
  type        = string
}

variable "hub_role_names" {
  description = "Hub roles allowed to assume Patchgate-Execution."
  type        = list(string)
  default     = ["patchgate-hub-orchestrator", "patchgate-hub-resolver", "patchgate-hub-rollout-fn"]
}

variable "log_retention_days" {
  description = "Retention for Patchgate log groups."
  type        = number
  default     = 14
}
