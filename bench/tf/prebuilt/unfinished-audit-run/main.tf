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

# The scenario driver for bench/tasks/fleet-audit-unfinished-run-reported.
#
# It plants, in the stream's report store, one good `latest` (a clean run on an
# invented cluster, an hour old) and a newer run-start record that is never
# marked done: the trace a scheduled run leaves when it is stopped, runs out of
# memory, hits its deadline or loses its pod after `start`. The case then asks
# what the last audit found and grades that the reply says the last run did not
# finish, and does not give the clean report as the stream's current state.
#
# run_start.py does the work in the shell sandbox, where the audit worker runs
# audit_report.py: it writes the report with the function `finish` uses and
# the record through the store client `start` uses, so the plant is what those
# two would have left. The destroy removes what the seed wrote, by exact path,
# from the note the seed keeps beside the store, and puts back any `latest` the
# seed moved aside.

terraform {
  required_version = ">= 1.5.0"
  required_providers {
    null = {
      source  = "hashicorp/null"
      version = ">= 3.0.0"
    }
  }
}

locals {
  python     = "python3"
  script_b64 = base64encode(file("${path.module}/run_start.py"))
  # How long an exec into the sandbox waits for a running pod; see
  # bench/tf/prebuilt/bootstrap-ranking/main.tf.
  pod_wait = 5
}

resource "null_resource" "unfinished_run" {
  triggers = {
    host_cluster      = var.host_cluster_name
    host_location     = var.host_cluster_location
    host_project      = var.project_id
    namespace         = var.agent_namespace
    sandbox_selector  = var.sandbox_selector
    sandbox_container = var.sandbox_container
    audit_id          = var.audit_id
    gitops_repo       = var.gitops_repo
    python            = local.python
    script_b64        = local.script_b64
    pod_wait          = local.pod_wait
  }

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    command     = <<-EOT
      set -euo pipefail

      kubeconfig_dir="$(mktemp -d)"
      trap 'rm -rf "$kubeconfig_dir"' EXIT
      KUBECONFIG="$kubeconfig_dir/config"
      export KUBECONFIG

      project="${var.project_id}"
      if [ -z "$project" ]; then
        project="$(gcloud config get-value project 2>/dev/null || true)"
      fi
      if [ -z "$project" ]; then
        echo "ERROR: no project id. Pass -var project_id=... or set a gcloud default project; this stack needs one to fetch credentials for ${var.host_cluster_name}." >&2
        exit 1
      fi
      gcloud container clusters get-credentials "${var.host_cluster_name}" \
        --location "${var.host_cluster_location}" --project "$project" --quiet

      sandbox_pods="$(kubectl get pods -n "${var.agent_namespace}" -l "${var.sandbox_selector}" -o name)"
      set -- $sandbox_pods
      if [ "$#" -ne 1 ]; then
        echo "ERROR: expected one shell sandbox pod matching ${var.sandbox_selector} in ${var.agent_namespace}, found $#. The audit worker writes the report store from the sandbox, so the plant is written there." >&2
        exit 1
      fi

      printf '%s' '${local.script_b64}' | base64 -d | \
        kubectl exec -i -n "${var.agent_namespace}" "$1" -c "${var.sandbox_container}" \
          --pod-running-timeout=${local.pod_wait}s -- \
          ${local.python} - seed "${var.audit_id}" ${var.gitops_repo}
    EOT
  }

  # No on_failure = continue: a destroy that cannot remove the plant fails the
  # run, rather than leaving a record that Terraform records as removed.
  provisioner "local-exec" {
    when        = destroy
    interpreter = ["/bin/bash", "-c"]
    command     = <<-EOT
      set -euo pipefail
      kubeconfig_dir="$(mktemp -d)"
      trap 'rm -rf "$kubeconfig_dir"' EXIT
      KUBECONFIG="$kubeconfig_dir/config"
      export KUBECONFIG

      project="${self.triggers.host_project}"
      if [ -z "$project" ]; then
        project="$(gcloud config get-value project 2>/dev/null || true)"
      fi
      gcloud container clusters get-credentials "${self.triggers.host_cluster}" \
        --location "${self.triggers.host_location}" --project "$project" --quiet

      sandbox_pods="$(kubectl get pods -n "${self.triggers.namespace}" -l "${self.triggers.sandbox_selector}" -o name)"
      set -- $sandbox_pods
      if [ "$#" -ne 1 ]; then
        echo "ERROR: expected one shell sandbox pod matching ${self.triggers.sandbox_selector}, found $#; the planted run-start record stays in the report store." >&2
        exit 1
      fi

      printf '%s' '${self.triggers.script_b64}' | base64 -d | \
        kubectl exec -i -n "${self.triggers.namespace}" "$1" -c "${self.triggers.sandbox_container}" \
          --pod-running-timeout=${self.triggers.pod_wait}s -- \
          ${self.triggers.python} - teardown "${self.triggers.audit_id}" ${self.triggers.gitops_repo}
    EOT
  }
}

# Passed straight through: devops-bench reads these after apply and points the
# ambient kubeconfig at cluster_name.
output "cluster_name" {
  value = var.host_cluster_name
}

output "cluster_location" {
  value = var.host_cluster_location
}
