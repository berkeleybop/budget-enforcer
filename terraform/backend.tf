# -----------------------------------------------------------------------------
# State is LOCAL only.
#
# Each developer keeps their own terraform.tfstate on their machine.
# There is no shared remote backend (no Terraform Cloud, no GCS, no S3).
#
# This means:
#   - Only the person who ran "terraform apply" has the state for that
#     project. If you need to manage someone else's deployment, ask them
#     for their terraform.tfstate file (or re-import resources — see below).
#   - State files contain sensitive data (SA emails, resource IDs). Transfer
#     them securely and never commit them (they are gitignored).
#   - Two people should not run "terraform apply" against the same project
#     without coordinating — there is no state locking.
#
# If you need to take over an existing deployment without the original
# state file, you can reconstruct state by importing each resource:
#
#   terraform import google_service_account.consumer \
#     projects/PROJECT_ID/serviceAccounts/SA_EMAIL
#   terraform import google_pubsub_topic.budget_alerts \
#     projects/PROJECT_ID/topics/budget-alerts-01
#   ... (see "terraform import" docs for each resource type)
#
# A full import sequence is documented in docs/SOP.md.
# -----------------------------------------------------------------------------
