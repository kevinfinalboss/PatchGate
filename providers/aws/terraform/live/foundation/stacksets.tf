locals {
  spoke_template = "${path.module}/../../../stacksets/spoke.template.json"
}

# Private, versioned bucket for deployment artifacts read by CloudFormation StackSets.
resource "aws_s3_bucket" "artifacts" {
  bucket = "patchgate-artifacts-${local.account_id}"
}

resource "aws_s3_bucket_ownership_controls" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_public_access_block" "artifacts" {
  bucket                  = aws_s3_bucket.artifacts.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  rule {
    id     = "expire-old-versions"
    status = "Enabled"
    filter {}
    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
    noncurrent_version_expiration {
      noncurrent_days = 30
    }
  }
}

data "aws_iam_policy_document" "artifacts" {
  statement {
    sid       = "DenyInsecureTransport"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.artifacts.arn, "${aws_s3_bucket.artifacts.arn}/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "artifacts" {
  bucket     = aws_s3_bucket.artifacts.id
  policy     = data.aws_iam_policy_document.artifacts.json
  depends_on = [aws_s3_bucket_public_access_block.artifacts]
}

resource "aws_s3_object" "spoke_template" {
  bucket       = aws_s3_bucket.artifacts.id
  key          = "stacksets/patchgate-spoke.template.json"
  source       = local.spoke_template
  etag         = filemd5(local.spoke_template)
  content_type = "application/json"

  depends_on = [aws_s3_bucket_versioning.artifacts]
}

# Spoke for every account in the Workloads OU, deployed by a service-managed StackSet.
# Accounts created in (or moved into) the OU are onboarded automatically; accounts that
# leave the OU have the spoke removed.

resource "aws_cloudformation_stack_set" "spoke" {
  name             = "patchgate-spoke"
  description      = "Patchgate spoke: execution role, event forwarding, SSM documents, instance profile."
  permission_model = "SERVICE_MANAGED"
  capabilities     = ["CAPABILITY_NAMED_IAM"]
  # Uploaded to S3: the template embeds the in-VM installers and exceeds the 51 KB inline limit.
  # Pinning the object version makes every template change a StackSet update.
  template_url = "https://${aws_s3_bucket.artifacts.bucket_regional_domain_name}/${aws_s3_object.spoke_template.key}?versionId=${aws_s3_object.spoke_template.version_id}"

  parameters = {
    HubAccountId   = var.hub_account_id
    HubEventBusArn = var.hub_event_bus_arn
  }

  auto_deployment {
    enabled                          = true
    retain_stacks_on_account_removal = false
  }

  managed_execution {
    active = true
  }

  operation_preferences {
    failure_tolerance_count = 0
    max_concurrent_count    = 2
    region_concurrency_type = "PARALLEL"
  }

  lifecycle {
    ignore_changes = [administration_role_arn]
  }
}

resource "aws_cloudformation_stack_set_instance" "workloads" {
  stack_set_name            = aws_cloudformation_stack_set.spoke.name
  stack_set_instance_region = var.workload_region

  deployment_targets {
    organizational_unit_ids = [aws_organizations_organizational_unit.workloads.id]
  }

  retain_stack = false
}
