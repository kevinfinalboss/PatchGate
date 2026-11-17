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

  # Partial configuration: terraform init -backend-config=backend.hcl
  backend "s3" {}
}

provider "aws" {
  region  = var.region
  profile = var.aws_profile

  default_tags {
    tags = {
      Project   = "Patchgate"
      ManagedBy = "Terraform"
      Stack     = "spoke"
    }
  }
}

variable "region" {
  description = "Patchgate workload region."
  type        = string
  default     = "us-east-2"
}

variable "aws_profile" {
  description = "AWS CLI profile for the target account (null = default credential chain, e.g. CI)."
  type        = string
  default     = null
}
