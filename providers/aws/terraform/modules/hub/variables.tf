variable "organization_id" {
  description = "AWS Organizations ID; only its accounts may send events or be acted on. null = single-account mode."
  type        = string
  default     = null

  validation {
    condition     = var.organization_id == null || can(regex("^o-[a-z0-9]{10,32}$", var.organization_id))
    error_message = "organization_id must look like o-xxxxxxxxxx."
  }
}

variable "execution_role_name" {
  description = "Name of the role the hub assumes in spoke accounts."
  type        = string
  default     = "Patchgate-Execution"
}

variable "notification_email" {
  description = "Email subscribed to Patchgate notifications (null to skip)."
  type        = string
  default     = null
}

variable "ssm_wait_attempts" {
  description = "How many 30s waits for the SSM agent to come online before failing."
  type        = number
  default     = 20
}

variable "log_retention_days" {
  description = "Retention for hub log groups."
  type        = number
  default     = 14
}
