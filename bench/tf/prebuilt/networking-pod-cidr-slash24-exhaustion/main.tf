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

# The scenario driver for bench/tasks/networking-pod-cidr-slash24-exhaustion.
#
# It plants ONE exhausted Pod range: an eight-node zonal Standard cluster of
# its own on the project's default network, its Pod range a /21 that GKE
# picks and creates. /21 is the smallest range GKE takes by netmask (the API
# refuses a smaller one: "Cluster CIDR range is greater than maximum
# (24 > 21)"). At the default 110 Pods a node, GKE gives each node a /24
# block, and a /21 holds eight of them, the most nodes GKE's own sizing rule
# allows on it. The eight nodes take the whole range and GKE's own
# ipAllocationPolicy.defaultPodIpv4RangeUtilization reads 1.0: the cluster
# cannot add a node. That is the state the networking SOP's check 2.1
# (subnet-ip-exhaustion, under 15% available) flags from GKE's number, and it
# is planted for real because nothing short of a cluster produces that number.
# Apply about 5 to 8 minutes, destroy about 3 to 5.
#
# A cluster of its own rather than a pool on the seeded fleet: a full /24
# pool on a seeded cluster reds the nightly case
# upgrades-readiness-surge-capacity (a pool with no free per-node block cannot
# surge) and the fleet reconciler holds no subnet permission for a
# pool-created range.
#
# The default network rather than a VPC of its own: the lane's runner holds
# roles/container.admin and the service-account roles but no Compute network
# role (scripts/verify_ci_pool_project.py, PROW_RUNNER_ROLES), so it cannot
# create a network or a subnet; GKE creates and removes its own secondary
# ranges through its service agent. The host cluster and the seeded fleet sit
# on the same default network with GKE-chosen ranges and are not touched: the
# planted range is a new one, and GKE deletes it with the cluster.
#
# The node service account holds no project role. A node's /24 alias block
# is allocated when its VM is created, so the utilization reads 1.0 whether
# or not the kubelet ships logs; and a fixture edits no project IAM
# (bench/README.md, Validated Capabilities fixture rules 2 and 3).
#
# Names are derived from the run's cluster_name so two runs never collide on
# a leftover; the GKE-created range is named gke-<cluster>-pods-<hash>, which
# the case's objective asserts on through the cluster name prefix.

terraform {
  required_version = ">= 1.5.0"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 5.0.0"
    }
  }
}

locals {
  region = regex("^(.*)-[a-z]$", var.location)[0]

  # 23 characters: inside GKE's 40-character cluster name limit whatever the
  # runner's cluster_name is, and distinct from it.
  name = "${var.cluster_name_prefix}-${substr(md5(var.cluster_name), 0, 8)}"

  # A service account id is 6 to 30 characters; the plant name fits.
  node_account_id = local.name

  # GCP label values accept lowercase letters, digits, '-' and '_' only, so
  # the Prow ids go in verbatim; quoted because a Prow build id is an integer.
  ci_labels = {
    "managed-by"  = "kube-agents-bench"
    "build-id"    = var.prow_build_id != "" ? var.prow_build_id : "local"
    "pull-number" = var.prow_pull_number != "" ? var.prow_pull_number : "none"
  }

  # The Kubernetes label on the planted node; the case's safeguard selects on
  # it. Repeated in the task file: change one and change both.
  node_label_key   = "bench-plant"
  node_label_value = "pod-range-full"

  # 110 Pods is a /24 block per node; var.node_count of them fill the /21
  # range's eight blocks.
  max_pods_per_node = 110
}

provider "google" {
  project = var.project_id
  region  = local.region
}

# A dedicated node account with no project role, destroyed with the stack. A
# service account takes no labels, so its description carries the plant
# prefix fixture rule 8 gives such a kind (bench/README.md). The compute sweep
# cannot delete the account: it names it when it reaps the cluster.
resource "google_service_account" "nodes" {
  account_id   = local.node_account_id
  display_name = "GKE node service account for bench plant ${local.name}"
  description  = "kube-agents-bench plant (node account of ${local.name}); safe to delete when no cluster names it"
}

# The default pool is the planted pool, created with the cluster, so the
# apply pays for one pool rather than a throwaway one and its replacement.
# Its nodes are the smallest shape GKE runs: only their count matters.
resource "google_container_cluster" "plant" {
  name     = local.name
  location = var.location

  networking_mode = "VPC_NATIVE"

  # A netmask alone makes GKE pick and create the range on the default
  # subnet; eight /24 blocks at 110 Pods a node are the whole of it.
  ip_allocation_policy {
    cluster_ipv4_cidr_block  = var.pod_range_netmask
    services_ipv4_cidr_block = var.services_range_netmask
  }

  default_max_pods_per_node = local.max_pods_per_node

  initial_node_count  = var.node_count
  deletion_protection = false
  resource_labels     = local.ci_labels

  node_config {
    machine_type    = "e2-small"
    disk_size_gb    = 20
    resource_labels = local.ci_labels
    labels          = { (local.node_label_key) = local.node_label_value }
    service_account = google_service_account.nodes.email
    oauth_scopes    = ["https://www.googleapis.com/auth/cloud-platform"]

    workload_metadata_config {
      mode = "GKE_METADATA"
    }

    metadata = {
      disable-legacy-endpoints = "true"
    }
  }

  # Workload Identity on with GKE_METADATA above, as on the fleet: a cluster
  # standing in the leased project during a run is one the other audits can
  # sweep, and a metadata escalation is not what this case plants.
  workload_identity_config {
    workload_pool = "${var.project_id}.svc.id.goog"
  }
}

# devops-bench reads these outputs after up() and fetches credentials for
# cluster_name, so the run's kubeconfig is the planted cluster and the case's
# resource_property safeguard reads its node.
output "cluster_name" {
  value = google_container_cluster.plant.name
}

output "cluster_location" {
  value = google_container_cluster.plant.location
}
