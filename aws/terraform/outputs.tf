output "consumer_user_name" {
  value       = aws_iam_user.consumer.name
  description = "Create this user's access key by hand (docs/SOP.md, AWS section)."
}

output "consumer_user_arn" {
  value = aws_iam_user.consumer.arn
}

output "enforcer_function_name" {
  value = aws_lambda_function.enforcer.function_name
}

output "budget_name" {
  value = aws_budgets_budget.monthly.name
}

output "budget_topic_arn" {
  value = aws_sns_topic.budget.arn
}

output "alarm_topic_arn" {
  value = aws_sns_topic.alarms.arn
}

output "test_invoke_command" {
  description = "Run the flux check once, now (append your --profile)."
  value       = "aws lambda invoke --function-name ${aws_lambda_function.enforcer.function_name} --payload '{}' /dev/stdout"
}
