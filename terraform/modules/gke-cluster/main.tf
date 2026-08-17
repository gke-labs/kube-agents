# GKE Service Agent identity for KMS access
resource "google_project_service_identity" "gke_service_agent" {
  count    = var.enable_database_encryption ? 1 : 0
  provider = google-beta
  project  = var.project_id
  service  = "container.googleapis.com"
}

# Cloud KMS Keyring and CryptoKey for GKE Database Encryption (etcd CMEK)
resource "google_kms_key_ring" "gke_keyring" {
  count    = var.enable_database_encryption ? 1 : 0
  name     = var.kms_keyring_name
  location = var.location
  project  = var.project_id
}

resource "google_kms_crypto_key" "gke_key" {
  count    = var.enable_database_encryption ? 1 : 0
  name     = var.kms_key_name
  key_ring = google_kms_key_ring.gke_keyring[0].id
  purpose  = "ENCRYPT_DECRYPT"
}

resource "google_kms_crypto_key_iam_member" "gke_kms_binding" {
  count         = var.enable_database_encryption ? 1 : 0
  crypto_key_id = google_kms_crypto_key.gke_key[0].id
  role          = "roles/cloudkms.cryptoKeyEncrypterDecrypter"
  member        = "serviceAccount:${google_project_service_identity.gke_service_agent[0].email}"
}

resource "google_container_cluster" "autopilot" {
  name     = var.cluster_name
  location = var.location
  project  = var.project_id

  enable_autopilot    = true
  deletion_protection = var.deletion_protection
  resource_labels     = var.resource_labels

  # Matches provision_01's --enable-fqdn-network-policy. Autopilot always runs
  # Dataplane V2, so the script's --enable-dataplane-v2 needs no counterpart.
  enable_fqdn_network_policy = var.enable_fqdn_network_policy

  workload_identity_config {
    workload_pool = "${var.project_id}.svc.id.goog"
  }

  release_channel {
    channel = var.release_channel
  }

  dynamic "database_encryption" {
    for_each = var.enable_database_encryption ? [1] : []
    content {
      state    = "ENCRYPTED"
      key_name = google_kms_crypto_key.gke_key[0].id
    }
  }

  # Backup for GKE. provision_01_gcp_cluster.sh creates its cluster with
  # `--addons=GcpFilestoreCsiDriver,BackupRestore`, so the agent is installed
  # on a backup-capable cluster either way; the gke-backup-plan module (and
  # provision_12_gke_backup_plan.sh) then schedules the backups themselves.
  # The agent has to be enabled on the cluster before a BackupPlan can
  # target it.
  #
  # Only the BackupRestore half is mirrored. Nothing in the harness mounts a
  # Filestore volume, and this module builds an Autopilot cluster, where
  # `gcloud container clusters create-auto` has no --addons flag to pass either
  # half. Recorded in the divergence lists that
  # .agents/skills/review-iac-parity/SKILL.md and scripts/check_iac_parity.py
  # share.
  addons_config {
    gke_backup_agent_config {
      enabled = var.enable_backup_agent
    }
  }

  depends_on = [
    google_kms_crypto_key_iam_member.gke_kms_binding
  ]
}
