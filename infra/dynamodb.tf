# All tables: on-demand billing (pay per request, ~free at demo scale),
# encryption at rest, point-in-time recovery, deletion protection in prod.

resource "aws_dynamodb_table" "accounts" {
  name                        = "${local.name}-accounts"
  billing_mode                = "PAY_PER_REQUEST"
  hash_key                    = "account_id"
  deletion_protection_enabled = local.is_prod

  attribute {
    name = "account_id"
    type = "S"
  }

  attribute {
    name = "owner_id"
    type = "S"
  }

  # "List my accounts" queries by owner.
  global_secondary_index {
    name            = "owner_id-index"
    hash_key        = "owner_id"
    projection_type = "ALL"
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled = true # AWS-managed KMS key (aws/dynamodb)
  }
}

# Immutable ledger: one row per side of every money movement.
resource "aws_dynamodb_table" "transactions" {
  name                        = "${local.name}-transactions"
  billing_mode                = "PAY_PER_REQUEST"
  hash_key                    = "account_id"
  range_key                   = "sk" # "<timestamp>#<transfer_id>"
  deletion_protection_enabled = local.is_prod

  # Every committed ledger write is streamed to the fraud checker.
  stream_enabled   = true
  stream_view_type = "NEW_IMAGE"

  attribute {
    name = "account_id"
    type = "S"
  }

  attribute {
    name = "sk"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled = true
  }
}

# Stores the response of each transfer per Idempotency-Key; expires after 24h via TTL.
resource "aws_dynamodb_table" "idempotency" {
  name         = "${local.name}-idempotency"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "idempotency_key"

  attribute {
    name = "idempotency_key"
    type = "S"
  }

  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }

  server_side_encryption {
    enabled = true
  }
}
