variable "project_id" {
  description = "GCP Project ID"
  type        = string
}

variable "topic_name" {
  description = "Pub/Sub topic Google Chat publishes inbound events to"
  type        = string
  default     = "platform-agent-chat-events"
}

variable "subscription_name" {
  description = "Pub/Sub subscription the Platform Agent pulls chat events from"
  type        = string
  default     = "platform-agent-chat-events-sub"
}

variable "agent_service_account_email" {
  description = "Email of the Platform Agent's Google Service Account (granted subscriber and viewer on the subscription, or roles/iam.serviceAccountTokenCreator on the Chat service account when chat_service_account_id is set)"
  type        = string

  validation {
    condition     = can(regex(".+@.+\\.iam\\.gserviceaccount\\.com$", var.agent_service_account_email))
    error_message = "agent_service_account_email must be a service account email (name@project.iam.gserviceaccount.com)."
  }
}

variable "chat_service_account_id" {
  description = "Account ID (the part before @) of a dedicated Google Chat service account created in project_id when Google Chat lives in a different GCP project or organization from the Platform Agent. When non-empty, the module creates that service account, grants agent_service_account_email roles/iam.serviceAccountTokenCreator on it as a resource, and binds roles/pubsub.subscriber and roles/pubsub.viewer to it instead of agent_service_account_email. Empty (default) binds the subscription directly to agent_service_account_email."
  type        = string
  default     = ""
  nullable    = false

  validation {
    condition     = var.chat_service_account_id == "" || can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]$", var.chat_service_account_id))
    error_message = "chat_service_account_id must be empty or 6–30 lowercase letters, digits, or hyphens, starting with a letter and ending with a letter or digit."
  }
}

variable "manage" {
  description = "Create the Pub/Sub topic, subscription, service identities, optional Chat service account, and IAM bindings in project_id. Set false when the installing identity cannot administer project_id and a project admin has provisioned them out of band; the module then creates nothing and outputs the configured names and derived service account email."
  type        = bool
  default     = true
  nullable    = false
}
