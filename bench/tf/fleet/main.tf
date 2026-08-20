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

# The seeded dirty fleet: three small standing clusters whose defects are the
# fixtures the Phase 2 presubmit scenarios assert on. Everything here is
# planted on purpose; "fixing" any of it breaks a test. README.md is the
# defect-to-scenario map, and the scheduled re-apply of this stack is what
# keeps the defects planted (GKE auto-upgrade would otherwise quietly heal
# seeded-b's version lag, and a well-meaning cleanup would delete the orphan
# disks).
#
# The label is deliberately NOT managed-by=kube-agents-bench: that label is
# what the eval orphan sweep in modules/cluster/gke deletes clusters by, and
# a standing fleet must never match it. kube-agents-host is the deploy
# pipeline's marker and is equally off limits.
locals {
  fleet_labels = {
    "managed-by" = "kube-agents-seeded-fleet"
  }
}

provider "google" {
  project = var.project_id
  zone    = var.zone
}

# seeded-b is held one minor version behind whatever the REGULAR channel
# currently defaults to, derived live rather than hardcoded so the pin does
# not rot: each scheduled re-apply re-computes "current default minus one"
# and re-pins. If GKE has auto-upgraded the cluster past the pin in the
# meantime, the apply moves it back down at the next maintenance window --
# re-planting the defect is this stack's whole job.
data "google_container_engine_versions" "fleet" {
  location = var.zone
  project  = var.project_id
}

locals {
  regular_default = data.google_container_engine_versions.fleet.release_channel_default_version["REGULAR"]
  default_minor   = tonumber(split(".", local.regular_default)[1])
  lagging_versions = [
    for v in data.google_container_engine_versions.fleet.valid_master_versions :
    v if startswith(v, "1.${local.default_minor - 1}.")
  ]
  # valid_master_versions is newest-first, so [0] is the freshest patch of the
  # previous minor: lagging by exactly one minor, not by every CVE since.
  lagging_version = local.lagging_versions[0]
}

# ---------------------------------------------------------------------------
# The three clusters. A carries every namespace-level defect; B is the
# version laggard; C is the consistency outlier. One file-level rule: a
# defect lives on exactly one cluster, so a scenario red points at one place.
# ---------------------------------------------------------------------------

resource "google_container_cluster" "seeded_a" {
  name     = "${var.cluster_prefix}-a"
  location = var.zone

  remove_default_node_pool = true
  initial_node_count       = 1
  resource_labels          = local.fleet_labels

  # The fleet is standing, but deletion protection stays off: the scheduled
  # re-apply must be able to replace a cluster when a facet change requires
  # it, and the label plus README are the guard against accidental sweeps.
  deletion_protection = false

  # A and B log workloads; C deliberately does not (see seeded_c).
  logging_config {
    enable_components = ["SYSTEM_COMPONENTS", "WORKLOADS"]
  }
}

resource "google_container_node_pool" "seeded_a_default" {
  name       = "default-pool"
  location   = var.zone
  cluster    = google_container_cluster.seeded_a.name
  node_count = 1

  node_config {
    # e2-medium rather than e2-small: cluster A hosts the defect workloads
    # (checkout-gateway, payments-api, inference-server) alongside system
    # pods, and e2-small's ~1.5 GiB allocatable cannot fit them all.
    machine_type    = "e2-medium"
    resource_labels = local.fleet_labels
    oauth_scopes    = ["https://www.googleapis.com/auth/cloud-platform"]
  }
}

# Defect (cost): a pool running zero non-system pods. Nothing schedules here
# because nothing selects it; its existence is the waste the cost audit must
# name. The name is the planted noun.
resource "google_container_node_pool" "idle_batch_pool" {
  name       = "idle-batch-pool"
  location   = var.zone
  cluster    = google_container_cluster.seeded_a.name
  node_count = 1

  node_config {
    machine_type    = "e2-small"
    resource_labels = local.fleet_labels
    oauth_scopes    = ["https://www.googleapis.com/auth/cloud-platform"]
  }
}

# Defect (capacity): a single-zone pool whose autoscaler is already at its
# maximum, hosting a workload whose HPA wants more than the pool can add
# (defects-a.tf plants the workload and the HPA). The capacity audit must
# name the pool and quantify the gap.
resource "google_container_node_pool" "pinned_inference_pool" {
  name       = "pinned-inference-pool"
  location   = var.zone
  cluster    = google_container_cluster.seeded_a.name
  node_count = 1

  autoscaling {
    min_node_count = 1
    max_node_count = 1
  }

  node_config {
    machine_type    = "e2-small"
    resource_labels = local.fleet_labels
    oauth_scopes    = ["https://www.googleapis.com/auth/cloud-platform"]
    labels = {
      "seeded-role" = "pinned-inference"
    }
  }
}

# Defect (upgrades): held one minor behind the REGULAR channel default.
# UNSPECIFIED channel plus a pinned version and auto-upgrade off is what
# keeps GKE from healing the lag between re-applies.
resource "google_container_cluster" "seeded_b" {
  name     = "${var.cluster_prefix}-b"
  location = var.zone

  remove_default_node_pool = true
  initial_node_count       = 1
  resource_labels          = local.fleet_labels
  deletion_protection      = false

  min_master_version = local.lagging_version
  release_channel {
    channel = "UNSPECIFIED"
  }

  logging_config {
    enable_components = ["SYSTEM_COMPONENTS", "WORKLOADS"]
  }

  lifecycle {
    precondition {
      condition     = length(local.lagging_versions) > 0
      error_message = "No valid master version exists one minor behind the REGULAR default; pick the lag manually this cycle."
    }
  }
}

resource "google_container_node_pool" "seeded_b_default" {
  name       = "default-pool"
  location   = var.zone
  cluster    = google_container_cluster.seeded_b.name
  node_count = 1
  version    = local.lagging_version

  management {
    auto_upgrade = false
  }

  node_config {
    machine_type    = "e2-small"
    resource_labels = local.fleet_labels
    oauth_scopes    = ["https://www.googleapis.com/auth/cloud-platform"]
  }
}

# Defect (consistency): the outlier. A and B enable workload logging; C
# enables system logging only. Two agree, one differs -- the drift audit's
# baseline is the fleet majority, which is the reason this fleet is three
# clusters and not two.
resource "google_container_cluster" "seeded_c" {
  name     = "${var.cluster_prefix}-c"
  location = var.zone

  remove_default_node_pool = true
  initial_node_count       = 1
  resource_labels          = local.fleet_labels
  deletion_protection      = false

  logging_config {
    enable_components = ["SYSTEM_COMPONENTS"]
  }
}

resource "google_container_node_pool" "seeded_c_default" {
  name       = "default-pool"
  location   = var.zone
  cluster    = google_container_cluster.seeded_c.name
  node_count = 1

  node_config {
    machine_type    = "e2-small"
    resource_labels = local.fleet_labels
    oauth_scopes    = ["https://www.googleapis.com/auth/cloud-platform"]
  }
}

# Defect (cost): two unattached disks. The prefix is the planted noun the
# waste audit must name; nothing ever mounts them.
resource "google_compute_disk" "orphan_pd" {
  count = 2
  name  = "orphan-pd-${count.index + 1}"
  type  = "pd-standard"
  size  = 10
  zone  = var.zone

  labels = local.fleet_labels
}
