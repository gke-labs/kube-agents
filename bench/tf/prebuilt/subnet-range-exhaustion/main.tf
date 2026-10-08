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

# Plants a subnet whose primary range is nearly full, for the networking
# audit's subnet-ip-exhaustion check (SOP 2.1). A dedicated VPC holds one /29
# subnet: 8 addresses, of which GCP reserves 4, and this stack reserves 3 of
# the remaining 4 as static internal addresses. 7 of 8 in use is 12.5%
# available, under the check's 15% floor. GKE reports no usage for a primary
# range, so the only way to see it is to count the addresses held in it,
# which is what networking_audit.py's subnet sweep does. No cluster: the
# addresses alone exhaust the range, so the stack applies in about a minute.

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
  # Every stack in a run is handed the same cluster_name, so it is hashed
  # into this stack's own names rather than used as one.
  name = "${var.subnet_name_prefix}-${substr(md5(var.cluster_name), 0, 8)}"
  # Networks and subnets take no labels, so a sweep for a killed run's
  # leftovers has this fixed description to match on, as well as the name.
  plant_description = "kube-agents-bench plant (networking-audit-subnet-range-exhaustion); safe to delete when no eval run holds the infra lock"
}

provider "google" {
  project = var.project_id
  region  = local.region
}

resource "google_compute_network" "plant" {
  name                    = local.name
  description             = local.plant_description
  auto_create_subnetworks = false
}

resource "google_compute_subnetwork" "plant" {
  name          = local.name
  description   = local.plant_description
  region        = local.region
  network       = google_compute_network.plant.id
  ip_cidr_range = "10.10.0.0/29" # sanitizer: allow a private range inside this stack's own VPC
}

# Three of the four usable addresses. GCP picks each one; the count is what
# exhausts the range, not which addresses they are.
resource "google_compute_address" "plant" {
  count        = 3
  name         = "${local.name}-${count.index}"
  region       = local.region
  address_type = "INTERNAL"
  description  = local.plant_description
  subnetwork   = google_compute_subnetwork.plant.id
  labels = {
    "managed-by"  = "kube-agents-bench"
    "build-id"    = var.prow_build_id != "" ? var.prow_build_id : "local"
    "pull-number" = var.prow_pull_number != "" ? var.prow_pull_number : "none"
  }
}

# This stack creates no cluster. devops-bench reads these outputs after up()
# and points the ambient kubeconfig at cluster_name, so they name the agent's
# host cluster, as prebuilt/orphan-service does.
output "cluster_name" {
  value = var.host_cluster_name
}

output "cluster_location" {
  value = var.host_cluster_location
}
