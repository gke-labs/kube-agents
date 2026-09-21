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

# ---------------------------------------------------------------------------
# seeded-d: the multi-zonal cluster, and the only one whose SHAPE is the
# fixture. The anomaly-detection checks have to tell one cause of zonal skew
# from another, and a cluster whose nodes are all in one zone has no zonal
# distribution to skew -- a, b and c are each `location = var.zone` with a
# single-zone pool, so no amount of in-cluster planting produces the signal.
#
# Zonal control plane with `node_locations` across two zones, not a regional
# cluster: the fleet is standing, and a regional control plane would triple
# the node floor for a property nothing here needs. Two e2-small nodes, one
# per zone, is the minimum shape on which "the pods are all in one zone" is a
# true statement about a real cluster.
# ---------------------------------------------------------------------------

resource "google_container_cluster" "seeded_d" {
  name     = "${var.cluster_prefix}-d"
  location = var.zone

  # The shape. A zonal cluster with node_locations is multi-zonal: one control
  # plane in var.zone, nodes in both. var.second_zone defaults to a sibling of
  # var.zone's region, and the variable's description says why the pair has to
  # stay in one region.
  node_locations = [var.second_zone]

  remove_default_node_pool = true
  initial_node_count       = 1

  # local.fleet_labels, NOT local.cluster_labels. The `environment = seeded`
  # label in cluster_labels is what confines the drift cohort to exactly
  # {a, b, c}, and main.tf's locals block says outright that labelling a
  # fourth cluster with it "would add a fourth voter and change the
  # arithmetic". The drift scenario's severity ladder is computed on r = 2/3
  # over three clusters; a fourth voter moves it and reds
  # consistency-drift-outlier, a scenario this cluster has nothing to do
  # with. Unlabelled, seeded-d lands in the unknown-environment cohort, is
  # alone there, and the SOP's three-cluster floor keeps it from ever
  # producing a drift finding of its own.
  resource_labels     = local.fleet_labels
  deletion_protection = false

  logging_config {
    enable_components = ["SYSTEM_COMPONENTS", "WORKLOADS"]
  }

  # The same background closure a and c carry: enrolled with a window, so the
  # upgrade SOP's no-channel and no-maintenance-window checks stay quiet here
  # and the only findings this cluster produces are the ones it is for.
  release_channel {
    channel = "REGULAR"
  }

  maintenance_policy {
    daily_maintenance_window {
      start_time = "03:00"
    }
  }

  workload_identity_config {
    workload_pool = "${var.project_id}.svc.id.goog"
  }
}

# One node per zone. node_count on a multi-zonal pool is PER ZONE, so 1 here
# is two nodes in total -- the whole standing cost of this cluster.
resource "google_container_node_pool" "seeded_d_default" {
  name       = "default-pool"
  location   = var.zone
  cluster    = google_container_cluster.seeded_d.name
  node_count = 1

  node_config {
    machine_type    = "e2-small"
    disk_size_gb    = 20
    resource_labels = local.fleet_labels
    service_account = google_service_account.fleet_nodes.email
    oauth_scopes    = ["https://www.googleapis.com/auth/cloud-platform"]

    workload_metadata_config {
      mode = "GKE_METADATA"
    }
  }
}
