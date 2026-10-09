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

# The scenario driver for bench/tasks/findings-decision-covers-item: plant the
# five findings-queue rows in findings.json on the install under test, every one
# `surfaced`, and resolve them again on destroy.
#
# Three rows share check, project and cluster, so they are one gathered line of
# the nudge's message, which names only the first of them by id. The other two
# share the project and differ in cluster or in check, so they are lines of their
# own, and a decision on the first line must leave them alone.
#
# queue.py does the work inside the agent container, through the Session KV
# server's own findings routes on loopback: the routes every build of that
# server has, so the plant is the same on a build whose decision covers the
# whole line and on one whose decision covers one row. It sets no
# `first_shown_at`: the item-wide decision does not read it, and marking rows as
# the paced publisher would spend a day's addition budget on the install.
#
# The project and clusters are invented names only this case uses. Nothing in
# the fleet carries them, and the teardown resolves every open row under that
# project. A plant that fails part-way runs the same teardown before it exits,
# because Terraform skips the destroy of a resource whose create failed.
#
# While the rows are planted, the install's nudge may name them in its next
# message, like any other open finding. They are planted at a non-critical
# severity on three lines, so one nudge run may add all three, up to the day's
# non-critical limit. The run's decision or the teardown closes them.

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
  python = "/opt/hermes/.venv/bin/python3"
  # The Session KV server's loopback address in the agent pod
  # (platform_mcp_server.py, _findings_request).
  session_kv_url = "http://127.0.0.1:8699" # sanitizer: allow the agent pod's own loopback, where the Session KV server listens
  rows_b64       = base64encode(file("${path.module}/findings.json"))
  script_b64     = base64encode(file("${path.module}/queue.py"))
  # How long an exec into the agent Deployment waits for a running pod; see
  # bench/tf/prebuilt/bootstrap-ranking/main.tf.
  pod_wait = 5
}

resource "null_resource" "findings" {
  triggers = {
    host_cluster   = var.host_cluster_name
    host_location  = var.host_cluster_location
    host_project   = var.project_id
    namespace      = var.agent_namespace
    deployment     = var.agent_deployment
    container      = var.agent_container
    python         = local.python
    session_kv_url = local.session_kv_url
    rows_b64       = local.rows_b64
    script_b64     = local.script_b64
    pod_wait       = local.pod_wait
  }

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    command     = <<-EOT
      set -euo pipefail

      kubeconfig_dir="$(mktemp -d)"
      planted=""
      on_exit() {
        status=$?
        trap '' TERM INT
        set +e
        if [ "$status" -ne 0 ] && [ -n "$planted" ]; then
          echo "Plant failed (exit $status); resolving the rows it may have planted." >&2
          queue teardown >&2 || echo "Teardown failed too; the next apply's plant resets the rows and its destroy resolves them." >&2
        fi
        rm -rf "$kubeconfig_dir"
      }
      trap on_exit EXIT
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

      queue() {
        printf '%s' '${local.script_b64}' | base64 -d | \
          kubectl exec -i -n "${var.agent_namespace}" "deployment/${var.agent_deployment}" \
            -c "${var.agent_container}" --pod-running-timeout=${local.pod_wait}s -- \
            ${local.python} - "$1" "${local.session_kv_url}" '${local.rows_b64}'
      }

      planted=1
      queue plant
    EOT
  }

  # No on_failure = continue: a destroy that cannot confirm the rows are closed
  # fails the run, rather than leaving open rows Terraform records as removed.
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

      printf '%s' '${self.triggers.script_b64}' | base64 -d | \
        kubectl exec -i -n "${self.triggers.namespace}" "deployment/${self.triggers.deployment}" \
          -c "${self.triggers.container}" --pod-running-timeout=${self.triggers.pod_wait}s -- \
          ${self.triggers.python} - teardown "${self.triggers.session_kv_url}" '${self.triggers.rows_b64}'
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
