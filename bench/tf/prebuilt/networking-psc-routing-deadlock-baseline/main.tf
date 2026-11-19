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

# The scenario driver for bench/tasks/networking-psc-routing-deadlock-baseline.
#
# It plants a Private Service Connect endpoint whose producer rejects it, for
# the networking audit's psc-routing-deadlock check (SOP 2.3): a forwarding
# rule that targets a service attachment and reads pscConnectionStatus
# REJECTED or CLOSED. The producer side is a dedicated VPC with an internal
# passthrough load balancer (a backend service with no backends behind one
# forwarding rule) published through a service attachment whose consumer
# reject list names this project. The consumer side is a second dedicated
# VPC, because an endpoint cannot sit in the producer's own network, holding
# one internal address and the endpoint forwarding rule. The endpoint reads
# REJECTED within a minute of its creation and stays so.
#
# This is the readable half of the customer failure: traffic to the producer
# black-holes while every object reads healthy. No route, no firewall rule and
# nothing on the host cluster is planted; the audit reads GCP only.
#
# Isolation class: per-run project-level Compute objects in the leased
# project, created and torn down by this stack. Every object carries the
# description prefix hack/ci_sweep_compute_plants.py matches, and the kinds
# that take labels carry the bench label set, so a killed run's leftovers
# can be identified. Nothing on the host cluster, the host's VPC or the
# seeded fleet is touched.

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
  suffix        = substr(md5(var.cluster_name), 0, 8)
  producer_name = "${var.name_prefix}-producer-${local.suffix}"
  consumer_name = "${var.name_prefix}-consumer-${local.suffix}"
  # The case's objective names this prefix: the endpoint is the object the
  # check files its finding on (ForwardingRule/<region>/<name>).
  endpoint_name = "${var.endpoint_name_prefix}-${local.suffix}"
  # Networks, subnets, health checks, backend services and service
  # attachments take no labels, so a sweep for a killed run's leftovers has
  # this fixed description to match on (hack/ci_sweep_compute_plants.py), as
  # well as the names.
  plant_description = "kube-agents-bench plant (networking-psc-routing-deadlock-baseline); safe to delete when no eval run holds the infra lock"
  labels = {
    "managed-by"  = "kube-agents-bench"
    "build-id"    = var.prow_build_id != "" ? var.prow_build_id : "local"
    "pull-number" = var.prow_pull_number != "" ? var.prow_pull_number : "none"
  }
  # Each /28 holds one address of this stack's, far above the subnet
  # check's 15% floor, so the stack files no subnet-ip-exhaustion finding of
  # its own; the NAT subnet's purpose keeps it out of that check.
  producer_cidr = "10.20.0.0/28" # sanitizer: allow a private range inside this stack's own VPC
  psc_nat_cidr  = "10.20.1.0/28" # sanitizer: allow a private range inside this stack's own VPC
  consumer_cidr = "10.30.0.0/28" # sanitizer: allow a private range inside this stack's own VPC
  service_port  = 80
}

provider "google" {
  project = var.project_id
  region  = local.region
}

# --- The producer: a published service that rejects this project ---------

resource "google_compute_network" "producer" {
  name                    = local.producer_name
  description             = local.plant_description
  auto_create_subnetworks = false
}

resource "google_compute_subnetwork" "producer" {
  name          = local.producer_name
  description   = local.plant_description
  region        = local.region
  network       = google_compute_network.producer.id
  ip_cidr_range = local.producer_cidr
}

# A service attachment needs a NAT subnet of this purpose in the producer's
# network; Google allocates out of it, and the subnet check skips it.
resource "google_compute_subnetwork" "psc_nat" {
  name          = "${local.producer_name}-nat"
  description   = local.plant_description
  region        = local.region
  network       = google_compute_network.producer.id
  ip_cidr_range = local.psc_nat_cidr
  purpose       = "PRIVATE_SERVICE_CONNECT"
}

resource "google_compute_region_health_check" "producer" {
  name        = local.producer_name
  description = local.plant_description
  region      = local.region

  tcp_health_check {
    port = local.service_port
  }
}

# No backends: nothing answers, and nothing needs to. The service attachment
# publishes the forwarding rule, and the rejection is decided before any
# packet reaches a backend.
resource "google_compute_region_backend_service" "producer" {
  name                  = local.producer_name
  description           = local.plant_description
  region                = local.region
  load_balancing_scheme = "INTERNAL"
  protocol              = "TCP"
  health_checks         = [google_compute_region_health_check.producer.id]
}

resource "google_compute_forwarding_rule" "producer" {
  name                  = local.producer_name
  description           = local.plant_description
  region                = local.region
  load_balancing_scheme = "INTERNAL"
  backend_service       = google_compute_region_backend_service.producer.id
  network               = google_compute_network.producer.id
  subnetwork            = google_compute_subnetwork.producer.id
  ports                 = [tostring(local.service_port)]
  labels                = local.labels
}

# The consumer reject list is what plants the status: an endpoint from a
# project on it reads pscConnectionStatus REJECTED, the value the check
# flags. ACCEPT_MANUAL with an empty accept list means no other project can
# connect either.
resource "google_compute_service_attachment" "producer" {
  name                  = local.producer_name
  description           = local.plant_description
  region                = local.region
  connection_preference = "ACCEPT_MANUAL"
  consumer_reject_lists = [var.project_id]
  enable_proxy_protocol = false
  nat_subnets           = [google_compute_subnetwork.psc_nat.id]
  target_service        = google_compute_forwarding_rule.producer.id
}

# --- The consumer: the endpoint the audit finds --------------------------

resource "google_compute_network" "consumer" {
  name                    = local.consumer_name
  description             = local.plant_description
  auto_create_subnetworks = false
}

resource "google_compute_subnetwork" "consumer" {
  name          = local.consumer_name
  description   = local.plant_description
  region        = local.region
  network       = google_compute_network.consumer.id
  ip_cidr_range = local.consumer_cidr
}

resource "google_compute_address" "endpoint" {
  name         = local.endpoint_name
  description  = local.plant_description
  region       = local.region
  address_type = "INTERNAL"
  subnetwork   = google_compute_subnetwork.consumer.id
  labels       = local.labels
}

# A Private Service Connect endpoint is a forwarding rule with an empty
# load-balancing scheme whose target is the service attachment; its
# pscConnectionStatus is what `forwarding-rules list` shows the audit.
resource "google_compute_forwarding_rule" "endpoint" {
  name                  = local.endpoint_name
  description           = local.plant_description
  region                = local.region
  load_balancing_scheme = ""
  target                = google_compute_service_attachment.producer.id
  network               = google_compute_network.consumer.id
  ip_address            = google_compute_address.endpoint.id
  labels                = local.labels
}

# This stack creates no cluster. devops-bench reads these outputs after up()
# and points the ambient kubeconfig at cluster_name, so they name the agent's
# host cluster, as prebuilt/subnet-range-exhaustion does.
output "cluster_name" {
  value = var.host_cluster_name
}

output "cluster_location" {
  value = var.host_cluster_location
}

output "endpoint_name" {
  description = "The planted endpoint's name, the object the case's objective names."
  value       = google_compute_forwarding_rule.endpoint.name
}
