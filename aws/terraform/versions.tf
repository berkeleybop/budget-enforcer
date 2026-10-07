# Reusable module: no provider block here.  The calling configuration sets
# the AWS provider, and should pin the account with `allowed_account_ids`.
terraform {
  required_version = ">= 1.5"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 6.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = ">= 2.4"
    }
  }
}
