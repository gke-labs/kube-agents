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
  required_version = ">= 1.5.0"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 5.0.0"
    }
    kind = {
      source  = "tehcyx/kind"
      version = ">= 0.5.0"
    }
    null = {
      source  = "hashicorp/null"
      version = ">= 3.0.0"
    }
  }
}

# GCP label values accept lowercase letters, digits, '-' and '_' only, so the
# Prow ids go in verbatim.
locals {
  ci_labels = {
    "managed-by"  = "kube-agents-bench"
    "build-id"    = var.prow_build_id != "" ? var.prow_build_id : "local"
    "pull-number" = var.prow_pull_number != "" ? var.prow_pull_number : "none"
  }
}

provider "google" {
  project = var.project_id != "" ? var.project_id : null
  region  = var.location != "" && var.location != "local" ? var.location : null

  # module.cluster declares no provider of its own and so inherits this one:
  # the labels reach the cluster it creates without forking that module.
  default_labels = local.ci_labels
}

provider "kind" {}

# The directory name is historical and is deliberately NOT being changed:
# hack/ci-eval-pr.sh's TASKS list, testgrid history and the task.yaml
# `stack:` reference all point at "prebuilt/gpu-stress-test", and a rename
# costs far more than it buys. Nothing here is GPU-backed and nothing ever
# was in anger. The whole incident is seeded by null_resource
# .write_synthetic_logs below as two Cloud Logging entries; the cluster runs
# no workloads at all -- the task's own `read-only-in-a-post-incident-task`
# safeguard asserts exactly that ("still no Deployments in the default
# namespace"). The node hosts the GKE system pods and nothing else, so it
# takes a plain general-purpose machine type and no accelerator.
#
# gpu_type / gpu_count are omitted rather than set: the module defaults
# gpu_type to "" and the gke submodule's `enable_gpu` local is false for a
# non-g2/non-a2 machine type, so its `dynamic "guest_accelerator"` block
# emits nothing.
#
# The cluster itself cannot go away, even though nothing schedules onto it.
# devops-bench (pinned at 4670d76 in bench/pyproject.toml) calls
# `deployer.get_cluster_info()` unconditionally after `up()` for any
# non-noop deployer -- evalharness/default.py -- and TFDeployer's
# implementation reads the `cluster_name` and `cluster_location` outputs
# below and hands them to `GCPProvider.ensure_cluster_credentials`, which
# shells out to `gcloud container clusters get-credentials` with
# `check=True`. A logging-only stack therefore either raises ConfigError on
# the missing outputs or SubprocessError on credentials for a cluster that
# does not exist; both red the task for every PR. Making this stack
# cluster-free needs an upstream change (an opt-out for get_cluster_info),
# not a change here.
#
# What CAN go away is creating that cluster fresh per run. With
# reuse_existing_cluster the runner names an existing cluster (a seeded-fleet
# one), the module below plans nothing, the fixture and the outputs read the
# subject cluster from the variables, get_cluster_info still hands
# get-credentials a real cluster, and destroy tears down only the fixture
# resource. The orphan sweep rides this module and so does not run on reuse
# runs -- and every pool project carries the fleet, so in CI there is
# ordinarily no creating run left to ride; gke-labs/kube-agents#950 tracks
# giving the sweep a home that does not depend on one.
module "cluster" {
  count  = var.reuse_existing_cluster ? 0 : 1
  source = "../../modules/cluster"

  infra_provider  = var.infra_provider
  cluster_name    = var.cluster_name
  location        = var.location
  node_count      = var.node_count
  machine_type    = var.machine_type
  project_id      = var.project_id
  kubeconfig_path = var.kubeconfig_path
}

# The subject cluster of the planted incident: the one the module created, or
# on a reuse run the existing cluster the variables name.
locals {
  task_cluster_name     = coalesce(one(module.cluster[*].cluster_name), var.cluster_name)
  task_cluster_location = coalesce(one(module.cluster[*].location), var.location)
}

# The task is a post-incident analysis, so the incident is seeded rather than
# reproduced: two Cloud Logging entries stand in for the workload that has
# already stopped. They are what the agent has to find.
resource "null_resource" "write_synthetic_logs" {
  count = var.infra_provider == "gcp" ? 1 : 0

  # Re-plant when the subject cluster moves. A null_resource re-runs its
  # provisioner only when a trigger changes; without this, persistent local
  # state (the intended behaviour for local runs, per hack/ci-eval-pr.sh)
  # would keep the OLD subject's entries when the fleet arrives in a project
  # between two runs -- outputs and prompt naming a cluster whose fixture
  # was never written. Inert in CI, where state starts fresh every run.
  triggers = {
    subject_cluster = local.task_cluster_name
  }

  provisioner "local-exec" {
    command = <<EOT
      gcloud logging write "container" "{\"message\": \"hypercomputer-agent: GCS FUSE buffer exhaustion during checkpoint load\", \"container_name\": \"hypercomputer-agent\"}" --severity=ERROR --project=${var.project_id} --payload-type=json --monitored-resource-type=k8s_container --monitored-resource-labels=project_id=${var.project_id},location=${local.task_cluster_location},cluster_name=${local.task_cluster_name},namespace_name=default,pod_name=hypercomputer-agent-deployment-xyz,container_name=hypercomputer-agent
      gcloud logging write "container" "{\"message\": \"HorizontalPodAutoscaler: HPA max-replica saturation for deployment/hypercomputer-agent (max: 10)\", \"container_name\": \"hpa-controller\"}" --severity=WARNING --project=${var.project_id} --payload-type=json --monitored-resource-type=k8s_container --monitored-resource-labels=project_id=${var.project_id},location=${local.task_cluster_location},cluster_name=${local.task_cluster_name},namespace_name=default,pod_name=hpa-controller-xyz,container_name=hpa-controller

      # Cloud Logging ingestion is asynchronous: the writes above return
      # before the entries are queryable, and the evaluation starts the
      # moment this apply finishes -- so the agent could look for the
      # incident before it exists. Poll until both entries read back,
      # bounded, and fail the apply if they never do: a fixture that is
      # not queryable is an infrastructure failure, and it should die
      # here, loudly, rather than surface as a judged zero that reads as
      # the agent's mistake.
      #
      # The filter pins all three axes the writes control: the logName
      # (the writes above use log id "container"; GKE's own agents log
      # under different logNames, so without this a busy cluster's system
      # entries flood the newest-first --limit window and starve the
      # fixture pair out of it forever -- a healthy apply failing on a
      # noisy neighbour), the cluster name, and the two seeded message
      # strings themselves. On a per-run cluster the name is unique, so a
      # concurrent run's entries cannot satisfy this one's wait. On a
      # reuse run every concurrent run shares the seeded cluster's name,
      # so one run's entries CAN satisfy another's wait -- and that is
      # benign by construction: the writes are byte-identical, so the
      # fixture the agent must find exists either way.
      #
      # A failing poll is not a slow poll: gcloud erroring (IAM, API)
      # would otherwise read as "not ingested yet" for the full 120s and
      # bury the real cause. Three consecutive command failures fail
      # fast with gcloud's own stderr; a timeout prints it too.
      err_file="$(mktemp)"
      elapsed=0
      poll_errs=0
      while :; do
        if found="$(gcloud logging read "logName=\"projects/${var.project_id}/logs/container\" AND resource.type=\"k8s_container\" AND resource.labels.cluster_name=\"${local.task_cluster_name}\" AND (jsonPayload.message:\"GCS FUSE buffer exhaustion\" OR jsonPayload.message:\"HPA max-replica saturation\")" --project=${var.project_id} --freshness=10m --limit=10 --format='value(jsonPayload.message)' 2>"$err_file")"; then
          poll_errs=0
        else
          poll_errs=$((poll_errs + 1))
          found=""
          if [ "$poll_errs" -ge 3 ]; then
            echo "ERROR: the ingestion poll itself failed $poll_errs times running -- this is not an ingestion delay. Check logging.logEntries.list on the calling service account. gcloud said:" >&2
            cat "$err_file" >&2
            rm -f "$err_file"
            exit 1
          fi
        fi
        ok=0
        printf '%s' "$found" | grep -q "GCS FUSE buffer exhaustion" && ok=$((ok + 1))
        printf '%s' "$found" | grep -q "HPA max-replica saturation" && ok=$((ok + 1))
        if [ "$ok" -eq 2 ]; then
          echo "Synthetic log entries queryable after $${elapsed}s."
          rm -f "$err_file"
          break
        fi
        if [ "$elapsed" -ge 120 ]; then
          echo "ERROR: synthetic log entries not queryable after $${elapsed}s (found $ok of 2); the post-incident fixture does not exist and the evaluation must not start." >&2
          if [ -s "$err_file" ]; then
            echo "Last poll stderr:" >&2
            cat "$err_file" >&2
          fi
          rm -f "$err_file"
          exit 1
        fi
        sleep 5
        elapsed=$((elapsed + 5))
      done
    EOT
  }

  depends_on = [
    module.cluster
  ]
}

output "cluster_name" {
  value = local.task_cluster_name
}

output "cluster_location" {
  value = local.task_cluster_location
}
