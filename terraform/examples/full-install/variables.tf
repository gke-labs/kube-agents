variable "project_id" {
  description = "GCP Project ID everything is provisioned in"
  type        = string
}

variable "cluster_name" {
  description = "Name of the GKE cluster to create (or, with create_cluster = false, the existing cluster to install onto)"
  type        = string
}

variable "cluster_mode" {
  description = "Cluster shape: \"autopilot\" (default) or \"standard\". Standard builds an e2-standard-4 default pool with Dataplane V2, FQDN NetworkPolicy, and the Filestore CSI and BackupRestore addons, and is the only mode that can carry a gVisor node pool."
  type        = string
  default     = "autopilot"

  validation {
    condition     = contains(["autopilot", "standard"], var.cluster_mode)
    error_message = "cluster_mode must be \"autopilot\" or \"standard\"."
  }
}

variable "create_cluster" {
  description = "Whether to create the cluster. Set false to install onto an existing cluster: the gke-cluster module then only reads it, creates no KMS resources, and enabling CMEK on it stays a gcloud step outside Terraform. The existing cluster must already have Workload Identity enabled."
  type        = bool
  default     = true
}

variable "location" {
  description = "GCP location for the cluster (and the KMS key ring when the GitHub minter is enabled): a region, or a zone for a zonal Standard or pre-existing cluster. Autopilot clusters are regional, so a zone is rejected by the gke-cluster module in autopilot mode."
  type        = string
}

variable "enable_gvisor_node_pool" {
  description = "Whether to add the dedicated GKE Sandbox (gVisor) node pool. Standard mode only; fails the plan on Autopilot, which provides the gvisor RuntimeClass natively."
  type        = bool
  default     = false
}

variable "gvisor_pool_name" {
  description = "Name of the gVisor node pool."
  type        = string
  default     = "gvisor-pool"
}

variable "agent_runtime_class" {
  description = "RuntimeClass for the agent pod, overriding what enable_gvisor_node_pool implies. Autopilot ships the gvisor RuntimeClass with no node pool to manage — and enable_gvisor_node_pool fails the plan there — so \"gvisor\" here is how an Autopilot install asks for the sandbox without reaching for extra_helm_values. Empty derives the value from enable_gvisor_node_pool."
  type        = string
  default     = ""
}

variable "deletion_protection" {
  description = "Whether deletion protection is enabled on the cluster. Passed through to the gke-cluster module; must be false before `terraform destroy` can remove the cluster."
  type        = bool
  default     = true
}

variable "allow_external_dns_traffic" {
  description = "Whether the cluster's DNS-based control plane endpoint serves traffic from outside the VPC. Passed through to the gke-cluster module, and false by default there so that applying an existing root does not publish an endpoint on a cluster that has none; set it true for a cluster the Platform Agent must reach from outside the VPC."
  type        = bool
  default     = false
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
  description = "Which GCP IAM role bundle the agent's service account gets: read-only, gke-admin, or custom (custom requires project_roles). Ignored when project_roles is set explicitly."
  type        = string
  default     = "read-only"

  validation {
    condition     = contains(["read-only", "gke-admin", "custom"], var.permission_set)
    error_message = "permission_set must be one of read-only, gke-admin, or custom."
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

variable "image_registry" {
  description = "Registry prefix for the images built from this project (operator, agent, credential proxy). Empty pulls the public ghcr.io images. Set this for a cluster that may only pull from an approved registry, after copying the images there with `make mirror-images MIRROR_PREFIX=<prefix> IMAGE_TAG=<tag>` from the repository root — the prefix here must be the same one, and that IMAGE_TAG must be the image_tag set below, since the mirror only holds the tag it was told to copy. A mirror the nodes' own credentials cannot read (an Artifact Registry in this project can be) also needs image_pull_secrets."
  type        = string
  default     = ""
}

variable "image_pull_secrets" {
  description = "Names of docker-registry Secrets in the kube-agents namespace holding credentials for image_registry, for a mirror the nodes cannot read on their own (Harbor, Artifactory). They are referenced, never created: this composition would otherwise hold registry credentials in Terraform state. Create them before `terraform apply` — and create the namespace first, since Helm has not made it yet: `kubectl create namespace <namespace>` then `kubectl create secret docker-registry <name> -n <namespace> --docker-server=... --docker-username=... --docker-password=...`, both idempotent against what Helm then finds. Does not reach helm_release.cert_manager, on the same terms as image_registry: a cluster whose registry needs authenticating to wants enable_cert_manager = false and cert-manager installed by hand."
  type        = list(string)
  default     = []

  # A blank entry renders `- name: ""` into four pod specs. The API server
  # accepts it — core PodSpec validation only rejects a name that differs from
  # its own trimmed form — and the kubelet then looks for a Secret named "",
  # fails, and pulls anonymously. That surfaces as ImagePullBackOff, several
  # layers from the tfvars typo. The operator's webhook rejects the same thing
  # on a hand-written PlatformAgent.
  validation {
    condition     = alltrue([for s in var.image_pull_secrets : trimspace(s) != ""])
    error_message = "Every image_pull_secrets entry must name a Secret."
  }
}

variable "third_party_image_registry" {
  description = "Registry prefix for the images this project does not build (LiteLLM, fluent-bit). Defaults to image_registry; set it only when the mirror keeps third-party images under a different path."
  type        = string
  default     = ""
}

variable "model_provider" {
  description = "Model provider the LiteLLM gateway routes model-default to (gemini, anthropic, openai, or vertex_ai). Set the matching *_api_key variable; vertex_ai takes no key and authenticates with Workload Identity instead."
  type        = string
  default     = "gemini"

  validation {
    condition     = contains(["gemini", "anthropic", "openai", "vertex_ai"], var.model_provider)
    error_message = "model_provider must be one of gemini, anthropic, openai, or vertex_ai."
  }
}

variable "vertex_project_id" {
  description = "Project serving the Vertex AI models when model_provider = \"vertex_ai\". Empty uses project_id. The gateway's service account is granted roles/aiplatform.user here, which works cross-project."
  type        = string
  default     = ""
}

variable "vertex_location" {
  description = "Vertex AI serving location when model_provider = \"vertex_ai\" (e.g. us-east4). Empty uses \"global\", which serves the first-party Gemini models from wherever has capacity. Set a region when you have a data-residency requirement, or when the model is a Model Garden partner model served only from specific regions."
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

variable "session_kv_api_key" {
  description = "Existing SESSION_KV_API_KEY to keep, for adopting a cluster whose Secret already holds one. Empty generates a fresh value (the right choice for a new install)."
  type        = string
  sensitive   = true
  default     = ""
}

variable "session_kv_salt" {
  description = "Existing SESSION_KV_SALT to keep, for adopting a cluster whose Secret already holds one. Rotating the salt re-anonymises every chat user, so an adoption must pass the live value; empty generates a fresh one."
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

variable "chat_topic_name" {
  description = "Pub/Sub topic for Google Chat events. The default matches the chat-pubsub module and the chart."
  type        = string
  default     = "platform-agent-chat-events"
}

variable "chat_subscription_name" {
  description = "Pub/Sub subscription for Google Chat events."
  type        = string
  default     = "platform-agent-chat-events-sub"
}

variable "hermes_dashboard_enabled" {
  description = "Whether the Hermes Web UI dashboard is enabled on the agent. null leaves the field out of the CR so the CRD default (true) applies."
  type        = bool
  default     = null
}

variable "memory_enabled" {
  description = "Whether agent memory persistence is enabled. null defers to the CRD default (false)."
  type        = bool
  default     = null
}

variable "memory_provider" {
  description = "Agent memory provider (multiuser_memory, kube_agents_memory, hindsight, none, ...). Empty defers to the CRD default. Selecting a hindsight-backed provider makes the chart render the Hindsight store automatically."
  type        = string
  default     = ""
}

variable "user_profile_enabled" {
  description = "Whether per-user profiles are enabled in agent memory. null defers to the CRD default (false)."
  type        = bool
  default     = null
}

variable "github_repo" {
  description = "Target GitOps repository for the agent's GitHub integration (owner/repo or URL). Empty leaves the GitHub integration unconfigured. Independent of enable_github_minter, which only provisions the minter's GCP identity."
  type        = string
  default     = ""
}

variable "enable_github_minter" {
  description = "Provision the GitHub token minter: its GCP resources (service account, KMS key ring and signing key) and, through the chart, its Kubernetes workload. Requires github_repo in owner/repo (or github.com URL) form. The App private key must be imported into the KMS key before the minter goes Ready."
  type        = bool
  default     = false
}

variable "github_minter_kms_keyring" {
  description = "Cloud KMS key ring holding the GitHub minter's signing key."
  type        = string
  default     = "github-token-minter-keyring"
}

variable "github_minter_kms_key" {
  description = "Cloud KMS asymmetric signing key the minter signs GitHub App JWTs with. The App private key is imported into it outside Terraform."
  type        = string
  default     = "github-token-minter-key"
}

variable "github_app_id" {
  description = "GitHub App ID the minter signs as. Set, the chart creates the github-app-credentials Secret; empty, that Secret must already exist in the release namespace before the minter pod can start."
  type        = string
  default     = ""
}

variable "enable_backup_agent" {
  description = "Enable the Backup for GKE agent on the cluster (the BackupRestore addon). It costs nothing until a BackupPlan targets the cluster, but it must be on before enable_gke_backup_plan can work."
  type        = bool
  default     = true
}

variable "enable_gke_backup_plan" {
  description = "Create a scheduled BackupPlan for the release namespace (opt-in). Backups include Secrets and volume data and are billed per backed-up pod and per GB of snapshot storage."
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
  description = "Install cert-manager, which issues the serving certificate for the operator's admission webhooks. Set to false when the target cluster already runs cert-manager: Terraform does not detect an existing install and the apply fails on the existing CRDs (install.sh probes for one on the existing-cluster path and sets this for you). Turning this off with enable_webhooks left on leaves the webhooks without a certificate."
  type        = bool
  default     = true
}

variable "cert_manager_version" {
  description = "cert-manager chart version. Values below 1.15.x need the crds.enabled key in main.tf renamed back to installCRDs."
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
