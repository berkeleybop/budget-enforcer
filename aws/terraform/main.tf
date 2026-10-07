# Budget enforcer for Amazon Bedrock: the AWS counterpart of ../../terraform
# (GCP).  Three identities, as on GCP:
#   - the operator (whoever runs terraform),
#   - the enforcer role (this Lambda): may disable the consumer's keys only,
#   - the consumer user: the application's only Bedrock credential.

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}
data "aws_partition" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  region     = data.aws_region.current.region
  partition  = data.aws_partition.current.partition
  fn_name    = "${var.name}-budget-enforcer"
}

# ---------------------------------------------------------------------------
# Consumer: the application's Bedrock identity
# ---------------------------------------------------------------------------

resource "aws_iam_user" "consumer" {
  name = var.consumer_user_name
  tags = var.tags
}

data "aws_iam_policy_document" "consumer" {
  statement {
    sid       = "MantleInference"
    actions   = ["bedrock-mantle:CreateInference"]
    resources = ["arn:${local.partition}:bedrock-mantle:${local.region}:${local.account_id}:project/*"]
    condition {
      test     = "StringEquals"
      variable = "bedrock-mantle:Model"
      values   = var.allowed_mantle_models
    }
  }

  dynamic "statement" {
    for_each = length(var.allowed_runtime_model_arns) > 0 ? [1] : []
    content {
      sid       = "RuntimeInvoke"
      actions   = ["bedrock:InvokeModel"]
      resources = var.allowed_runtime_model_arns
    }
  }

  # Bedrock API keys (bearer tokens) issued to this user would keep working
  # after its access key is disabled.  Refuse them outright.
  statement {
    sid       = "NoBearerTokens"
    effect    = "Deny"
    actions   = ["bedrock:CallWithBearerToken", "bedrock-mantle:CallWithBearerToken"]
    resources = ["*"]
  }
}

resource "aws_iam_user_policy" "consumer" {
  name   = "${var.name}-bedrock-only"
  user   = aws_iam_user.consumer.name
  policy = data.aws_iam_policy_document.consumer.json
}

# ---------------------------------------------------------------------------
# Enforcer: Lambda role and function
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "enforcer" {
  name               = local.fn_name
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
  tags               = var.tags
}

data "aws_iam_policy_document" "enforcer" {
  statement {
    sid       = "ConsumerKeysOnly"
    actions   = ["iam:ListAccessKeys", "iam:UpdateAccessKey", "iam:ListUserTags"]
    resources = [aws_iam_user.consumer.arn]
  }
  statement {
    sid       = "ReadMetrics"
    actions   = ["cloudwatch:ListMetrics", "cloudwatch:GetMetricData"]
    resources = ["*"]
  }
  statement {
    sid       = "OwnLogs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.enforcer.arn}:*"]
  }
}

resource "aws_iam_role_policy" "enforcer" {
  name   = local.fn_name
  role   = aws_iam_role.enforcer.id
  policy = data.aws_iam_policy_document.enforcer.json
}

resource "aws_cloudwatch_log_group" "enforcer" {
  name              = "/aws/lambda/${local.fn_name}"
  retention_in_days = 90
  tags              = var.tags
}

data "archive_file" "handler" {
  type        = "zip"
  source_file = "${path.module}/../handler.py"
  output_path = "${path.module}/.build/handler.zip"
}

resource "aws_lambda_function" "enforcer" {
  function_name    = local.fn_name
  role             = aws_iam_role.enforcer.arn
  runtime          = "python3.12"
  architectures    = ["arm64"]
  handler          = "handler.lambda_handler"
  filename         = data.archive_file.handler.output_path
  source_code_hash = data.archive_file.handler.output_base64sha256
  timeout          = 60
  memory_size      = 128

  # One run at a time: a billing notification and a scheduled check
  # never race on the same keys.
  reserved_concurrent_executions = 1

  environment {
    variables = {
      CONSUMER_USER         = aws_iam_user.consumer.name
      MONTHLY_BUDGET        = tostring(var.monthly_budget_usd)
      ENFORCEMENT_TOLERANCE = tostring(var.enforcement_tolerance)
      FLUX_WINDOW           = var.flux_window
      SLACK_WEBHOOK_URL     = var.slack_webhook_url
      DEPLOYMENT_NAME       = var.name
    }
  }

  depends_on = [aws_cloudwatch_log_group.enforcer, aws_iam_role_policy.enforcer]
  tags       = var.tags
}

# ---------------------------------------------------------------------------
# Flux path: schedule
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_event_rule" "flux" {
  name                = "${local.fn_name}-flux"
  schedule_expression = var.schedule_expression
  tags                = var.tags
}

resource "aws_cloudwatch_event_target" "flux" {
  rule = aws_cloudwatch_event_rule.flux.name
  arn  = aws_lambda_function.enforcer.arn
}

resource "aws_lambda_permission" "flux" {
  statement_id  = "AllowEventBridge"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.enforcer.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.flux.arn
}

# ---------------------------------------------------------------------------
# Billing path: budget -> SNS -> Lambda
# ---------------------------------------------------------------------------

resource "aws_sns_topic" "budget" {
  name = "${local.fn_name}-budget"
  tags = var.tags
}

data "aws_iam_policy_document" "budget_topic" {
  statement {
    actions   = ["SNS:Publish"]
    resources = [aws_sns_topic.budget.arn]
    principals {
      type        = "Service"
      identifiers = ["budgets.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

resource "aws_sns_topic_policy" "budget" {
  arn    = aws_sns_topic.budget.arn
  policy = data.aws_iam_policy_document.budget_topic.json
}

resource "aws_sns_topic_subscription" "budget_to_lambda" {
  topic_arn = aws_sns_topic.budget.arn
  protocol  = "lambda"
  endpoint  = aws_lambda_function.enforcer.arn
}

resource "aws_lambda_permission" "budget" {
  statement_id  = "AllowBudgetTopic"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.enforcer.function_name
  principal     = "sns.amazonaws.com"
  source_arn    = aws_sns_topic.budget.arn
}

# Unfiltered: Claude on Bedrock bills as one Marketplace product per model
# ("Claude Opus 5 (Amazon Bedrock Edition)"), so a service filter would need
# changing at every model change.  Deploy in an account where this
# application is the only Bedrock workload.
resource "aws_budgets_budget" "monthly" {
  name         = "${local.fn_name}-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.monthly_budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  dynamic "notification" {
    for_each = var.email_thresholds_percent
    content {
      comparison_operator        = "GREATER_THAN"
      threshold                  = notification.value
      threshold_type             = "PERCENTAGE"
      notification_type          = "ACTUAL"
      subscriber_email_addresses = var.alert_emails
      subscriber_sns_topic_arns  = notification.value >= 100 ? [aws_sns_topic.budget.arn] : []
    }
  }

  depends_on = [aws_sns_topic_policy.budget]
  tags       = var.tags
}

# ---------------------------------------------------------------------------
# Enforcer health: alarms to email
# ---------------------------------------------------------------------------

resource "aws_sns_topic" "alarms" {
  name = "${local.fn_name}-alarms"
  tags = var.tags
}

resource "aws_sns_topic_subscription" "alarm_emails" {
  for_each  = toset(var.alert_emails)
  topic_arn = aws_sns_topic.alarms.arn
  protocol  = "email"
  endpoint  = each.value
}

resource "aws_cloudwatch_metric_alarm" "errors" {
  alarm_name          = "${local.fn_name}-errors"
  alarm_description   = "The budget enforcer Lambda raised an error."
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = aws_lambda_function.enforcer.function_name }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alarms.arn]
  ok_actions          = [aws_sns_topic.alarms.arn]
  tags                = var.tags
}

resource "aws_cloudwatch_metric_alarm" "not_running" {
  alarm_name          = "${local.fn_name}-not-running"
  alarm_description   = "The budget enforcer has not run for 15 minutes."
  namespace           = "AWS/Lambda"
  metric_name         = "Invocations"
  dimensions          = { FunctionName = aws_lambda_function.enforcer.function_name }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 3
  threshold           = 1
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "breaching"
  alarm_actions       = [aws_sns_topic.alarms.arn]
  ok_actions          = [aws_sns_topic.alarms.arn]
  tags                = var.tags
}
