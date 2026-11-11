# Copyright 2026 The Kubernetes Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# The sized test fleet (docs/designs/sized-test-fleet.md): the clusters and their
# one node pool each, for one size of bench/scale/fleet-sizes.yaml. The workloads
# are not here: hack/sized_fleet.py applies them with kubectl, because they number
# in the thousands. Unlike the seeded fleet this stack does not stand; a run
# applies it and destroys it.
#
# The label is neither managed-by=kube-agents-bench, which the eval orphan sweep
# deletes clusters by, nor managed-by=kube-agents-seeded-fleet, which the seeded
# fleet's tooling reads: no other tool may match a sized cluster.

locals {
  sizes  = yamldecode(file("${path.module}/../../scale/fleet-sizes.yaml"))
  factor = local.sizes.factors[var.size]
  shape  = local.sizes.shape

  clusters = ceil(local.sizes.base.clusters * local.factor)
  nodes    = ceil(local.sizes.base.nodes * local.factor)

  # The nodes divide as evenly as they go: the first `remainder` clusters take one
  # more (M: 32, 32 and six of 31).
  per_cluster = floor(local.nodes / local.clusters)
  remainder   = local.nodes - local.per_cluster * local.clusters

  size_label = lower(var.size)
  fleet = {
    for i in range(local.clusters) : format("sized-%s-%02d", local.size_label, i + 1) => {
      index = i
      nodes = local.per_cluster + (i < local.remainder ? 1 : 0)
    }
  }

  labels = {
    "managed-by"             = "kube-agents-sized-fleet"
    "kube-agents-scale-size" = local.size_label
    "kube-agents-scale-run"  = var.run_id
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
  zone    = var.zone
}

# A network of its own, so the fleet's ranges never meet the project's other
# clusters and a destroy removes every address it took.
resource "google_compute_network" "fleet" {
  name                    = "sized-fleet-${local.size_label}"
  auto_create_subnetworks = false
}

# One subnet a cluster. Up to 16 clusters (L):
#   nodes     10.100.<i>.0/24   (at most 32 nodes a cluster at L)
#   Pods      10.<128+i>.0.0/16 (32 nodes x /26 a node fits with room)
#   Services  10.200.<16i>.0/20
#   control plane 172.16.0.<16i>/28, the private endpoint's peering range
resource "google_compute_subnetwork" "fleet" {
  for_each = local.fleet

  name                     = each.key
  network                  = google_compute_network.fleet.id
  region                   = var.region
  ip_cidr_range            = "10.100.${each.value.index}.0/24"
  private_ip_google_access = true

  secondary_ip_range {
    range_name    = "pods"
    ip_cidr_range = "10.${128 + each.value.index}.0.0/16"
  }
  secondary_ip_range {
    range_name    = "services"
    ip_cidr_range = "10.200.${each.value.index * 16}.0/20"
  }
}

# The nodes have no external address, so L's 500 nodes take none of the
# project's address quota; NAT gives them the way out to pull the image.
resource "google_compute_router" "fleet" {
  name    = "sized-fleet-${local.size_label}"
  network = google_compute_network.fleet.id
  region  = var.region
}

resource "google_compute_router_nat" "fleet" {
  name                               = "sized-fleet-${local.size_label}"
  router                             = google_compute_router.fleet.name
  region                             = var.region
  nat_ip_allocate_option             = "AUTO_ONLY"
  source_subnetwork_ip_ranges_to_nat = "ALL_SUBNETWORKS_ALL_IP_RANGES"
}

resource "google_container_cluster" "fleet" {
  for_each = local.fleet

  name     = each.key
  location = var.zone

  network    = google_compute_network.fleet.id
  subnetwork = google_compute_subnetwork.fleet[each.key].id

  # The one node pool is the resource below, so the count of node pools is the
  # count of clusters, and no default pool adds nodes outside the size.
  remove_default_node_pool = true
  initial_node_count       = 1
  resource_labels          = local.labels
  deletion_protection      = false

  ip_allocation_policy {
    cluster_secondary_range_name  = "pods"
    services_secondary_range_name = "services"
  }

  private_cluster_config {
    enable_private_nodes    = true
    enable_private_endpoint = false
    master_ipv4_cidr_block  = "172.16.0.${each.value.index * 16}/28"
  }

  logging_config {
    enable_components = ["SYSTEM_COMPONENTS", "WORKLOADS"]
  }

  release_channel {
    channel = "REGULAR"
  }

  # No upgrade during a run: a node pool upgrade replaces nodes under the measurement, and the
  # cost record prices the nodes that exist when it is taken. The window is set apart from the
  # 00:00 UTC run; the exclusion covers the run whatever the window.
  maintenance_policy {
    daily_maintenance_window {
      start_time = "12:00"
    }
    maintenance_exclusion {
      exclusion_name = "sized-fleet-run"
      start_time     = timestamp()
      end_time       = timeadd(timestamp(), "24h")
      exclusion_options {
        scope = "NO_UPGRADES"
      }
    }
  }

  lifecycle {
    # timestamp() moves on every plan; the exclusion is set once, when the cluster is created.
    ignore_changes = [maintenance_policy]
  }

  workload_identity_config {
    workload_pool = "${var.project_id}.svc.id.goog"
  }

  depends_on = [google_compute_router_nat.fleet]
}

resource "google_container_node_pool" "fleet" {
  for_each = local.fleet

  name              = "pool"
  location          = var.zone
  cluster           = google_container_cluster.fleet[each.key].name
  node_count        = each.value.nodes
  max_pods_per_node = local.shape.max_pods_per_node

  management {
    auto_repair  = true
    auto_upgrade = true
  }

  node_config {
    machine_type    = local.shape.machine_type
    disk_size_gb    = local.shape.disk_size_gb
    disk_type       = local.shape.disk_type
    resource_labels = local.labels
    labels          = { "kube-agents-scale-size" = local.size_label }
    service_account = var.node_service_account
    oauth_scopes    = ["https://www.googleapis.com/auth/cloud-platform"]

    workload_metadata_config {
      mode = "GKE_METADATA"
    }
  }

  # A pool of 32 nodes comes up in a few minutes; the default 30 is generous.
  timeouts {
    create = "40m"
    delete = "40m"
  }
}
