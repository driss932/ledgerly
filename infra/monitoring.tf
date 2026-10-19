resource "aws_cloudwatch_metric_alarm" "lambda_errors" {
  for_each            = local.functions
  alarm_name          = "${local.name}-${each.key}-errors"
  alarm_description   = "The ${each.key} Lambda is throwing errors"
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = aws_lambda_function.fn[each.key].function_name }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.ops_alerts.arn]
  ok_actions          = [aws_sns_topic.ops_alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "api_5xx" {
  alarm_name          = "${local.name}-api-5xx"
  alarm_description   = "The API is returning server errors"
  namespace           = "AWS/ApiGateway"
  metric_name         = "5xx"
  dimensions          = { ApiId = aws_apigatewayv2_api.http.id }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.ops_alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "fraud_dlq" {
  alarm_name          = "${local.name}-fraud-dlq-not-empty"
  alarm_description   = "Some transactions could not be fraud-checked"
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  dimensions          = { QueueName = aws_sqs_queue.fraud_dlq.name }
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.ops_alerts.arn]
}

resource "aws_cloudwatch_dashboard" "main" {
  dashboard_name = local.name
  dashboard_body = jsonencode({
    widgets = [
      {
        type   = "metric"
        x      = 0
        y      = 0
        width  = 12
        height = 6
        properties = {
          title  = "API requests and errors"
          region = data.aws_region.current.name
          stat   = "Sum"
          period = 300
          metrics = [
            ["AWS/ApiGateway", "Count", "ApiId", aws_apigatewayv2_api.http.id],
            ["AWS/ApiGateway", "4xx", "ApiId", aws_apigatewayv2_api.http.id],
            ["AWS/ApiGateway", "5xx", "ApiId", aws_apigatewayv2_api.http.id],
          ]
        }
      },
      {
        type   = "metric"
        x      = 12
        y      = 0
        width  = 12
        height = 6
        properties = {
          title  = "API latency (p50 / p99, ms)"
          region = data.aws_region.current.name
          period = 300
          metrics = [
            ["AWS/ApiGateway", "Latency", "ApiId", aws_apigatewayv2_api.http.id, { stat = "p50" }],
            ["AWS/ApiGateway", "Latency", "ApiId", aws_apigatewayv2_api.http.id, { stat = "p99" }],
          ]
        }
      },
      {
        type   = "metric"
        x      = 0
        y      = 6
        width  = 12
        height = 6
        properties = {
          title  = "Lambda errors"
          region = data.aws_region.current.name
          stat   = "Sum"
          period = 300
          metrics = [
            for name, fn in aws_lambda_function.fn :
            ["AWS/Lambda", "Errors", "FunctionName", fn.function_name]
          ]
        }
      },
      {
        type   = "metric"
        x      = 12
        y      = 6
        width  = 12
        height = 6
        properties = {
          title   = "Transfers flagged for fraud review"
          region  = data.aws_region.current.name
          stat    = "Sum"
          period  = 300
          metrics = [["Ledgerly", "FraudFlags", "Environment", var.environment]]
        }
      },
    ]
  })
}
