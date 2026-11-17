# Lab: disposable test environment. Created at the start of a session, destroyed at the end.
# - Public subnet with no inbound rules (no NAT Gateway cost); IMDSv2 required
# - One Linux and one Windows instance tagged for Patchgate bootstrap
# - TTL reaper that terminates lab instances older than their Patchgate:TTL

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

data "aws_availability_zones" "available" {
  state = "available"
}

locals {
  repo_root = "${path.module}/../../../../.."
  lab_tags = {
    "Patchgate:Lab" = "true"
    "Patchgate:TTL" = tostring(var.ttl_hours)
  }
}

# ------------------------------------------------------------------ network

resource "aws_vpc" "lab" {
  cidr_block           = "10.70.0.0/16"
  enable_dns_hostnames = true
  enable_dns_support   = true
  tags                 = { Name = "patchgate-lab" }
}

resource "aws_default_security_group" "lab" {
  vpc_id = aws_vpc.lab.id
  # No rules: the default security group denies everything.
}

resource "aws_internet_gateway" "lab" {
  vpc_id = aws_vpc.lab.id
  tags   = { Name = "patchgate-lab" }
}

resource "aws_subnet" "public" {
  vpc_id            = aws_vpc.lab.id
  cidr_block        = "10.70.1.0/24"
  availability_zone = data.aws_availability_zones.available.names[0]
  tags              = { Name = "patchgate-lab-public" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.lab.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.lab.id
  }
  tags = { Name = "patchgate-lab-public" }
}

resource "aws_route_table_association" "public" {
  subnet_id      = aws_subnet.public.id
  route_table_id = aws_route_table.public.id
}

resource "aws_security_group" "instances" {
  name        = "patchgate-lab-instances"
  description = "Lab instances: no inbound access, outbound HTTPS/HTTP for SSM, packages and agents."
  vpc_id      = aws_vpc.lab.id

  egress {
    description = "HTTPS (SSM, package repositories, agent downloads)"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    description = "HTTP (some distribution mirrors)"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# ------------------------------------------------------------------ instances

data "aws_ssm_parameter" "ami" {
  for_each = {
    linux   = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
    windows = "/aws/service/ami-windows-latest/Windows_Server-2022-English-Full-Base"
  }
  name = each.value
}

locals {
  instances = { for k, v in {
    linux = {
      enabled = var.enable_linux
      type    = var.linux_instance_type
      profile = var.linux_profile
      disk_gb = 8
    }
    windows = {
      enabled = var.enable_windows
      type    = var.windows_instance_type
      profile = var.windows_profile
      disk_gb = 30
    }
  } : k => v if v.enabled }
}

resource "aws_instance" "lab" {
  for_each = local.instances

  ami                         = data.aws_ssm_parameter.ami[each.key].value
  instance_type               = each.value.type
  subnet_id                   = aws_subnet.public.id
  vpc_security_group_ids      = [aws_security_group.instances.id]
  iam_instance_profile        = var.instance_profile_name
  associate_public_ip_address = true
  ebs_optimized               = true
  monitoring                  = false

  metadata_options {
    http_endpoint = "enabled"
    http_tokens   = "required"
  }

  root_block_device {
    volume_type           = "gp3"
    volume_size           = each.value.disk_gb
    encrypted             = true
    delete_on_termination = true
  }

  tags = merge(local.lab_tags, {
    Name                = "patchgate-lab-${each.key}"
    "Patchgate:Managed" = "true"
    "Patchgate:Profile" = each.value.profile
    "Patchgate:Env"     = "lab"
  })

  volume_tags = merge(local.lab_tags, { Name = "patchgate-lab-${each.key}" })

  lifecycle {
    ignore_changes = [ami]
  }
}

# ------------------------------------------------------------------ TTL reaper

data "archive_file" "reaper" {
  type        = "zip"
  output_path = "${path.root}/.build/reaper.zip"
  source {
    content  = file("${local.repo_root}/providers/aws/functions/reaper/handler.py")
    filename = "handler.py"
  }
}

resource "aws_iam_role" "reaper" {
  name = "patchgate-lab-reaper"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "lambda.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy" "reaper" {
  name = "terminate-expired-lab-instances"
  role = aws_iam_role.reaper.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Sid = "Describe", Effect = "Allow", Action = "ec2:DescribeInstances", Resource = "*" },
      {
        Sid       = "TerminateLabOnly"
        Effect    = "Allow"
        Action    = "ec2:TerminateInstances"
        Resource  = "*"
        Condition = { StringEquals = { "aws:ResourceTag/Patchgate:Lab" = "true" } }
      },
      {
        Sid      = "Logs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.reaper.arn}:*"
      },
    ]
  })
}

resource "aws_cloudwatch_log_group" "reaper" {
  name              = "/aws/lambda/patchgate-lab-reaper"
  retention_in_days = 7
}

resource "aws_lambda_function" "reaper" {
  function_name    = "patchgate-lab-reaper"
  description      = "Terminates Patchgate lab instances older than their TTL."
  role             = aws_iam_role.reaper.arn
  runtime          = "python3.12"
  handler          = "handler.handler"
  filename         = data.archive_file.reaper.output_path
  source_code_hash = data.archive_file.reaper.output_base64sha256
  timeout          = 60

  environment {
    variables = { DEFAULT_TTL_HOURS = tostring(var.ttl_hours) }
  }

  depends_on = [aws_cloudwatch_log_group.reaper, aws_iam_role_policy.reaper]
}

resource "aws_cloudwatch_event_rule" "reaper" {
  name                = "patchgate-lab-reaper"
  description         = "Run the Patchgate lab TTL reaper every 15 minutes."
  schedule_expression = "rate(15 minutes)"
}

resource "aws_cloudwatch_event_target" "reaper" {
  rule = aws_cloudwatch_event_rule.reaper.name
  arn  = aws_lambda_function.reaper.arn
}

resource "aws_lambda_permission" "reaper" {
  statement_id  = "AllowEventBridgeSchedule"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.reaper.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.reaper.arn
}
