output "investigator_service_account_email" {
  description = "Email of the investigator's IAM service account, for confirming the pair lines up. The chart does not consume it: it builds the KSA's Workload Identity annotation itself from selfImprovement.github.gsaName and the project, so this email must equal that construction."
  value       = google_service_account.investigator.email
}
