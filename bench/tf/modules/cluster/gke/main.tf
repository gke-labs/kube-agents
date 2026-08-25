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
# resources behind with no state file to destroy them from. Sweep those
# leftovers before provisioning, so an aborted run costs money for at most
# orphan_max_age_hours rather than forever.
#
# This is a cost sweep, not a correctness precondition. Cluster names are
# derived from the Prow BUILD_ID (hack/ci-eval-pr.sh), so within a project two
# runs can never share a name and a "409 Already Exists" between runs is
# impossible by construction -- the sweep no longer has to clear the way for
# this run's own name. That alone does NOT make raising the Prow job's
# max_concurrency safe: every run installs cluster-wide singletons on the
# shared platform-agent-host cluster, so concurrency itself arrives with
# issue #637 (Boskos one-project-per-run leasing) -- do not raise it before
# that lands. Under #637 this sweep matters MORE, not less: leasing turns the
# Boskos janitor off, so this is the only cleanup a leased project gets.
#
# What keeps a live concurrent run's cluster out of the sweep is the AND of
# two server-side conditions: it must carry the managed-by=kube-agents-bench
# label (which this module fixes on everything it creates, and which the
# persistent platform-agent-host cluster does not carry), and it must be older
# than orphan_max_age_hours -- far longer than any evaluation runs.
#
# In CI each run gets a fresh pod and checkout, so there is never prior state
# here, this resource is created, and the sweep runs every time. Locally,
# state persists under bench/tf, so it re-runs only when cluster_name,
# location or project_id change; a laptop is not where orphans accumulate.
# A local run that lost its state file and 409s on its own stable name
# self-heals the same way: the leftover cluster ages out and the next sweep
# removes it.
#
# Residual the sweep does not cover: the allow-iap-ssh-<cluster> firewall
# rule (created only when enable_iap_ssh=true) is not labelable and is left
# behind for a scheduled janitor.
resource "terraform_data" "reap_orphans" {
  triggers_replace = [var.cluster_name, var.location, var.project_id, var.orphan_max_age_hours]

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    command     = <<-EOT
      set -uo pipefail

      project="${var.project_id}"

      # Best-effort throughout: a sweep that cannot list or delete must not
      # block the evaluation it runs ahead of. Whatever survives is matched
      # again by the next run's sweep.
      orphans=$(gcloud container clusters list --project "$project" \
        --filter="resourceLabels.managed-by=kube-agents-bench AND createTime<-PT${var.orphan_max_age_hours}H" \
        --format="value(name,location)" 2>/dev/null) \
        || { echo "WARNING: orphan sweep could not list clusters; skipping"; exit 0; }

      while read -r name location; do
        [[ -n "$name" && -n "$location" ]] || continue

        # The cluster delete is fired --async: a synchronous delete is ~5
        # minutes, runs serially ahead of provisioning, and a handful of
        # orphans would eat the Prow job's budget before this run's own
        # cluster exists. Deleting the service account below while the
        # teardown is still in flight loses little -- an orphan's nodes have
        # nothing left to do. </dev/null keeps gcloud from ever reading the
        # orphan list on stdin if a future version decides to prompt.
        echo "reaping orphaned cluster $name ($location), older than ${var.orphan_max_age_hours}h"
        if ! gcloud container clusters delete "$name" --async \
               --location "$location" --project "$project" --quiet </dev/null; then
          echo "WARNING: could not delete orphaned cluster $name; the next sweep retries it"
          continue
        fi

        # Re-derive the cluster's node service account exactly as the locals
        # above do: "gke-nodes-" + 9-char sanitized slug + "-" + first 6 hex
        # chars of md5 over the full cluster name. Change one, change both.
        slug=$(printf '%s' "$name" | tr '[:upper:]' '[:lower:]' \
                 | sed -E 's/[^a-z0-9]+/-/g' | cut -c1-9 | sed -E 's/^-+//; s/-+$//')
        if command -v md5sum >/dev/null 2>&1; then
          hash=$(printf '%s' "$name" | md5sum | cut -c1-6)
        else
          hash=$(printf '%s' "$name" | md5 -q | cut -c1-6)
        fi
        sa="gke-nodes-$slug-$hash@$project.iam.gserviceaccount.com"

        # Strip the account's project bindings before deleting the account
        # itself. Deleting it first leaves each binding behind as an inert
        # "deleted:serviceAccount:<email>?uid=..." member, and the tombstone
        # pass below is the only thing that would ever remove it.
        #
        # Roles are read back from the live policy rather than listed here, so
        # this cannot drift from the google_project_iam_member resources
        # below. Filtering on this account's email also keeps it away from
        # agent_container_admin, which binds a long-lived external account.
        policy=$(gcloud projects get-iam-policy "$project" \
                   --flatten="bindings[].members" \
                   --filter="bindings.members:$sa" \
                   --format="value(bindings.role,bindings.members)" 2>/dev/null || true)

        failed=0
        while read -r role member; do
          [[ -n "$role" && -n "$member" ]] || continue
          if gcloud projects remove-iam-policy-binding "$project" \
               --member "$member" --role "$role" --condition=None --quiet >/dev/null </dev/null; then
            echo "removed stale binding $role for $member"
          else
            failed=$((failed + 1))
            echo "WARNING: could not remove stale binding $role for $member"
          fi
        done <<< "$policy"
        if [ "$failed" -gt 0 ]; then
          echo "WARNING: $failed stale binding(s) survived the reap; the next sweep retries them"
        fi

        if gcloud iam service-accounts describe "$sa" --project "$project" >/dev/null 2>&1; then
          echo "reaping orphaned service account $sa"
          gcloud iam service-accounts delete "$sa" --project "$project" --quiet </dev/null \
            || echo "WARNING: could not delete service account $sa; the next sweep retries it"
        fi
      done <<< "$orphans"

      # Tombstone pass: bindings whose member is a deleted gke-nodes account
      # ("deleted:serviceAccount:gke-nodes-...?uid=...") are inert by
      # definition -- the account is gone -- so removing them is always safe
      # and can never race a live run. This clears what aborted runs left
      # behind before this sweep existed, and what a run killed between
      # account creation and cluster creation leaves (its account never
      # matches a reaped cluster above, so its bindings land here once the
      # account is eventually removed).
      #
      # Known residual: the *live* account of a run killed in the window
      # after the service account exists but before its cluster does is not
      # reaped -- an account's age is not listable, so deleting it cannot be
      # made race-free from here. It holds five project roles and costs
      # nothing; a scheduled janitor with an inventory is the right owner.
      tombstones=$(gcloud projects get-iam-policy "$project" \
        --flatten="bindings[].members" \
        --filter="bindings.members ~ ^deleted:serviceAccount:gke-nodes-.*@$project" \
        --format="value(bindings.role,bindings.members)" 2>/dev/null || true)
      while read -r role member; do
        [[ -n "$role" && -n "$member" ]] || continue
        if gcloud projects remove-iam-policy-binding "$project" \
             --member "$member" --role "$role" --condition=None --quiet >/dev/null </dev/null; then
          echo "removed tombstone binding $role for $member"
        else
          echo "WARNING: could not remove tombstone binding $role for $member"
        fi
      done <<< "$tombstones"

      exit 0
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
