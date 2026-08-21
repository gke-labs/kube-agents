variable "infra_provider" {
  type        = string
  default     = "gcp"
  description = "Infrastructure provider (gcp or kind)"
}

variable "cluster_name" {
  type        = string
  default     = "bench-cluster"
  description = "Unique name of the cluster"
}

variable "location" {
  type        = string
  default     = "us-central1-a"
  description = "GCP location or local"
}

variable "node_count" {
  type        = number
  default     = 2
  description = "Number of nodes"
}

variable "machine_type" {
  type        = string
  default     = "e2-standard-4"
  description = "Machine type for nodes"
}

variable "project_id" {
  type        = string
  default     = ""
  description = "GCP Project ID"
}

variable "kubeconfig_path" {
  type        = string
  default     = ""
  description = "Path to write kubeconfig for local/kind"
}

variable "target_deployment_name" {
  type        = string
  default     = "my-service-app"
  description = "Target deployment name for the task"
}
