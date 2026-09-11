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

output "service_account_id" {
  description = "Account id of the minter's GSA (the part before the @). The chart's githubMinter.gsaName annotates the minter KSA with it, so read it from here rather than from the input: a null input selects this module's default, and the annotation has to follow whichever name the module actually created."
  value       = google_service_account.minter.account_id
}
