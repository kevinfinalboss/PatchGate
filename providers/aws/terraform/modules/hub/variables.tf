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

variable "protect_config" {
  description = "Enable deletion protection on the configuration and history tables."
  type        = bool
  default     = true
}

variable "default_approver_group" {
  description = "Approver group for instances in accounts not registered with patchgate-cli account set (ADR 0010)."
  type        = string
  default     = "platform"

  validation {
    condition     = can(regex("^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$", var.default_approver_group))
    error_message = "Must be an approver group name (lowercase letters, digits and dashes)."
  }
}

variable "approval_sender_email" {
  description = "Verified SES identity that sends approval emails (defaults to notification_email). In the SES sandbox approvers must be verified identities too."
  type        = string
  default     = null
}
