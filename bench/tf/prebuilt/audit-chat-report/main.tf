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

# The scenario driver for bench/tasks/fleet-audit-chat-report-is-the-report: run
# one scheduled fleet audit for real and let its report reach the chat relay, so
# the verifier can read what the channel got.
#
# The run must not be silent, or nothing reaches the channel to grade. Before it
# fires, the stack renames the stream's `latest.json` in the report store on the
# shell sandbox (to `latest.json.bench-moved`): with no trusted memory of the
# previous run, `finish` answers `silent_ok: false` whatever the fleet holds
# (fleet-audit SKILL.md, "lost memory"). A run that reaches `finish` writes a new
# `latest.json`, the store's record from then on, and the renamed copy is
# deleted; where no new one was written, the teardown, or the exit trap on a
# failed apply, puts the renamed copy back (store.sh), so the stream keeps its
# memory.
#
# arm.py (beside this file) marks the job due with Hermes' own `trigger_job`, as
# the `oobe` job's first-run audits stage does, and waits for the run to end. The
# profile's cron tick claims the mark and the scheduler delivers the run through
# the job's `deliver` leg, so the report takes the path every scheduled report
# takes. An audit run already in flight on the stream is waited out first, up to
# `own_wait`, because its end would otherwise read as this run's.
#
# On an install with a chat platform the report is posted to the home channel,
# and the audit writes its ledger issue in the install's GitOps repository.
#
# The teardown removes the state file and restores or deletes the renamed store
# files as above.

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
  home      = "/opt/data"
  python    = "/opt/hermes/.venv/bin/python3"
  arm_b64   = base64encode(file("${path.module}/arm.py"))
  store_b64 = base64encode(file("${path.module}/store.sh"))
  # audit_report.py: REPORTS_DIR and the in-flight note under SCRATCH_DIR.
  store    = "/opt/data/fleet-audit/reports/${var.audit_job}"
  inflight = "/opt/data/scratch/inflight_${var.audit_job}.json"
  # audit_report.py: INFLIGHT_TTL_SECONDS; an older note holds nothing.
  inflight_ttl = 7200
  pod_wait     = 5
}

resource "null_resource" "audit_chat_report" {
  triggers = {
    host_cluster      = var.host_cluster_name
    host_location     = var.host_cluster_location
    host_project      = var.project_id
    namespace         = var.agent_namespace
    deployment        = var.agent_deployment
    container         = var.agent_container
    sandbox_pod       = var.sandbox_pod
    sandbox_container = var.sandbox_container
    audit_job         = var.audit_job
    pod_wait          = local.pod_wait
    home              = local.home
    python            = local.python
    store             = local.store
    arm_b64           = local.arm_b64
    store_b64         = local.store_b64
  }

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    command     = <<-EOT
      set -euo pipefail

      kubeconfig_dir="$(mktemp -d)"
      # Terraform taints a resource whose create-time provisioner failed and
      # skips its destroy-time provisioners, so a failure after the rename puts
      # the store back here. errexit stays in force inside a trap, hence `set +e`.
      moved=""
      on_exit() {
        status=$?
        trap '' TERM INT
        set +e
        if [ "$status" -ne 0 ] && [ -n "$moved" ]; then
          echo "Apply failed (exit $status); restoring the report store." >&2
          store restore >&2 || echo "WARNING: could not restore the report store." >&2
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
        echo "ERROR: no project id. Pass -var project_id=... or set a gcloud default project." >&2
        exit 1
      fi
      gcloud container clusters get-credentials "${var.host_cluster_name}" \
        --location "${var.host_cluster_location}" --project "$project" --quiet

      arm() {
        printf '%s' '${local.arm_b64}' | base64 -d | \
          kubectl exec -i -n "${var.agent_namespace}" "deployment/${var.agent_deployment}" \
            -c "${var.agent_container}" --pod-running-timeout=${local.pod_wait}s -- \
            ${local.python} - "$@"
      }
      sandbox() {
        kubectl exec -n "${var.agent_namespace}" "${var.sandbox_pod}" -c "${var.sandbox_container}" -- sh -c "$1"
      }
      store() {
        printf '%s' '${local.store_b64}' | base64 -d | \
          kubectl exec -i -n "${var.agent_namespace}" "${var.sandbox_pod}" -c "${var.sandbox_container}" -- \
            sh -s "$1" "${local.store}"
      }

      arm clean "${local.home}"

      deadline=$(( $(date +%s) + ${var.own_wait} ))
      while sandbox 'f=${local.inflight}; [ -f "$f" ] && [ $(( $(date +%s) - $(stat -c %Y "$f") )) -lt ${local.inflight_ttl} ]'; do
        if [ "$(date +%s)" -ge "$deadline" ]; then
          echo "ERROR: a ${var.audit_job} run is still in flight after ${var.own_wait}s." >&2
          exit 1
        fi
        echo "A ${var.audit_job} run is in flight; waiting for it to end."
        sleep 30
      done

      moved=1
      store move

      arm fire "${local.home}" "${local.python}" "${var.audit_job}"
      arm wait "${local.home}" "${var.audit_job}" "${var.run_wait}"
    EOT
  }

  provisioner "local-exec" {
    when        = destroy
    on_failure  = continue
    interpreter = ["/bin/bash", "-c"]
    command     = <<-EOT
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
        || echo "WARNING: could not fetch credentials for ${self.triggers.host_cluster}." >&2

      printf '%s' '${self.triggers.arm_b64}' | base64 -d \
        | kubectl exec -i -n "${self.triggers.namespace}" "deployment/${self.triggers.deployment}" \
          -c "${self.triggers.container}" --pod-running-timeout=${self.triggers.pod_wait}s -- \
          ${self.triggers.python} - clean "${self.triggers.home}" \
        || echo "WARNING: could not remove the state file; the next run removes it first." >&2
      printf '%s' '${self.triggers.store_b64}' | base64 -d \
        | kubectl exec -i -n "${self.triggers.namespace}" "${self.triggers.sandbox_pod}" -c "${self.triggers.sandbox_container}" -- \
          sh -s restore "${self.triggers.store}" \
        || echo "WARNING: could not restore the renamed store files." >&2
    EOT
  }
}

output "cluster_name" {
  value = var.host_cluster_name
}

output "cluster_location" {
  value = var.host_cluster_location
}
