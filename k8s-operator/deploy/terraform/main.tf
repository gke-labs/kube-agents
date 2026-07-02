# ==============================================================================
# 🤖 GCP Project & Enabled APIs
# ==============================================================================
data "google_project" "project" {
  project_id = var.project_id
}

resource "google_project_service" "apis" {
  for_each = toset([
    "container.googleapis.com",
    "pubsub.googleapis.com",
    "chat.googleapis.com",
    "gsuiteaddons.googleapis.com",
    "cloudresourcemanager.googleapis.com",
    "cloudkms.googleapis.com"
  ])
  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}

# ==============================================================================
# 🤖 GKE Standard Cluster Provisioning
# ==============================================================================
resource "google_container_cluster" "primary" {
  name                     = var.cluster_name
  location                 = var.region
  project                  = var.project_id
  remove_default_node_pool = true
  initial_node_count       = 1
  deletion_protection      = var.deletion_protection

  workload_identity_config {
    workload_pool = "${var.project_id}.svc.id.goog"
  }

  monitoring_config {
    enable_components = ["SYSTEM_COMPONENTS"]
    managed_prometheus {
      enabled = true
    }
  }

  depends_on = [google_project_service.apis]
}

resource "google_container_node_pool" "primary_nodes" {
  name       = "primary-node-pool"
  location   = var.region
  cluster    = google_container_cluster.primary.name
  project    = var.project_id
  node_count = 1 # 1 node per zone, total 3 nodes

  node_config {
    preemptible  = false
    machine_type = "e2-standard-4"

    workload_metadata_config {
      mode = "GKE_METADATA"
    }

    oauth_scopes = [
      "https://www.googleapis.com/auth/cloud-platform"
    ]
  }
}

# ==============================================================================
# 🤖 GCP Service Accounts (GSAs) Creation
# ==============================================================================
resource "google_service_account" "controller" {
  account_id   = var.controller_gsa_name
  display_name = "Kubeagents Controller Manager GSA"
  project      = var.project_id
}

resource "google_service_account" "platform_agent" {
  account_id   = var.platform_gsa_name
  display_name = "Platform Agent GSA"
  project      = var.project_id
}

resource "google_service_account" "operator_agent" {
  account_id   = var.operator_gsa_name
  display_name = "Operator Agent GSA"
  project      = var.project_id
}

resource "google_service_account" "devteam_agent" {
  account_id   = var.devteam_gsa_name
  display_name = "DevTeam Agent GSA"
  project      = var.project_id
}

# ==============================================================================
# 🤖 GCP IAM Roles & Permissions
# ==============================================================================

# Helper local to flatten the IAM role mapping
locals {
  controller_roles = [
    "roles/container.clusterViewer",
    "roles/container.admin"
  ]
  platform_roles = [
    "roles/container.clusterAdmin",
    "roles/container.admin",
    "roles/monitoring.admin",
    "roles/logging.admin",
    "roles/iam.serviceAccountUser",
    "roles/iam.securityReviewer"
  ]
  operator_roles = [
    "roles/container.clusterViewer",
    "roles/monitoring.viewer",
    "roles/logging.viewer",
    "roles/iam.serviceAccountUser"
  ]
  devteam_roles = [
    "roles/container.clusterViewer",
    "roles/monitoring.viewer",
    "roles/logging.viewer",
    "roles/iam.serviceAccountUser"
  ]

  # Create a list of maps to loop through for project level IAM bindings
  iam_bindings = concat(
    [for r in local.controller_roles : { gsa = google_service_account.controller.email, role = r }],
    [for r in local.platform_roles : { gsa = google_service_account.platform_agent.email, role = r }],
    [for r in local.operator_roles : { gsa = google_service_account.operator_agent.email, role = r }],
    [for r in local.devteam_roles : { gsa = google_service_account.devteam_agent.email, role = r }]
  )
}

resource "google_project_iam_member" "gsa_bindings" {
  for_each = { for idx, val in local.iam_bindings : "${val.gsa}-${val.role}" => val }
  project  = var.project_id
  role     = each.value.role
  member   = "serviceAccount:${each.value.gsa}"
}

# ==============================================================================
# 🤖 GCP Workload Identity IAM Bindings
# ==============================================================================
resource "google_service_account_iam_member" "controller_wi" {
  service_account_id = google_service_account.controller.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "serviceAccount:${var.project_id}.svc.id.goog[${var.namespace}/kubeagents-controller]"
}

resource "google_service_account_iam_member" "platform_agent_wi" {
  service_account_id = google_service_account.platform_agent.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "serviceAccount:${var.project_id}.svc.id.goog[${var.namespace}/kubeagents-platform-agent]"
}

resource "google_service_account_iam_member" "operator_agent_wi" {
  service_account_id = google_service_account.operator_agent.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "serviceAccount:${var.project_id}.svc.id.goog[${var.namespace}/kubeagents-operator-agent]"
}

resource "google_service_account_iam_member" "devteam_agent_wi" {
  service_account_id = google_service_account.devteam_agent.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "serviceAccount:${var.project_id}.svc.id.goog[${var.namespace}/kubeagents-devteam-agent]"
}

# ==============================================================================
# 🤖 Google Workspace Add-ons Service Identity
# ==============================================================================
resource "google_project_service_identity" "gsuite_addons" {
  provider = google-beta
  project  = var.project_id
  service  = "gsuiteaddons.googleapis.com"
}

# ==============================================================================
# 🤖 Google Chat Pub/Sub Setup
# ==============================================================================
resource "google_pubsub_topic" "chat_events" {
  name    = var.gchat_topic_name
  project = var.project_id

  depends_on = [google_project_service.apis]
}

resource "google_pubsub_subscription" "chat_events_sub" {
  name                 = var.gchat_subscription_name
  topic                = google_pubsub_topic.chat_events.name
  project              = var.project_id
  ack_deadline_seconds = 60

  depends_on = [google_project_service.apis]
}

# Grant GChat systems publisher role on the topic
resource "google_pubsub_topic_iam_member" "chat_api_publisher" {
  topic   = google_pubsub_topic.chat_events.name
  project = var.project_id
  role    = "roles/pubsub.publisher"
  member  = "serviceAccount:chat-api-push@system.gserviceaccount.com"
}

resource "google_pubsub_topic_iam_member" "gsuite_addons_publisher" {
  topic   = google_pubsub_topic.chat_events.name
  project = var.project_id
  role    = "roles/pubsub.publisher"
  member  = "serviceAccount:service-${data.google_project.project.number}@gcp-sa-gsuiteaddons.iam.gserviceaccount.com"

  depends_on = [google_project_service_identity.gsuite_addons]
}

# Grant Platform Agent GSA subscription subscriber and viewer roles
resource "google_pubsub_subscription_iam_member" "agent_subscriber" {
  subscription = google_pubsub_subscription.chat_events_sub.name
  project      = var.project_id
  role         = "roles/pubsub.subscriber"
  member       = "serviceAccount:${google_service_account.platform_agent.email}"
}

resource "google_pubsub_subscription_iam_member" "agent_viewer" {
  subscription = google_pubsub_subscription.chat_events_sub.name
  project      = var.project_id
  role         = "roles/pubsub.viewer"
  member       = "serviceAccount:${google_service_account.platform_agent.email}"
}

# ==============================================================================
# 🤖 GitHub Token Minter Integration (Optional)
# ==============================================================================

resource "google_service_account" "github_minter" {
  count        = var.github_org != "" ? 1 : 0
  account_id   = var.github_minter_gsa_name
  display_name = "GitHub Token Minter GSA"
  project      = var.project_id
}

resource "google_service_account_iam_member" "github_minter_wi" {
  count              = var.github_org != "" ? 1 : 0
  service_account_id = google_service_account.github_minter[0].name
  role               = "roles/iam.workloadIdentityUser"
  member             = "serviceAccount:${var.project_id}.svc.id.goog[${var.namespace}/kubeagents-github-minter]"
}

resource "google_kms_key_ring" "github_minter" {
  count    = var.github_org != "" ? 1 : 0
  name     = var.kms_keyring_name
  location = var.region
  project  = var.project_id

  depends_on = [google_project_service.apis]
}

resource "google_kms_crypto_key" "github_minter" {
  count    = var.github_org != "" ? 1 : 0
  name     = var.kms_key_name
  key_ring = google_kms_key_ring.github_minter[0].id
  purpose  = "ASYMMETRIC_SIGNING"

  version_template {
    algorithm        = "RSA_SIGN_PKCS1_2048_SHA256"
    protection_level = "SOFTWARE"
  }

  import_only = true
}

resource "google_kms_crypto_key_iam_member" "github_minter_signer" {
  count         = var.github_org != "" ? 1 : 0
  crypto_key_id = google_kms_crypto_key.github_minter[0].id
  role          = "roles/cloudkms.signerVerifier"
  member        = "serviceAccount:${google_service_account.github_minter[0].email}"
}

# ==============================================================================
# 🤖 Outputs
# ==============================================================================

output "github_minter_gsa_email" {
  value       = var.github_org != "" ? google_service_account.github_minter[0].email : ""
  description = "The email of the GitHub Token Minter GSA."
}

output "kms_keyring" {
  value       = var.github_org != "" ? google_kms_key_ring.github_minter[0].name : ""
  description = "The name of the KMS Keyring."
}

output "kms_key" {
  value       = var.github_org != "" ? google_kms_crypto_key.github_minter[0].name : ""
  description = "The name of the KMS Key."
}
