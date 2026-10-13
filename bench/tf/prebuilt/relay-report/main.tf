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

# The scenario driver for bench/tasks/chat-relay-posts-report-as-written: hand
# one scheduled report to the chat relay, the way every `deliver: "chat"` job's
# report reaches it, and record what the relay answered.
#
# relay_probe.py (beside this file) POSTs report.txt to the Session KV server's
# /v1/cron-reports on loopback in the agent container: the call the scheduler's
# relay plugin makes (deploy/docker/plugins/chat/adapter.py). The route posts to
# every enabled chat platform and stores what it posted against the thread; the
# verifier reads that back. The call is synchronous, so the apply returns once
# the relay has answered.
#
# report.txt is modelled on the fleet-wide-cost-analysis report that
# toshiowang-gkedemos3's relay turn answered with a kanban card and a one-line
# reply on 2026-10-08 (gke-labs/kube-agents#2742). The workload it names does
# not exist, so a card an image's relay turn files sends a specialist after
# nothing.
#
# On an install with a chat platform the report is posted to the home channel.
#
# The teardown, and the exit trap on a failed apply, archive any card filed from
# the probe's relay sessions, delete the probe's routing rows and stored posts,
# and remove the state file.

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
  home       = "/opt/data"
  hermes     = "/opt/hermes/.venv/bin/hermes"
  python     = "/opt/hermes/.venv/bin/python3"
  probe_b64  = base64encode(file("${path.module}/relay_probe.py"))
  report_b64 = base64encode(file("${path.module}/report.txt"))
  # The Session KV server's loopback address in the agent pod
  # (deploy/docker/plugins/chat/adapter.py, DEFAULT_RELAY_URL).
  session_kv_url = "http://127.0.0.1:8699" # sanitizer: allow the agent pod's own loopback, where the Session KV server listens
  # How long an exec into the agent Deployment waits for a running pod; see
  # bench/tf/prebuilt/bootstrap-ranking/main.tf.
  pod_wait = 5
}

resource "null_resource" "relay_probe" {
  triggers = {
    host_cluster   = var.host_cluster_name
    host_location  = var.host_cluster_location
    host_project   = var.project_id
    namespace      = var.agent_namespace
    deployment     = var.agent_deployment
    container      = var.agent_container
    pod_wait       = local.pod_wait
    home           = local.home
    python         = local.python
    hermes         = local.hermes
    session_kv_url = local.session_kv_url
    probe_b64      = local.probe_b64
    report_b64     = local.report_b64
  }

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    command     = <<-EOT
      set -euo pipefail

      # Own kubeconfig: by the time this apply runs, an earlier tofu task may
      # have pointed the ambient context at its own cluster.
      kubeconfig_dir="$(mktemp -d)"

      # Terraform taints a resource whose create-time provisioner failed and
      # skips its destroy-time provisioners, so a failure after the post starts
      # cleans up here. errexit stays in force inside a trap, hence `set +e`.
      posting=""
      on_exit() {
        status=$?
        trap '' TERM INT
        set +e
        if [ "$status" -ne 0 ] && [ -n "$posting" ]; then
          echo "Post failed (exit $status); cleaning up." >&2
          probe clean >&2 || echo "Cleanup incomplete. The next run cleans up before it posts." >&2
        fi
        rm -rf "$kubeconfig_dir"
      }
      trap on_exit EXIT
      # bash skips the EXIT trap when an untrapped signal kills it, and a Prow
      # deadline arrives as SIGTERM.
      trap 'exit 143' TERM INT
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

      probe() {
        printf '%s' '${local.probe_b64}' | base64 -d | \
          kubectl exec -i -n "${var.agent_namespace}" "deployment/${var.agent_deployment}" \
            -c "${var.agent_container}" --pod-running-timeout=${local.pod_wait}s -- \
            ${local.python} - "$1" "${local.home}" "${local.hermes}" "${local.session_kv_url}" "$${@:2}"
      }

      # An earlier run's leftovers are removed first.
      probe clean

      posting=1
      probe post '${local.report_b64}'
    EOT
  }

  provisioner "local-exec" {
    when        = destroy
    on_failure  = continue
    interpreter = ["/bin/bash", "-c"]
    # Best effort, with no errexit: a cleanup that cannot run is finished by the
    # next apply's first step.
    command = <<-EOT
      set -uo pipefail
      kubeconfig_dir="$(mktemp -d)"
      trap 'rm -rf "$kubeconfig_dir"' EXIT
      KUBECONFIG="$kubeconfig_dir/config"
      export KUBECONFIG

      project="${self.triggers.host_project}"
      if [ -z "$project" ]; then
        project="$(gcloud config get-value project 2>/dev/null || true)"
      fi
      gcloud container clusters get-credentials "${self.triggers.host_cluster}" \
        --location "${self.triggers.host_location}" --project "$project" --quiet \
        || echo "WARNING: could not fetch credentials for ${self.triggers.host_cluster}; the cleanup below will likely fail too." >&2

      printf '%s' '${self.triggers.probe_b64}' | base64 -d \
        | kubectl exec -i -n "${self.triggers.namespace}" "deployment/${self.triggers.deployment}" \
          -c "${self.triggers.container}" --pod-running-timeout=${self.triggers.pod_wait}s -- \
          ${self.triggers.python} - clean "${self.triggers.home}" "${self.triggers.hermes}" "${self.triggers.session_kv_url}" \
        || echo "WARNING: could not clean up the relay probe on ${self.triggers.host_cluster}; the next run of this case cleans up before it posts." >&2
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
