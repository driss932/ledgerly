# One-time setup, run from your laptop with your own AWS credentials:
#   - S3 bucket for Terraform state (versioned, encrypted, private)
#   - GitHub OIDC provider + deploy role, so GitHub Actions never needs AWS access keys
#
# This uses local state on purpose (it creates the remote state bucket).
# Do not commit terraform.tfstate from this folder.

terraform {
  required_version = ">= 1.10"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.80"
    }
  }
}

variable "region" {
  type    = string
  default = "eu-west-2"
}

variable "github_owner" {
  description = "Your GitHub username"
  type        = string
}

variable "github_repo" {
  description = "Repository name"
  type        = string
  default     = "ledgerly"
}

variable "github_owner_id" {
  description = "Numeric GitHub user ID"
  type        = string
}

variable "github_repo_id" {
  description = "Numeric GitHub repository ID"
  type        = string
}

# GitHub's OIDC subject for new repos (since July 2026) includes immutable IDs.
locals {
  github_subject = "repo:${var.github_owner}@${var.github_owner_id}/${var.github_repo}@${var.github_repo_id}"
}

provider "aws" {
  region = var.region
}

data "aws_caller_identity" "current" {}

# ---------- Terraform state bucket ----------

resource "aws_s3_bucket" "state" {
  bucket = "ledgerly-tfstate-${data.aws_caller_identity.current.account_id}"
}

resource "aws_s3_bucket_versioning" "state" {
  bucket = aws_s3_bucket.state.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "state" {
  bucket = aws_s3_bucket.state.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "state" {
  bucket                  = aws_s3_bucket.state.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_policy" "state_tls_only" {
  bucket = aws_s3_bucket.state.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyInsecureTransport"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource  = [aws_s3_bucket.state.arn, "${aws_s3_bucket.state.arn}/*"]
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })
}

# ---------- GitHub Actions OIDC ----------

resource "aws_iam_openid_connect_provider" "github" {
  url             = "https://token.actions.githubusercontent.com"
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = ["6938fd4d98bab03faadb97b34396831e3780aea1"]
}

resource "aws_iam_role" "github_deploy" {
  name = "ledgerly-github-deploy"

  # Only workflows in YOUR repo, running in the dev/prod GitHub environments, can assume this.
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = aws_iam_openid_connect_provider.github.arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
        }
        StringLike = {
          "token.actions.githubusercontent.com:sub" = [
            "${local.github_subject}:environment:dev",
            "${local.github_subject}:environment:prod",
          ]
        }
      }
    }]
  })
}

# Trade-off: Terraform needs broad permissions to create IAM roles, Lambdas, tables, etc.
# The trust policy above is what keeps this safe. Next step: replace with a scoped policy
# plus an IAM permissions boundary (listed in the README under "What I'd improve").
resource "aws_iam_role_policy_attachment" "github_deploy" {
  role       = aws_iam_role.github_deploy.name
  policy_arn = "arn:aws:iam::aws:policy/AdministratorAccess"
}

output "deploy_role_arn" {
  value = aws_iam_role.github_deploy.arn
}

output "state_bucket" {
  value = aws_s3_bucket.state.bucket
}
