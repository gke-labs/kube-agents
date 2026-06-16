variable "project_id" {
  type        = string
  description = "The GCP Project ID"

  validation {
    condition     = length(var.project_id) > 0
    error_message = "The project_id variable must not be empty. Please configure it in terraform.tfvars (e.g. project_id = \"your-gcp-project-id\") or set TF_VAR_project_id environment variable."
  }
}

variable "region" {
  type        = string
  description = "The region to deploy the cluster"
  default     = "us-east1"
}

variable "autopilot_clusters" {
  type = map(object({
    location      = string
    traffic_shape = string
    enable_ray    = optional(bool, false)
  }))
  description = "Map of GKE Autopilot clusters to create"
  default = {
    "wolfram-auto-01" = {
      location      = "us-east1"
      traffic_shape = "Steady"
    }
    "wolfram-auto-02" = {
      location      = "asia-east1"
      traffic_shape = "Spikes"
      enable_ray    = true
    }
  }
}

variable "standard_clusters" {
  type = map(object({
    location       = string
    node_locations = list(string)
    traffic_shape  = string
  }))
  description = "Map of GKE Standard clusters to create"
  default = {
    "wolfram-std-01" = {
      location       = "us-east1"
      node_locations = ["us-east1-b"]
      traffic_shape  = "Steady"
    }
  }
}
