terraform {
  required_version = "~> 1.5"
  # 6.11 is the floor for control_plane_endpoints_config on
  # google_container_cluster, which main.tf sets; 5.x has no such block.
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 6.11, < 8.0"
    }
    google-beta = {
      source  = "hashicorp/google-beta"
      version = ">= 6.11, < 8.0"
    }
  }
}
