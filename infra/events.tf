# ---------- Fraud pipeline: DynamoDB Stream -> Lambda -> SNS ----------

# Encrypted with the AWS-managed SNS key (fine for same-account Lambda publishers).
resource "aws_sns_topic" "fraud_alerts" {
  name              = "${local.name}-fraud-alerts"
  kms_master_key_id = "alias/aws/sns"
}

# Not encrypted on purpose: CloudWatch alarms cannot publish to a topic that uses the
# AWS-managed key. Encrypting this needs a customer-managed KMS key (see README).
resource "aws_sns_topic" "ops_alerts" {
  name = "${local.name}-ops-alerts"
}

resource "aws_sns_topic_subscription" "fraud_email" {
  count     = var.alert_email == "" ? 0 : 1
  topic_arn = aws_sns_topic.fraud_alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

resource "aws_sns_topic_subscription" "ops_email" {
  count     = var.alert_email == "" ? 0 : 1
  topic_arn = aws_sns_topic.ops_alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

# Stream records that still fail after retries land here instead of being lost.
resource "aws_sqs_queue" "fraud_dlq" {
  name                      = "${local.name}-fraud-dlq"
  message_retention_seconds = 1209600 # 14 days
  sqs_managed_sse_enabled   = true
}

resource "aws_lambda_event_source_mapping" "fraud" {
  event_source_arn                   = aws_dynamodb_table.transactions.stream_arn
  function_name                      = aws_lambda_function.fn["fraud"].arn
  starting_position                  = "LATEST"
  batch_size                         = 10
  maximum_batching_window_in_seconds = 1
  maximum_retry_attempts             = 3
  bisect_batch_on_function_error     = true
  function_response_types            = ["ReportBatchItemFailures"]

  # Only invoke the Lambda for new DEBIT entries; filtered events cost nothing.
  filter_criteria {
    filter {
      pattern = jsonencode({
        eventName = ["INSERT"]
        dynamodb = {
          NewImage = {
            direction = { S = ["DEBIT"] }
          }
        }
      })
    }
  }

  destination_config {
    on_failure {
      destination_arn = aws_sqs_queue.fraud_dlq.arn
    }
  }

  depends_on = [aws_iam_role_policy.fraud]
}
