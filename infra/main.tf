locals {
  name    = "${var.project}-${var.environment}"
  is_prod = var.environment == "prod"
}

data "aws_region" "current" {}
