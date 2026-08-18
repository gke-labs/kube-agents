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
  #checkov:skip=CKV_GCP_82:Database encryption key lifecycle managed according to cluster policy
  count           = var.enable_database_encryption ? 1 : 0
  name            = var.kms_key_name
  key_ring        = google_kms_key_ring.gke_keyring[0].id
  purpose         = "ENCRYPT_DECRYPT"
  rotation_period = "7776000s"
}

resource "google_kms_crypto_key_iam_member" "gke_kms_binding" {
  count         = var.enable_database_encryption ? 1 : 0
  crypto_key_id = google_kms_crypto_key.gke_key[0].id
  role          = "roles/cloudkms.cryptoKeyEncrypterDecrypter"
  member        = "serviceAccount:${google_project_service_identity.gke_service_agent[0].email}"
}

resource "google_container_cluster" "autopilot" {
  #checkov:skip=CKV_GCP_12:GKE Autopilot manages Dataplane V2 network policies automatically
  #checkov:skip=CKV_GCP_13:Client certificate authentication disabled by default in Autopilot
  #checkov:skip=CKV_GCP_20:Public control plane access required for operator kubectl connectivity without VPN or bastion
  #checkov:skip=CKV_GCP_21:Cluster resource labels are configured via var.resource_labels
  #checkov:skip=CKV_GCP_23:VPC-native alias IP is default and enforced on GKE Autopilot
  #checkov:skip=CKV_GCP_25:Public cluster endpoint required for developer and CI operator access in quickstart module
  #checkov:skip=CKV_GCP_61:Intra-node visibility not required for standard quickstart cluster telemetry
  #checkov:skip=CKV_GCP_64:Public node routing enabled for standard egress without Cloud NAT in quickstart module
  #checkov:skip=CKV_GCP_65:Google Groups RBAC integration not required for single-tenant agent host cluster
  #checkov:skip=CKV_GCP_66:Binary authorization not required for quickstart agent deployment module
  #checkov:skip=CKV_GCP_69:Workload Identity metadata server is enabled by default in Autopilot
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

  # Whether the DNS-based control plane endpoint serves traffic from outside the
  # VPC. The Platform Agent reaches fleet clusters from wherever it runs, and a
  # cluster with only an IP endpoint it cannot route to is unreachable.
  # allow_external_traffic is the field the agent's detection reads before it
  # passes `get-credentials --dns-endpoint`; see
  # k8s-operator/scripts/gke_dns_endpoint.sh.
  #
  # Defaults to false, matching GKE's own default and the value every cluster
  # this module already manages is sitting at. The module did not set this block
  # before, so defaulting to true would have made the next apply of an existing
  # root publish an externally reachable control plane on a cluster whose
  # operator never asked for one -- and this endpoint is governed by IAM alone,
  # so neither the private endpoint nor master-authorized-networks would be
  # holding it shut. Opting in is therefore a deliberate edit to the caller's
  # configuration; the gcloud path (provision_01_gcp_cluster.sh) enables it on
  # create only, for the same reason.
  #
  # Once set either way the field is Terraform-managed, so change it here rather
  # than with `gcloud container clusters update`: out-of-band it is drift that
  # the next apply reverts.
  control_plane_endpoints_config {
    dns_endpoint_config {
      allow_external_traffic = var.allow_external_dns_traffic
    }
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
