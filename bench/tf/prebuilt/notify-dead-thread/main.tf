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

# The scenario driver for bench/tasks/notification-dead-thread-falls-back: ask
# `send_notification` to report into a thread that cannot receive it, and record
# what the tool did about it.
#
# notify_probe.py (beside this file) writes one session row naming a Google Chat
# thread in a space this install does not own, then calls the tool with that
# session id, in the agent container, against the running Session KV server and
# the operator-rendered config. Under `next` the A2A gateway refuses a threaded
# notification outside the home channel, so the threaded send fails on every run
# without a deleted thread or an archived space to arrange.
#
# What the two versions do with that refusal is the case. The tool before
# gke-labs/kube-agents#743's fix gates its home-channel broadcast on whether a
# target was *composed* rather than on whether one was *delivered*, so the
# refusal ends the call and the report reaches nobody. The fixed tool falls back
# to the home channel and says so. The verifier reads the state file and grades
# that difference.
#
# On an install carrying the fix this posts one real message to the home
# channel; notify_probe.MESSAGE says it is bench traffic.
#
# The teardown, and the exit trap on a failed apply, delete the probe's session
# row and any incident row stored against its coordinates, and remove the state
# file.

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
  home   = "/opt/data"
  python = "/opt/hermes/.venv/bin/python3"
  # The PVC copy of the agent scripts, which is the one that runs; /opt/defaults
  # is the image copy (scripts/dev/dev_rebuild_agent.sh).
  scripts   = "/opt/data/scripts"
  probe_b64 = base64encode(file("${path.module}/notify_probe.py"))
  # The Session KV server's loopback address in the agent pod
  # (deploy/docker/plugins/chat/adapter.py, DEFAULT_RELAY_URL).
  session_kv_url = "http://127.0.0.1:8699" # sanitizer: allow the agent pod's own loopback, where the Session KV server listens
  # How long an exec into the agent Deployment waits for a running pod; see
  # bench/tf/prebuilt/relay-report/main.tf.
  pod_wait = 5
}

resource "null_resource" "notify_probe" {
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
    scripts        = local.scripts
    session_kv_url = local.session_kv_url
    probe_b64      = local.probe_b64
  }

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    command     = <<-EOT
      set -euo pipefail

      # Own kubeconfig: by the time this apply runs, an earlier tofu task may
      # have pointed the ambient context at its own cluster.
      kubeconfig_dir="$(mktemp -d)"

      # Terraform taints a resource whose create-time provisioner failed and
      # skips its destroy-time provisioners, so a failure after the row is
      # planted cleans up here. errexit stays in force inside a trap, hence
      # `set +e`.
      planting=""
      on_exit() {
        status=$?
        trap '' TERM INT
        set +e
        if [ "$status" -ne 0 ] && [ -n "$planting" ]; then
          echo "Plant failed (exit $status); cleaning up." >&2
          probe clean >&2 || echo "Cleanup incomplete. The next run cleans up before it plants." >&2
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
            ${local.python} - "$1" "${local.home}" "${local.scripts}" "${local.session_kv_url}"
      }

      # An earlier run's leftovers are removed first.
      probe clean

      planting=1
      probe plant
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
          ${self.triggers.python} - clean "${self.triggers.home}" "${self.triggers.scripts}" "${self.triggers.session_kv_url}" \
        || echo "WARNING: could not clean up the notify probe on ${self.triggers.host_cluster}; the next run of this case cleans up before it plants." >&2
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
