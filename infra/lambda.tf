data "archive_file" "lambda" {
  type        = "zip"
  source_dir  = "${path.module}/../src"
  output_path = "${path.module}/build/lambda.zip"
}

locals {
  functions = {
    accounts  = { handler = "app.accounts.handler", timeout = 10 }
    transfers = { handler = "app.transfers.handler", timeout = 10 }
    fraud     = { handler = "app.fraud.handler", timeout = 30 }
  }

  lambda_env = {
    ACCOUNTS_TABLE           = aws_dynamodb_table.accounts.name
    TRANSACTIONS_TABLE       = aws_dynamodb_table.transactions.name
    IDEMPOTENCY_TABLE        = aws_dynamodb_table.idempotency.name
    FRAUD_TOPIC_ARN          = aws_sns_topic.fraud_alerts.arn
    LARGE_TRANSFER_THRESHOLD = tostring(var.large_transfer_threshold)
    VELOCITY_LIMIT           = tostring(var.velocity_limit)
    ENVIRONMENT              = var.environment
    LOG_LEVEL                = "INFO"
  }
}

resource "aws_cloudwatch_log_group" "lambda" {
  for_each          = local.functions
  name              = "/aws/lambda/${local.name}-${each.key}"
  retention_in_days = var.log_retention_days
}

# ---------- IAM: one role per function (least privilege) ----------

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "lambda" {
  for_each           = local.functions
  name               = "${local.name}-${each.key}-lambda"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role_policy" "logs" {
  for_each = local.functions
  name     = "logs"
  role     = aws_iam_role.lambda[each.key].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
      Resource = "${aws_cloudwatch_log_group.lambda[each.key].arn}:*"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "xray" {
  for_each   = local.functions
  role       = aws_iam_role.lambda[each.key].name
  policy_arn = "arn:aws:iam::aws:policy/AWSXRayDaemonWriteAccess"
}

resource "aws_iam_role_policy" "accounts" {
  name = "data"
  role = aws_iam_role.lambda["accounts"].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:Query"]
        Resource = [
          aws_dynamodb_table.accounts.arn,
          "${aws_dynamodb_table.accounts.arn}/index/*",
        ]
      },
      {
        Effect   = "Allow"
        Action   = ["dynamodb:PutItem", "dynamodb:Query"]
        Resource = aws_dynamodb_table.transactions.arn
      },
    ]
  })
}

resource "aws_iam_role_policy" "transfers" {
  name = "data"
  role = aws_iam_role.lambda["transfers"].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:UpdateItem"]
        Resource = aws_dynamodb_table.accounts.arn
      },
      {
        Effect   = "Allow"
        Action   = ["dynamodb:PutItem"]
        Resource = aws_dynamodb_table.transactions.arn
      },
      {
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:PutItem"]
        Resource = aws_dynamodb_table.idempotency.arn
      },
    ]
  })
}

resource "aws_iam_role_policy" "fraud" {
  name = "data"
  role = aws_iam_role.lambda["fraud"].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["dynamodb:Query"]
        Resource = aws_dynamodb_table.transactions.arn
      },
      {
        Effect = "Allow"
        Action = [
          "dynamodb:DescribeStream",
          "dynamodb:GetRecords",
          "dynamodb:GetShardIterator",
          "dynamodb:ListStreams",
        ]
        Resource = "${aws_dynamodb_table.transactions.arn}/stream/*"
      },
      {
        Effect   = "Allow"
        Action   = ["sns:Publish"]
        Resource = aws_sns_topic.fraud_alerts.arn
      },
      {
        Effect   = "Allow"
        Action   = ["sqs:SendMessage"]
        Resource = aws_sqs_queue.fraud_dlq.arn
      },
    ]
  })
}

# ---------- Functions ----------

resource "aws_lambda_function" "fn" {
  for_each         = local.functions
  function_name    = "${local.name}-${each.key}"
  role             = aws_iam_role.lambda[each.key].arn
  handler          = each.value.handler
  runtime          = "python3.12"
  architectures    = ["arm64"] # Graviton: cheaper and faster for this workload
  filename         = data.archive_file.lambda.output_path
  source_code_hash = data.archive_file.lambda.output_base64sha256
  timeout          = each.value.timeout
  memory_size      = 256

  tracing_config {
    mode = "Active" # X-Ray
  }

  environment {
    variables = local.lambda_env
  }

  depends_on = [aws_cloudwatch_log_group.lambda, aws_iam_role_policy.logs]
}
