variable "project" {
  description = "Name prefix for all resources"
  type        = string
  default     = "ledgerly"
}

variable "environment" {
  description = "Deployment environment"
  type        = string
  default     = "dev"

  validation {
    condition     = contains(["dev", "prod"], var.environment)
    error_message = "environment must be dev or prod."
  }
}

variable "region" {
  description = "AWS region"
  type        = string
  default     = "eu-west-2" # London
}

variable "alert_email" {
  description = "Email for fraud and ops alerts (leave empty to skip subscriptions)"
  type        = string
  default     = ""
}

variable "large_transfer_threshold" {
  description = "Debits at or above this amount (minor units) are flagged. 100000 = 1,000.00"
  type        = number
  default     = 100000
}

variable "velocity_limit" {
  description = "Flag an account making more than this many debits within the window"
  type        = number
  default     = 5
}

variable "log_retention_days" {
  description = "CloudWatch log retention"
  type        = number
  default     = 30
}
