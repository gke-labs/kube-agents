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

terraform {
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 5.0.0"
    }
  }
}

# account_id is capped at 30 chars, so we can't fit a long cluster name. Truncating
# the name alone is unsafe: names that share a prefix but differ only in a suffix past
# the cutoff (e.g. "<base>-east" vs "<base>-west" when <base> is long) would collapse to
# the same account_id and collide. Append a short hash of the *full* cluster name so the
# id stays unique per cluster regardless of where the readable part is truncated.
locals {
  # A GCP IAM account_id must be lowercase letters, digits, and hyphens. Lowercase
  # the cluster name and collapse any other characters (uppercase, underscores,
  # dots) into hyphens before slicing so the readable part is always valid. The
  # md5 hash below is over the *original* name, so distinct names still differ.
  gke_nodes_name_slug = trim(substr(replace(lower(var.cluster_name), "/[^a-z0-9]+/", "-"), 0, 9), "-")

  # Named once so the account and the reap below cannot disagree about what to
  # look for.
  gke_nodes_account_id = "gke-nodes-${local.gke_nodes_name_slug}-${substr(md5(var.cluster_name), 0, 6)}"
}

# kube-agents fork: a run killed before its teardown leaves this module's
# resources behind with no state file to destroy them from, and the next apply
# then dies on "409 Already Exists" before the evaluation can start. Reap them
# first, so an aborted run costs the next one time rather than blocking it.
#
# This is safe only because the smoke test runs at max_concurrency 1: nothing
# else can own a cluster with this name. That assumption is load-bearing -- if
# the job is ever allowed to run concurrently, this deletes a live run's cluster.
#
# In CI each run gets a fresh pod and checkout, so there is never prior state
# here and this resource is created, and the reap runs every time. Locally,
# state persists under bench/tf, so after the first apply this only re-runs if
# cluster_name, location or project_id change.
resource "terraform_data" "reap_orphans" {
  triggers_replace = [var.cluster_name, var.location, var.project_id]

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    command     = <<-EOT
      set -euo pipefail

      # The cluster goes first: its nodes authenticate as the service account
      # below, so deleting that account first would strand them.
      if gcloud container clusters describe "${var.cluster_name}" \
           --location "${var.location}" --project "${var.project_id}" >/dev/null 2>&1; then
        echo "reaping orphaned cluster ${var.cluster_name} left by a previous run"
        gcloud container clusters delete "${var.cluster_name}" \
          --location "${var.location}" --project "${var.project_id}" --quiet
      fi

      sa="${local.gke_nodes_account_id}@${var.project_id}.iam.gserviceaccount.com"

      # Strip the account's project bindings before deleting the account itself.
      # Deleting it first leaves each binding behind as an inert
      # "deleted:serviceAccount:<email>?uid=..." member that nothing later
      # removes, and the recreated account gets a new uid, so its fresh bindings
      # are separate entries -- every aborted run would add five more.
      #
      # The filter matches the tombstones as well as the live member, so this
      # also clears what earlier runs left behind. It reaps only the resources
      # this module deploys for the evaluation, nothing else in the project. It
      # runs outside the existence check below because those tombstones outlive
      # the account.
      #
      # Roles are read back from the live policy rather than listed here, so
      # this cannot drift from the google_project_iam_member resources above.
      # Filtering on this account's email also keeps it away from
      # agent_container_admin, which binds a long-lived external account.
      policy=$(gcloud projects get-iam-policy "${var.project_id}" \
                 --flatten="bindings[].members" \
                 --filter="bindings.members:$sa" \
                 --format="value(bindings.role,bindings.members)")

      # Reported after the attempt rather than before it: this stays
      # best-effort so one stuck binding cannot block the run, but a reap where
      # every removal failed must not read identically to one where they all
      # succeeded. Whatever is left behind is matched again by the next reap.
      failed=0
      while read -r role member; do
        [[ -n "$role" && -n "$member" ]] || continue
        if gcloud projects remove-iam-policy-binding "${var.project_id}" \
             --member "$member" --role "$role" --condition=None --quiet >/dev/null; then
          echo "removed stale binding $role for $member"
        else
          failed=$((failed + 1))
          echo "WARNING: could not remove stale binding $role for $member"
        fi
      done <<< "$policy"

      # One line to grep for: the per-binding warnings above are easy to lose in
      # a Prow log thousands of lines long.
      if [ "$failed" -gt 0 ]; then
        echo "WARNING: $failed stale binding(s) survived the reap; the next run retries them"
      fi

      if gcloud iam service-accounts describe "$sa" --project "${var.project_id}" >/dev/null 2>&1; then
        echo "reaping orphaned service account $sa"
        gcloud iam service-accounts delete "$sa" --project "${var.project_id}" --quiet
      fi
    EOT
  }
}

resource "google_service_account" "gke_nodes" {
  account_id   = local.gke_nodes_account_id
  display_name = "GKE Node Service Account for ${var.cluster_name}"

  depends_on = [terraform_data.reap_orphans]
}

resource "google_project_iam_member" "gke_nodes_log_writer" {
  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.gke_nodes.email}"
}

resource "google_project_iam_member" "gke_nodes_metric_writer" {
  project = var.project_id
  role    = "roles/monitoring.metricWriter"
  member  = "serviceAccount:${google_service_account.gke_nodes.email}"
}

resource "google_project_iam_member" "gke_nodes_monitoring_viewer" {
  project = var.project_id
  role    = "roles/monitoring.viewer"
  member  = "serviceAccount:${google_service_account.gke_nodes.email}"
}

resource "google_project_iam_member" "gke_nodes_metadata_writer" {
  project = var.project_id
  role    = "roles/stackdriver.resourceMetadata.writer"
  member  = "serviceAccount:${google_service_account.gke_nodes.email}"
}

resource "google_project_iam_member" "gke_nodes_artifact_registry_reader" {
  project = var.project_id
  role    = "roles/artifactregistry.reader"
  member  = "serviceAccount:${google_service_account.gke_nodes.email}"
}

resource "google_project_iam_member" "agent_container_admin" {
  count   = var.agent_service_account != "" ? 1 : 0
  project = var.project_id
  role    = "roles/container.admin"
  member  = "serviceAccount:${var.agent_service_account}"
}

resource "google_compute_firewall" "allow_iap_ssh" {
  count   = var.enable_iap_ssh ? 1 : 0
  name    = "allow-iap-ssh-${var.cluster_name}"
  network = "default"
  project = var.project_id

  allow {
    protocol = "tcp"
    ports    = ["22"]
  }

  source_ranges           = ["35.235.240.0/20"]
  target_service_accounts = [google_service_account.gke_nodes.email]
}

# kube-agents fork: everything this module creates is disposable evaluation
# infrastructure, and a run killed before its teardown leaves it behind with no
# state file left to destroy it from. This label is the marker the orphan sweep
# matches on, so it is fixed here rather than passed in -- a caller cannot
# forget it, and it is identical on every run.
locals {
  bench_labels = {
    "managed-by" = "kube-agents-bench"
  }
}

resource "google_container_cluster" "primary" {
  name     = var.cluster_name
  location = var.location

  remove_default_node_pool = true
  initial_node_count       = 1
  deletion_protection      = false
  min_master_version       = var.kubernetes_version
  resource_labels          = local.bench_labels

  dynamic "workload_identity_config" {
    for_each = var.enable_workload_identity ? [1] : []
    content {
      workload_pool = "${var.project_id}.svc.id.goog"
    }
  }

  depends_on = [terraform_data.reap_orphans]
}

locals {
  # Map abstract types to GKE native guest accelerator strings
  abstract_gpu_map = {
    "l4"   = "nvidia-l4"
    "a100" = "nvidia-tesla-a100"
    "t4"   = "nvidia-tesla-t4"
  }

  # Map machine family prefix to GKE native guest accelerator strings
  machine_family_gpu_map = {
    "g2" = "nvidia-l4"
    "a2" = "nvidia-tesla-a100"
  }

  is_g2 = startswith(var.machine_type, "g2-")
  is_a2 = startswith(var.machine_type, "a2-")

  # Determine final GPU attachment parameters
  enable_gpu = var.gpu_type != "" || local.is_g2 || local.is_a2

  # Extract machine family (e.g. "g2" from "g2-standard-4")
  machine_family = split("-", var.machine_type)[0]

  # Deduce GPU type from machine family if not explicitly set but GPU is enabled.
  # This will fail at plan time if machine_family is not in machine_family_gpu_map.
  deduced_gpu_type = var.gpu_type == "" && local.enable_gpu ? local.machine_family_gpu_map[local.machine_family] : ""

  gpu_type = var.gpu_type != "" ? lookup(local.abstract_gpu_map, var.gpu_type) : local.deduced_gpu_type
}

resource "google_container_node_pool" "primary_nodes" {
  name       = "primary-node-pool"
  location   = var.location
  cluster    = google_container_cluster.primary.name
  node_count = var.node_count
  version    = var.kubernetes_version

  node_config {
    preemptible     = false
    machine_type    = var.machine_type
    service_account = google_service_account.gke_nodes.email

    # The node pool itself is not a labelable GCP resource, but this puts the
    # marker on the Compute Engine instances it creates, so nodes outliving a
    # deleted cluster are still identifiable.
    resource_labels = local.bench_labels

    oauth_scopes = [
      "https://www.googleapis.com/auth/cloud-platform"
    ]

    dynamic "guest_accelerator" {
      for_each = local.enable_gpu ? [1] : []
      content {
        type  = local.gpu_type
        count = var.gpu_count
        gpu_driver_installation_config {
          gpu_driver_version = "DEFAULT"
        }
      }
    }

    dynamic "workload_metadata_config" {
      for_each = var.enable_workload_identity ? [1] : []
      content {
        mode = "GKE_METADATA"
      }
    }
  }
}

output "cluster_name" {
  value = google_container_cluster.primary.name
}

output "cluster_location" {
  value = google_container_cluster.primary.location
}

output "endpoint" {
  value = google_container_cluster.primary.endpoint
}

output "cluster_ca_certificate" {
  value = google_container_cluster.primary.master_auth[0].cluster_ca_certificate
}
