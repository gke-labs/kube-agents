output "service_account_email" {
  description = "Email of the GitHub token minter's IAM service account"
  value       = google_service_account.minter.email
}

output "kms_keyring" {
  description = "Name of the KMS key ring holding the signing key"
  value       = google_kms_key_ring.minter.name
}

output "kms_key" {
  description = "Name of the KMS signing key (import the GitHub App PEM into it)"
  value       = google_kms_crypto_key.minter.name
}
