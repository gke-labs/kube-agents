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
  project = var.project_id
  name    = var.kms_keyring_name
  # Cloud KMS has no zonal locations; a zonal cluster location maps to its
  # region, matching the chart's KMS_KEY_NAME derivation and the installer's
  # derive_kms_location.
  location = replace(var.location, "/-[a-z]$/", "")
}

# The key is created import-only and without an initial version: the GitHub
# App private key PEM is imported into it afterwards (install.sh does it via
# the Minty CLI; the README carries the manual command).
resource "google_kms_crypto_key" "minter" {
  #checkov:skip=CKV_GCP_82:Import-only asymmetric signing key lifecycle is managed via Minty/KMS
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
