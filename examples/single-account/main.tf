# Patchgate in a single AWS account: the hub and the spoke live side by side.
# No AWS Organizations required.
#
#   terraform init && terraform apply -var notification_email=you@example.com
#
# Then launch an instance with the Patchgate-Instance profile and the tag
# Patchgate:Managed=true (optionally Patchgate:Profile=<profile>).

terraform {
  required_version = ">= 1.10"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.6"
    }
  }
}

provider "aws" {
  region  = var.region
  profile = var.aws_profile

  default_tags {
    tags = {
      Project   = "Patchgate"
      ManagedBy = "Terraform"
      Stack     = "single-account"
    }
  }
}

variable "region" {
  type    = string
  default = "us-east-2"
}

variable "aws_profile" {
  type    = string
  default = null
}

variable "notification_email" {
  type    = string
  default = null
}

module "hub" {
  source             = "../../providers/aws/terraform/modules/hub"
  notification_email = var.notification_email
}

module "spoke" {
  source            = "../../providers/aws/terraform/modules/spoke"
  hub_account_id    = module.hub.account_id
  hub_event_bus_arn = module.hub.event_bus_arn

  depends_on = [module.hub]
}

output "instance_profile_name" {
  value = module.spoke.instance_profile_name
}

output "state_machine_arn" {
  value = module.hub.state_machine_arn
}
