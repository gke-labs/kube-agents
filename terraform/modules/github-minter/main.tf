resource "google_service_account" "minter" {
  project      = var.project_id
  account_id   = var.service_account_id
  display_name = "Kube-Agents GitHub Token Minter Service Account"
}

resource "google_service_account_iam_member" "workload_identity" {
  service_account_id = google_service_account.minter.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "serviceAccount:${var.project_id}.svc.id.goog[${var.namespace}/${var.ksa_name}]"
}

resource "google_kms_key_ring" "minter" {
  project  = var.project_id
  name     = var.kms_keyring_name
  location = var.location
}

# The key is created import-only and without an initial version: the GitHub
# App private key PEM is imported into it afterwards (a manual step, or
# k8s-operator/scripts/provision_10_deploy_github_minter.sh via the Minty CLI).
resource "google_kms_crypto_key" "minter" {
  name     = var.kms_key_name
  key_ring = google_kms_key_ring.minter.id
  purpose  = "ASYMMETRIC_SIGN"

  version_template {
    algorithm        = "RSA_SIGN_PKCS1_2048_SHA256"
    protection_level = "SOFTWARE"
  }

  import_only                   = true
  skip_initial_version_creation = true
}

resource "google_kms_crypto_key_iam_member" "signer_verifier" {
  crypto_key_id = google_kms_crypto_key.minter.id
  role          = "roles/cloudkms.signerVerifier"
  member        = "serviceAccount:${google_service_account.minter.email}"
}
