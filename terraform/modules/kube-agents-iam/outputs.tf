output "service_account_email" {
  description = "Email of the created IAM service account"
  value       = google_service_account.agent.email
}
