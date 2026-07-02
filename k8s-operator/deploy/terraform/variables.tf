variable "project_id" {
  type        = string
  description = "The target GCP Project ID."
  default     = ""
}

variable "region" {
  type        = string
  description = "The GCP Region for GKE cluster."
  default     = "us-east4"
}

variable "cluster_name" {
  type        = string
  description = "The name of the dedicated GKE cluster to create."
  default     = "kube-agents-dedicated-cluster"
}

variable "namespace" {
  type        = string
  description = "The Kubernetes namespace for kube-agents components."
  default     = "kubeagents-system"
}

variable "model_provider" {
  type        = string
  description = "Model Provider: gemini, anthropic, openai, or chatgpt."
  default     = "gemini"
}

variable "model_default_name" {
  type        = string
  description = "The default model name to use."
  default     = "gemini-3.5-flash"
}

variable "gemini_api_key" {
  type        = string
  description = "API Key for Gemini."
  sensitive   = true
  default     = "placeholder"
}

variable "openai_api_key" {
  type        = string
  description = "API Key for OpenAI."
  sensitive   = true
  default     = "placeholder"
}

variable "anthropic_api_key" {
  type        = string
  description = "API Key for Anthropic."
  sensitive   = true
  default     = "placeholder"
}

variable "allowed_users" {
  type        = string
  description = "Comma-separated list of allowed Google Chat users. Leave blank for all."
  default     = ""
}

variable "gchat_topic_name" {
  type        = string
  description = "GCP Pub/Sub Topic name for Google Chat integration."
  default     = "platform-agent-chat-events"
}

variable "gchat_subscription_name" {
  type        = string
  description = "GCP Pub/Sub Subscription name for Google Chat integration."
  default     = "platform-agent-chat-events-sub"
}

variable "operator_image_tag" {
  type        = string
  description = "Image tag for the operator controller manager."
  default     = "latest"
}

variable "platform_agent_image_tag" {
  type        = string
  description = "Image tag for the Platform Agent."
  default     = "latest"
}

variable "controller_gsa_name" {
  type        = string
  description = "The name of the Google Service Account for the Controller."
  default     = "kubeagents-controller-gsa"
}

variable "platform_gsa_name" {
  type        = string
  description = "The name of the Google Service Account for the Platform Agent."
  default     = "kubeagents-platform-gsa"
}

variable "operator_gsa_name" {
  type        = string
  description = "The name of the Google Service Account for the Operator Agent."
  default     = "kubeagents-operator-gsa"
}

variable "devteam_gsa_name" {
  type        = string
  description = "The name of the Google Service Account for the DevTeam Agent."
  default     = "kubeagents-devteam-gsa"
}

variable "deletion_protection" {
  type        = bool
  description = "Whether to enable deletion protection on the GKE cluster."
  default     = true
}

variable "github_minter_gsa_name" {
  type        = string
  description = "The name of the Google Service Account for the GitHub Token Minter."
  default     = "kubeagents-github-minter-gsa"
}

variable "github_org" {
  type        = string
  description = "The GitHub organization/owner name for the Token Minter integration."
  default     = ""
}

variable "github_repo" {
  type        = string
  description = "The GitHub repository name for the Token Minter integration."
  default     = ""
}

variable "github_app_id" {
  type        = string
  description = "The GitHub App ID for the Token Minter integration."
  default     = ""
}

variable "kms_keyring_name" {
  type        = string
  description = "The name of the KMS Keyring for secure token minting."
  default     = "github-token-minter-keyring"
}

variable "kms_key_name" {
  type        = string
  description = "The name of the KMS cryptographic key for secure token minting."
  default     = "github-token-minter-key"
}


