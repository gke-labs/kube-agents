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

module "cluster" {
  source = "../../modules/cluster"

  infra_provider  = var.infra_provider
  cluster_name    = var.cluster_name
  location        = var.location
  node_count      = var.node_count
  machine_type    = var.machine_type
  project_id      = var.project_id
  kubeconfig_path = var.kubeconfig_path
  gpu_type        = "l4"
  gpu_count       = 1
}

# The task is a post-incident analysis, so the incident is seeded rather than
# reproduced: two Cloud Logging entries stand in for the workload that has
# already stopped. They are what the agent has to find.
resource "null_resource" "write_synthetic_logs" {
  count = var.infra_provider == "gcp" ? 1 : 0

  provisioner "local-exec" {
    command = <<EOT
      gcloud logging write "container" "{\"message\": \"hypercomputer-agent: GCS FUSE buffer exhaustion during checkpoint load\", \"container_name\": \"hypercomputer-agent\"}" --severity=ERROR --project=${var.project_id} --payload-type=json --monitored-resource-type=k8s_container --monitored-resource-labels=project_id=${var.project_id},location=${module.cluster.location},cluster_name=${module.cluster.cluster_name},namespace_name=default,pod_name=hypercomputer-agent-deployment-xyz,container_name=hypercomputer-agent
      gcloud logging write "container" "{\"message\": \"HorizontalPodAutoscaler: HPA max-replica saturation for deployment/hypercomputer-agent (max: 10)\", \"container_name\": \"hpa-controller\"}" --severity=WARNING --project=${var.project_id} --payload-type=json --monitored-resource-type=k8s_container --monitored-resource-labels=project_id=${var.project_id},location=${module.cluster.location},cluster_name=${module.cluster.cluster_name},namespace_name=default,pod_name=hpa-controller-xyz,container_name=hpa-controller

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
      # noisy neighbour), the cluster name (unique per run, so a
      # concurrent run's entries cannot satisfy this one's wait), and the
      # two seeded message strings themselves.
      #
      # A failing poll is not a slow poll: gcloud erroring (IAM, API)
      # would otherwise read as "not ingested yet" for the full 120s and
      # bury the real cause. Three consecutive command failures fail
      # fast with gcloud's own stderr; a timeout prints it too.
      err_file="$(mktemp)"
      elapsed=0
      poll_errs=0
      while :; do
        if found="$(gcloud logging read "logName=\"projects/${var.project_id}/logs/container\" AND resource.type=\"k8s_container\" AND resource.labels.cluster_name=\"${module.cluster.cluster_name}\" AND (jsonPayload.message:\"GCS FUSE buffer exhaustion\" OR jsonPayload.message:\"HPA max-replica saturation\")" --project=${var.project_id} --freshness=10m --limit=10 --format='value(jsonPayload.message)' 2>"$err_file")"; then
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
  value = module.cluster.cluster_name
}

output "cluster_location" {
  value = module.cluster.location
}
