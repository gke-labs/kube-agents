locals {
  # KMS is only managed alongside a cluster this module creates. For an
  # existing cluster (create_cluster = false) CMEK is a gcloud update the
  # caller runs outside Terraform, against whatever key they choose.
  manage_kms = var.create_cluster && var.enable_database_encryption

  # Cloud KMS has no zonal locations, so a zonal cluster location maps to its
  # enclosing region — the same derivation as derive_kms_location in
  # k8s-operator/scripts/installer_common.sh.
  kms_location = replace(var.location, "/-[a-z]$/", "")
}

# The module predates cluster_mode; the Autopilot cluster used to be the only
# resource and carried no index. Without this move, upgrading the module would
# plan a destroy/recreate of a live cluster.
moved {
  from = google_container_cluster.autopilot
  to   = google_container_cluster.autopilot[0]
}

# GKE Service Agent identity for KMS access
resource "google_project_service_identity" "gke_service_agent" {
  count    = local.manage_kms ? 1 : 0
  provider = google-beta
  project  = var.project_id
  service  = "container.googleapis.com"
}

# Cloud KMS Keyring and CryptoKey for GKE Database Encryption (etcd CMEK)
resource "google_kms_key_ring" "gke_keyring" {
  count    = local.manage_kms ? 1 : 0
  name     = var.kms_keyring_name
  location = local.kms_location
  project  = var.project_id
}

resource "google_kms_crypto_key" "gke_key" {
  #checkov:skip=CKV_GCP_82:Database encryption key lifecycle managed according to cluster policy
  count           = local.manage_kms ? 1 : 0
  name            = var.kms_key_name
  key_ring        = google_kms_key_ring.gke_keyring[0].id
  purpose         = "ENCRYPT_DECRYPT"
  rotation_period = "7776000s"
}

resource "google_kms_crypto_key_iam_member" "gke_kms_binding" {
  count         = local.manage_kms ? 1 : 0
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
  count    = var.create_cluster && var.cluster_mode == "autopilot" ? 1 : 0
  name     = var.cluster_name
  location = var.location
  project  = var.project_id

  enable_autopilot    = true
  deletion_protection = var.deletion_protection
  resource_labels     = var.resource_labels

  # Autopilot always runs Dataplane V2, so FQDN NetworkPolicy is the only
  # datapath knob to set here.
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
  # configuration.
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
    for_each = local.manage_kms ? [1] : []
    content {
      state    = "ENCRYPTED"
      key_name = google_kms_crypto_key.gke_key[0].id
    }
  }

  # Backup for GKE: the agent is enabled here, and the gke-backup-plan module
  # then schedules the backups themselves. The agent has to be enabled on the
  # cluster before a BackupPlan can target it.
  #
  # Only the backup agent is enabled here, without the Filestore CSI driver
  # the standard cluster below also carries: nothing in the harness mounts a
  # Filestore volume, and Autopilot manages its own CSI drivers.
  addons_config {
    gke_backup_agent_config {
      enabled = var.enable_backup_agent
    }
  }

  lifecycle {
    precondition {
      condition     = can(regex("^[a-z]+-[a-z]+[0-9]+$", var.location))
      error_message = "GKE Autopilot clusters are regional: location must be a region (e.g. us-central1), not a zone."
    }
  }

  depends_on = [
    google_kms_crypto_key_iam_member.gke_kms_binding
  ]
}

# A GKE Standard cluster: e2-standard-4 default node pool (one node per
# zone), Workload Identity, CMEK database encryption, the Filestore CSI and
# BackupRestore addons, and Dataplane V2 with FQDN NetworkPolicy. The one
# piece with no first-class field below is the managed OpenTelemetry
# collection scope (gcloud's --managed-otel-scope), which neither google
# provider knows (checked against 7.45): it stays a post-create
# `gcloud container clusters update` step for callers that want it.
resource "google_container_cluster" "standard" {
  #checkov:skip=CKV_GCP_12:Dataplane V2 (ADVANCED_DATAPATH) enforces NetworkPolicy without the addon
  #checkov:skip=CKV_GCP_13:Client certificate authentication is disabled by default on current GKE
  #checkov:skip=CKV_GCP_20:Public control plane access required for operator kubectl connectivity without VPN or bastion
  #checkov:skip=CKV_GCP_21:Cluster resource labels are configured via var.resource_labels
  #checkov:skip=CKV_GCP_25:Public cluster endpoint required for developer and CI operator access in quickstart module
  #checkov:skip=CKV_GCP_61:Intra-node visibility not required for standard quickstart cluster telemetry
  #checkov:skip=CKV_GCP_64:Public node routing enabled for standard egress without Cloud NAT in quickstart module
  #checkov:skip=CKV_GCP_65:Google Groups RBAC integration not required for single-tenant agent host cluster
  #checkov:skip=CKV_GCP_66:Binary authorization not required for quickstart agent deployment module
  #checkov:skip=CKV_GCP_67:Legacy metadata endpoints are disabled by default on current GKE node versions
  #checkov:skip=CKV_GCP_68:Secure boot stays at the gcloud create default
  #checkov:skip=CKV_GCP_69:Workload metadata is pinned to GKE_METADATA in node_config
  count    = var.create_cluster && var.cluster_mode == "standard" ? 1 : 0
  name     = var.cluster_name
  location = var.location
  project  = var.project_id

  deletion_protection = var.deletion_protection
  resource_labels     = var.resource_labels

  # The default node pool, kept (not replaced with a managed pool) because that
  # is the cluster shape `gcloud container clusters create` produced: num-nodes
  # is per zone, so a regional location yields one node per zone.
  initial_node_count = var.standard_node_count

  node_config {
    machine_type = var.standard_machine_type
    # gcloud's default "gke-default" scope set.
    oauth_scopes = [
      "https://www.googleapis.com/auth/devstorage.read_only",
      "https://www.googleapis.com/auth/logging.write",
      "https://www.googleapis.com/auth/monitoring",
      "https://www.googleapis.com/auth/service.management.readonly",
      "https://www.googleapis.com/auth/servicecontrol",
      "https://www.googleapis.com/auth/trace.append",
    ]
    workload_metadata_config {
      mode = "GKE_METADATA"
    }
  }

  # Dataplane V2. Requires VPC-native networking; the empty
  # ip_allocation_policy lets GKE pick secondary ranges, as
  # `gcloud container clusters create` does.
  networking_mode = "VPC_NATIVE"
  ip_allocation_policy {
  }
  datapath_provider          = "ADVANCED_DATAPATH"
  enable_fqdn_network_policy = var.enable_fqdn_network_policy

  workload_identity_config {
    workload_pool = "${var.project_id}.svc.id.goog"
  }

  release_channel {
    channel = var.release_channel
  }

  # See the comment on the autopilot resource: same field, same default, same
  # reason.
  control_plane_endpoints_config {
    dns_endpoint_config {
      allow_external_traffic = var.allow_external_dns_traffic
    }
  }

  dynamic "database_encryption" {
    for_each = local.manage_kms ? [1] : []
    content {
      state    = "ENCRYPTED"
      key_name = google_kms_crypto_key.gke_key[0].id
    }
  }

  # Filestore CSI for workloads that mount Filestore volumes, and the Backup
  # for GKE agent, which must be enabled on the cluster before the
  # gke-backup-plan module can schedule a BackupPlan against it.
  addons_config {
    gcp_filestore_csi_driver_config {
      enabled = true
    }
    gke_backup_agent_config {
      enabled = var.enable_backup_agent
    }
  }

  depends_on = [
    google_kms_crypto_key_iam_member.gke_kms_binding
  ]
}

# An existing cluster somebody else made (create_cluster = false). Read-only:
# the module contributes outputs, and the rest of the composition installs onto
# it. Works for Autopilot and Standard alike — cluster_mode only governs what
# would be created.
data "google_container_cluster" "existing" {
  count    = var.create_cluster ? 0 : 1
  name     = var.cluster_name
  location = var.location
  project  = var.project_id

  lifecycle {
    # kube-agents requires Workload Identity: every KSA→GSA binding the
    # composition creates rides the pool, and against a cluster without one
    # those bindings apply cleanly while the pods silently run as the node's
    # service account. A data source cannot enable it (install.sh does, via
    # ensure_existing_cluster_workload_identity), so refuse the plan instead
    # of installing something that reports success and authenticates as the
    # wrong identity.
    postcondition {
      condition     = try(self.workload_identity_config[0].workload_pool, "") == "${var.project_id}.svc.id.goog"
      error_message = "Cluster '${var.cluster_name}' has no Workload Identity pool, which kube-agents requires. Enable it first — gcloud container clusters update ${var.cluster_name} --location ${var.location} --project ${var.project_id} --workload-pool=${var.project_id}.svc.id.goog — and migrate any node pool still on the legacy metadata server to GKE_METADATA, then re-run."
    }

    # Same reasoning for NetworkPolicy enforcement: every NetworkPolicy the
    # install ships is accepted and silently inert on a cluster with neither
    # Dataplane V2 nor the legacy Calico addon — GKE Standard's default —
    # and the isolation the security reference documents would be absent
    # while the install reports success. install.sh enables the legacy addon
    # on adopted clusters (ensure_existing_cluster_network_policy); a bare
    # Terraform run refuses instead.
    postcondition {
      condition     = try(self.datapath_provider, "") == "ADVANCED_DATAPATH" || try(self.network_policy[0].enabled, false) == true
      error_message = "Cluster '${var.cluster_name}' enforces no NetworkPolicy (neither Dataplane V2 nor the legacy addon), so every NetworkPolicy kube-agents installs would be inert. Run these two commands in this order, then re-run: gcloud container clusters update ${var.cluster_name} --location ${var.location} --project ${var.project_id} --update-addons=NetworkPolicy=ENABLED, then gcloud container clusters update ${var.cluster_name} --location ${var.location} --project ${var.project_id} --enable-network-policy. GKE rejects the second until the addon is on, and gcloud rejects both flags in one invocation."
    }
  }
}

# A dedicated GKE Sandbox (gVisor) node pool. Standard only: Autopilot ships
# the gvisor RuntimeClass without a pool. The count is on the variable alone
# so that asking for the pool on an Autopilot cluster fails the plan loudly
# instead of being ignored.
resource "google_container_node_pool" "gvisor" {
  #checkov:skip=CKV_GCP_68:Secure boot stays at the gcloud create default
  count    = var.enable_gvisor_node_pool ? 1 : 0
  name     = var.gvisor_pool_name
  cluster  = local.cluster_name
  location = var.location
  project  = var.project_id

  # Per zone: a regional location yields one node per zone.
  initial_node_count = 1

  node_config {
    machine_type = "e2-standard-4"
    image_type   = "COS_CONTAINERD"
    sandbox_config {
      type = "GVISOR"
    }
    workload_metadata_config {
      mode = "GKE_METADATA"
    }
  }

  lifecycle {
    precondition {
      # The second clause covers create_cluster = false, where cluster_mode
      # only describes what would have been created: the pre-existing cluster
      # can still be Autopilot, and asking it for a node pool must fail at
      # plan time, not mid-apply at the API. try() keeps the expression legal
      # when the data source has count 0 (create_cluster = true).
      condition     = var.cluster_mode == "standard" && try(data.google_container_cluster.existing[0].enable_autopilot, false) != true
      error_message = "enable_gvisor_node_pool requires a Standard cluster: Autopilot provides the gvisor RuntimeClass natively, with no node pool to manage. (cluster_mode must be \"standard\", and an adopted pre-existing cluster must not be Autopilot.)"
    }
  }
}

locals {
  # Exactly one of the three cluster sources exists; concat + one() folds them
  # without a type-unifying conditional across resource and data-source objects.
  cluster_name = one(concat(
    google_container_cluster.autopilot[*].name,
    google_container_cluster.standard[*].name,
    data.google_container_cluster.existing[*].name,
  ))
}
