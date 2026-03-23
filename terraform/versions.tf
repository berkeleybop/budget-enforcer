terraform {
  required_version = ">= 1.5"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.cloud_run_region

  # Required for billing budget creation via Application Default Credentials.
  # When running as a personal account (gcloud auth application-default login),
  # the Billing Budget API needs a quota project to bill API usage against.
  # Without these two settings, you get a 403 "requires a quota project" error
  # on the google_billing_budget resource.
  billing_project       = var.project_id
  user_project_override = true
}
