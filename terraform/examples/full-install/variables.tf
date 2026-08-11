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

variable "project_roles" {
  description = "Project-level IAM roles granted to the agent's service account. Leave null to use the kube-agents-iam module's default read-only permission set; set to [] to grant nothing and manage roles externally."
  type        = list(string)
  default     = null
}

variable "image_tag" {
  description = "Image tag for both the operator and the platform agent. Required because a checkout's Chart.yaml carries an appVersion placeholder that never matches a published image tag, so the chart's tag defaulting cannot work from a checkout. `latest` is fine for evaluation; set a `vX.Y.Z` release tag for production."
  type        = string
  default     = "latest"
}

variable "model_provider" {
  description = "Model provider the LiteLLM gateway routes model-default to (gemini, anthropic, or openai — chatgpt needs the kustomize overlay and is rejected by the chart). Set the matching *_api_key variable."
  type        = string
  default     = "gemini"

  validation {
    condition     = contains(["gemini", "anthropic", "openai"], var.model_provider)
    error_message = "model_provider must be one of gemini, anthropic, or openai."
  }
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
