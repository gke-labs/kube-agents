locals {
  base_apis = [
    "container.googleapis.com",
    "cloudkms.googleapis.com",
    "iam.googleapis.com",
    "cloudresourcemanager.googleapis.com",
    "monitoring.googleapis.com",
    "logging.googleapis.com",
  ]
  chat_apis = var.enable_google_chat ? [
    "pubsub.googleapis.com",
    "chat.googleapis.com",
    "gsuiteaddons.googleapis.com",
  ] : []

  required_apis = toset(concat(local.base_apis, local.chat_apis))

  # Only non-empty credential keys end up in the Secret, so an unset optional
  # provider key does not create an empty entry.
  credentials = {
    for key, value in {
      API_SERVER_KEY = var.api_server_key
      # Generated rather than asked for: neither value means anything to an
      # operator, and both are scoped to the agent pod. Held in Terraform state
      # rather than left to the chart's own generation so that `terraform apply`
      # is idempotent without needing a cluster read — rotating the salt would
      # re-anonymise every user, breaking the link between their past sessions
      # and their future ones.
      SESSION_KV_API_KEY = random_password.session_kv_api_key.result
      SESSION_KV_SALT    = random_password.session_kv_salt.result
      ANTHROPIC_API_KEY  = var.anthropic_api_key
      GEMINI_API_KEY     = var.gemini_api_key
      OPENAI_API_KEY     = var.openai_api_key
    } : key => value if value != ""
  }
}

# Bearer token for the pod-local Session KV server on 127.0.0.1:8699. Both the
# sandbox container (which serves and calls it) and the credential-proxy
# container (whose event watcher posts to it) read this one value.
resource "random_password" "session_kv_api_key" {
  length  = 48
  special = false
}

# HMAC salt for pseudonymising chat identities before they reach session
# metadata, audit logs, or OTel spans.
resource "random_password" "session_kv_salt" {
  length  = 48
  special = false
}

resource "google_project_service" "required" {
  for_each = local.required_apis

  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}

module "gke_cluster" {
  source = "../../modules/gke-cluster"

  project_id                 = var.project_id
  cluster_name               = var.cluster_name
  location                   = var.location
  deletion_protection        = var.deletion_protection
  release_channel            = var.release_channel
  enable_database_encryption = var.enable_database_encryption
  kms_keyring_name           = var.kms_keyring_name
  kms_key_name               = var.kms_key_name

  depends_on = [google_project_service.required]
}

module "kube_agents_iam" {
  source = "../../modules/kube-agents-iam"

  project_id    = var.project_id
  namespace     = var.namespace
  project_roles = var.project_roles

  depends_on = [google_project_service.required]
}

module "chat_pubsub" {
  source = "../../modules/chat-pubsub"
  count  = var.enable_google_chat ? 1 : 0

  project_id                  = var.project_id
  agent_service_account_email = module.kube_agents_iam.service_account_email

  depends_on = [google_project_service.required]
}

module "github_minter" {
  source = "../../modules/github-minter"
  count  = var.enable_github_minter ? 1 : 0

  project_id = var.project_id
  location   = var.location
  namespace  = var.namespace

  depends_on = [google_project_service.required]
}

resource "helm_release" "kube_agents" {
  name             = "kube-agents"
  chart            = "${path.module}/../../../charts/kube-agents"
  namespace        = var.namespace
  create_namespace = true

  values = [yamlencode({
    operator = {
      image = {
        tag = var.image_tag
      }
    }
    litellm = {
      modelProvider    = var.model_provider
      modelDefaultName = var.model_default_name
    }
    platformAgent = {
      harness = {
        clusterName = module.gke_cluster.cluster_name
        location    = module.gke_cluster.cluster_location
        projectId   = var.project_id
      }
      deployment = {
        image = {
          tag = var.image_tag
        }
      }
      security = {
        # With annotations set, the OPERATOR creates and manages the KSA (see
        # the chart README's ServiceAccount-ownership section); this one wires
        # Workload Identity to the GSA the kube-agents-iam module created.
        serviceAccountAnnotations = {
          "iam.gke.io/gcp-service-account" = module.kube_agents_iam.service_account_email
        }
      }
      credentials = {
        create = true
        data   = local.credentials
      }
      integration = merge(
        var.enable_google_chat ? {
          googleChat = {
            enabled          = true
            topicName        = module.chat_pubsub[0].topic_name
            subscriptionName = module.chat_pubsub[0].subscription_name
            allowedUsers     = var.google_chat_allowed_users
          }
        } : {},
        var.github_repo != "" ? {
          github = { gitRepo = var.github_repo }
        } : {}
      )
    }
  })]

  depends_on = [module.gke_cluster]
}
