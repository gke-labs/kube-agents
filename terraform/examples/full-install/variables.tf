variable "project_id" {
  description = "GCP Project ID everything is provisioned in"
  type        = string
}

variable "cluster_name" {
  description = "Name of the GKE Autopilot cluster to create"
  type        = string
}

variable "location" {
  description = "GCP region for the cluster (and the KMS key ring when the GitHub minter is enabled). Autopilot clusters are regional, so a zone is rejected by the gke-cluster module."
  type        = string
}

variable "deletion_protection" {
  description = "Whether deletion protection is enabled on the cluster. Passed through to the gke-cluster module; must be false before `terraform destroy` can remove the cluster."
  type        = bool
  default     = true
}

variable "release_channel" {
  description = "GKE release channel for the cluster (RAPID, REGULAR, or STABLE; the gke-cluster module rejects EXTENDED, which its Autopilot clusters do not support)"
  type        = string
  default     = "REGULAR"
}

variable "enable_database_encryption" {
  description = "Whether to enable Cloud KMS database encryption for GKE etcd secrets (CMEK)"
  type        = bool
  default     = true
}

variable "kms_keyring_name" {
  description = "Name of the Cloud KMS Keyring for GKE database encryption"
  type        = string
  default     = "platform-agent-keyring"
}

variable "kms_key_name" {
  description = "Name of the Cloud KMS CryptoKey for GKE database encryption"
  type        = string
  default     = "k8s-secret-encryption-key"
}

variable "namespace" {
  description = "Kubernetes namespace the kube-agents release is installed into and the Workload Identity binding targets. Leave at the default: the agent's model-gateway endpoint is hard-wired to kubeagents-system (see the chart's values.yaml), so a release in any other namespace leaves the agent unable to reach the gateway."
  type        = string
  default     = "kubeagents-system"
}

variable "permission_set" {
  description = "Which of provision_04_gcp_iam.sh's role bundles the agent's service account gets: read-only, gke-admin, or custom (custom requires project_roles). Ignored when project_roles is set explicitly."
  type        = string
  default     = "read-only"

  validation {
    condition     = contains(["read-only", "gke-admin", "custom"], var.permission_set)
    error_message = "permission_set must be one of read-only, gke-admin, or custom (the same values the provisioning scripts accept)."
  }
}

variable "project_roles" {
  description = "Project-level IAM roles granted to the agent's service account. Leave null to take the bundle permission_set names; set explicitly (including []) to manage the roles yourself, which overrides permission_set."
  type        = list(string)
  default     = null
}

variable "image_tag" {
  description = "Image tag for both the operator and the platform agent. Required because a checkout's Chart.yaml carries an appVersion placeholder that never matches a published image tag, so the chart's tag defaulting cannot work from a checkout. `latest` is fine for evaluation; set an `X.Y.Z` release tag for production."
  type        = string
  default     = "latest"
}

variable "model_provider" {
  description = "Model provider the LiteLLM gateway routes model-default to (gemini, anthropic, openai, or vertex_ai — chatgpt needs the kustomize overlay and is rejected by the chart). Set the matching *_api_key variable; vertex_ai takes no key and authenticates with Workload Identity instead."
  type        = string
  default     = "gemini"

  validation {
    condition     = contains(["gemini", "anthropic", "openai", "vertex_ai"], var.model_provider)
    error_message = "model_provider must be one of gemini, anthropic, openai, or vertex_ai."
  }
}

variable "vertex_project_id" {
  description = "Project serving the Vertex AI models when model_provider = \"vertex\". Empty uses project_id. The gateway's service account is granted roles/aiplatform.user here, which works cross-project."
  type        = string
  default     = ""
}

variable "vertex_location" {
  description = "Vertex AI serving location when model_provider = \"vertex\" (e.g. us-east4). Empty uses the cluster location — override when the model is not served in the cluster's region."
  type        = string
  default     = ""
}

variable "model_default_name" {
  description = "Model name behind model-default. Empty selects the chart's per-provider default (which mirrors the provisioning scripts)."
  type        = string
  default     = ""
}

variable "api_server_key" {
  description = "API_SERVER_KEY for the agent harness (required; stored in the platform-agent-secrets Secret)"
  type        = string
  sensitive   = true

  validation {
    # An empty string would be silently dropped from the credentials Secret
    # (see local.credentials) and only fail at agent runtime.
    condition     = length(var.api_server_key) > 0
    error_message = "api_server_key must be non-empty — without it the platform-agent Secret lacks API_SERVER_KEY and the agent pod cannot start."
  }
}

variable "anthropic_api_key" {
  description = "ANTHROPIC_API_KEY model-provider credential (optional; omitted from the Secret when empty)"
  type        = string
  sensitive   = true
  default     = ""
}

variable "gemini_api_key" {
  description = "GEMINI_API_KEY model-provider credential (optional; omitted from the Secret when empty)"
  type        = string
  sensitive   = true
  default     = ""
}

variable "openai_api_key" {
  description = "OPENAI_API_KEY model-provider credential (optional; omitted from the Secret when empty)"
  type        = string
  sensitive   = true
  default     = ""
}

variable "enable_google_chat" {
  description = "Provision the Google Chat backend (Pub/Sub topic and subscription, Chat APIs) and enable the CR's googleChat integration with the created topic/subscription."
  type        = bool
  default     = false
}

variable "google_chat_allowed_users" {
  description = "Google Chat users allowed to talk to the agent (empty list = all users allowed). Only used when enable_google_chat is true."
  type        = list(string)
  default     = []
}

variable "google_chat_home_channel" {
  description = "Google Chat space the agent posts unsolicited messages to (e.g. cron findings). Empty leaves it unset. Only used when enable_google_chat is true."
  type        = string
  default     = ""
}

variable "google_chat_mode" {
  description = "Google Chat output verbosity: 'default' (quiet) or 'debug' (surfaces tool progress, memory reviews, and approval cards). Mirrors GOOGLE_CHAT_MODE."
  type        = string
  default     = "default"

  validation {
    condition     = contains(["default", "debug"], var.google_chat_mode)
    error_message = "google_chat_mode must be 'default' or 'debug'."
  }
}

variable "enable_slack" {
  description = "Enable the agent's Slack integration. Slack needs no GCP resources — this only writes the bot/app tokens into the credentials Secret and turns on the CR's slack section. The Slack app itself (Socket Mode, bot scopes, workspace install) is a manual step; see INSTALL.md."
  type        = bool
  default     = false
}

variable "slack_bot_token" {
  description = "SLACK_BOT_TOKEN (xoxb-...) stored in the credentials Secret. Only used when enable_slack is true."
  type        = string
  sensitive   = true
  default     = ""
}

variable "slack_app_token" {
  description = "SLACK_APP_TOKEN (xapp-...) stored in the credentials Secret. Only used when enable_slack is true."
  type        = string
  sensitive   = true
  default     = ""
}

variable "slack_allowed_users" {
  description = "Slack users allowed to talk to the agent (empty list = all users allowed). Only used when enable_slack is true."
  type        = list(string)
  default     = []
}

variable "slack_home_channel" {
  description = "Slack channel ID the agent posts unsolicited messages to. Empty leaves it unset."
  type        = string
  default     = ""
}

variable "slack_home_channel_name" {
  description = "Human-readable name of the Slack home channel. Empty leaves it unset."
  type        = string
  default     = ""
}

variable "github_repo" {
  description = "Target GitOps repository for the agent's GitHub integration (owner/repo or URL). Empty leaves the GitHub integration unconfigured. Independent of enable_github_minter, which only provisions the minter's GCP identity."
  type        = string
  default     = ""
}

variable "enable_github_minter" {
  description = "Provision the GitHub token minter's GCP resources (service account, KMS key ring and signing key)"
  type        = bool
  default     = false
}

variable "enable_backup_agent" {
  description = "Enable the Backup for GKE agent on the cluster (the BackupRestore addon). True matches the cluster provision_01_gcp_cluster.sh creates; it costs nothing until a BackupPlan targets the cluster, but it must be on before enable_gke_backup_plan can work."
  type        = bool
  default     = true
}

variable "enable_gke_backup_plan" {
  description = "Create a scheduled BackupPlan for the release namespace (mirrors provision_12_gke_backup_plan.sh, which is likewise opt-in). Backups include Secrets and volume data and are billed per backed-up pod and per GB of snapshot storage."
  type        = bool
  default     = false
}

variable "backup_cron_schedule" {
  description = "Cron schedule for automatic backups (5 fields). Only used when enable_gke_backup_plan is true."
  type        = string
  default     = "0 2 * * *"
}

variable "backup_retain_days" {
  description = "How many days each backup is retained. Only used when enable_gke_backup_plan is true."
  type        = number
  default     = 30
}

variable "backup_encryption_key" {
  description = "Optional Cloud KMS CryptoKey path encrypting the backups (projects/P/locations/L/keyRings/R/cryptoKeys/K). Empty uses Google-managed encryption. A CMEK key cannot later be removed from an existing plan."
  type        = string
  default     = ""
}

variable "enable_cert_manager" {
  description = "Install cert-manager, which issues the serving certificate for the operator's admission webhooks (mirrors provision_03_gcp_gke_operator.sh). Set to false when the target cluster already runs cert-manager: unlike the script, Terraform does not detect an existing install and the apply fails on the existing CRDs. Turning this off with enable_webhooks left on leaves the webhooks without a certificate."
  type        = bool
  default     = true
}

variable "cert_manager_version" {
  description = "cert-manager chart version, pinned to the release provision_03_gcp_gke_operator.sh installs. Values below 1.15.x need the crds.enabled key in main.tf renamed back to installCRDs."
  type        = string
  default     = "v1.21.1"
}

variable "enable_webhooks" {
  description = "Enable the operator's PlatformAgent admission webhooks (defaulting, validation, delete protection). Requires cert-manager in the cluster — either enable_cert_manager or a pre-existing install."
  type        = bool
  default     = true
}

variable "extra_helm_values" {
  description = "Extra values for the kube-agents Helm release, covering chart settings this composition does not expose as its own variable (telemetry.otlpEndpoint, litellm.otel, the resource blocks, the PlatformAgent harness knobs). Passed as a second values document, so Helm deep-merges it key by key over the ones computed here and anything set wins. Setting a key the composition also computes — platformAgent.harness.clusterName, say — overrides it, which is rarely what you want."
  type        = any
  default     = {}
}
