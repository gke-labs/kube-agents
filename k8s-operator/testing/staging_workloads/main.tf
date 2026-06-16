terraform {
  required_version = ">= 1.0"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

resource "google_container_cluster" "primary" {
  for_each = var.autopilot_clusters

  name     = each.key
  location = each.value.location

  # Disable deletion protection for staging PoC
  deletion_protection = false

  # Enable Autopilot
  enable_autopilot = true

  # Enable Workload Identity
  workload_identity_config {
    workload_pool = "${var.project_id}.svc.id.goog"
  }

  release_channel {
    channel = "REGULAR"
  }

  ip_allocation_policy {}

  addons_config {
    ray_operator_config {
      enabled = each.value.enable_ray
      ray_cluster_logging_config {
        enabled = each.value.enable_ray
      }
      ray_cluster_monitoring_config {
        enabled = each.value.enable_ray
      }
    }
  }

  lifecycle {
    ignore_changes = [
      node_config,
    ]
  }
}

resource "google_container_cluster" "standard" {
  for_each = var.standard_clusters

  name     = each.key
  location = each.value.location

  # Disable deletion protection for staging PoC
  deletion_protection = false

  # GKE Standard settings
  remove_default_node_pool = true
  initial_node_count       = 1

  workload_identity_config {
    workload_pool = "${var.project_id}.svc.id.goog"
  }

  release_channel {
    channel = "REGULAR"
  }

  ip_allocation_policy {}
}

resource "google_container_node_pool" "standard_nodes" {
  for_each = var.standard_clusters

  name       = "std-node-pool"
  location   = each.value.location
  cluster    = google_container_cluster.standard[each.key].name
  
  # Set zonal locations dynamically per cluster config
  node_locations = each.value.node_locations

  # Enable cluster autoscaler to accommodate the resource demands of the workloads
  initial_node_count = 1
  autoscaling {
    min_node_count = 1
    max_node_count = 2
  }

  node_config {
    preemptible  = false
    machine_type = "e2-standard-4"

    oauth_scopes = [
      "https://www.googleapis.com/auth/cloud-platform"
    ]

    workload_metadata_config {
      mode = "GKE_METADATA"
    }
  }
}
