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

# The scenario driver for bench/tasks/oobe-first-run-audits: put a long-lived
# install where a fresh one is when its onboarding inventory scan settles, so the
# `oobe` job's first-run audits stage fires, and let the verifier read whether the
# four audits were started.
#
# arm.py (beside this file) files two archived stand-in cards, a sweep and a
# ranking card after it, points `.bootstrap_scan_filed` at the sweep, removes
# `.oobe_audits_fired`, and puts back the `oobe` job when the deployed image ships
# one: an install that finished onboarding before the job existed never got it.
# An image without the job gets nothing put back, so nothing starts the audits.
#
# Before it arms, the apply waits until none of the four audits is still running
# from an earlier repetition, since an audit already in flight is not started
# again when it is marked due; it fails if they are still running after
# `busy_wait`. An earlier run's arm left behind is disarmed first. After the arm it
# waits, up to `ran_wait`, for the put-back job's first run to end: that run comes
# on the gateway's next minute and the audits start on the tick after it, which
# together outlast the verifier's two-minute window. On an image without the job
# there is nothing to wait for.
#
# The teardown, and the exit trap on a failed apply, run disarm.py: both markers
# go back as they were and the `oobe` job comes out if this stack put it there.
# The audits the stage started are left to finish; each writes its ledger issue in
# the install's GitOps repository and posts its summary where it always does.

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
  hermes = "/opt/hermes/.venv/bin/hermes"
  python = "/opt/hermes/.venv/bin/python3"
  # agents/chat/scripts/oobe.py: FIRST_RUN_AUDITS.
  audits     = "compliance-audit obtainability-audit fleet-wide-cost-analysis stockout-prevention"
  arm_b64    = base64encode(file("${path.module}/arm.py"))
  disarm_b64 = base64encode(file("${path.module}/disarm.py"))
  busy_b64   = base64encode(file("${path.module}/in_flight.py"))
  ran_b64    = base64encode(file("${path.module}/oobe_ran.py"))
  # arm.py prints this when the image ships no oobe job.
  no_job   = "ships no oobe job"
  ran_wait = 300
  ran_poll = 15
  # Each audit takes 9-15 minutes on its own (#985); four started together by an
  # earlier repetition finish well inside this.
  busy_wait = 2400
  poll      = 30
  # How long an exec into the agent Deployment waits for a pod when it has none.
  # A pod created in that time is not running yet and fails the exec anyway.
  pod_wait = 5
}

resource "null_resource" "oobe" {
  triggers = {
    host_cluster  = var.host_cluster_name
    host_location = var.host_cluster_location
    host_project  = var.project_id
    namespace     = var.agent_namespace
    deployment    = var.agent_deployment
    container     = var.agent_container
    pod_wait      = local.pod_wait
    home          = local.home
    python        = local.python
    disarm_b64    = local.disarm_b64
  }

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    command     = <<-EOT
      set -euo pipefail

      # Own kubeconfig: by the time this apply runs, an earlier tofu task may
      # have pointed the ambient context at its own cluster.
      kubeconfig_dir="$(mktemp -d)"

      # Terraform taints a resource whose create-time provisioner failed and
      # skips its destroy-time provisioners, so a failure after the arm starts
      # disarms here. errexit stays in force inside a trap, hence `set +e`.
      arming=""
      on_exit() {
        status=$?
        trap '' TERM INT
        set +e
        if [ "$status" -ne 0 ] && [ -n "$arming" ]; then
          echo "Arm failed (exit $status); disarming." >&2
          disarm >&2 || echo "Cleanup incomplete: could not disarm. The next run disarms before it arms." >&2
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

      agent_py() {
        kubectl exec -i -n "${var.agent_namespace}" "deployment/${var.agent_deployment}" \
          -c "${var.agent_container}" --pod-running-timeout=${local.pod_wait}s -- ${local.python} - "$@"
      }
      disarm() {
        printf '%s' '${local.disarm_b64}' | base64 -d | agent_py "${local.home}"
      }
      in_flight() {
        printf '%s' '${local.busy_b64}' | base64 -d | agent_py "${local.home}" ${local.audits}
      }

      # ---- 1. Finish an earlier run's teardown ----------------------------
      disarm

      # ---- 2. Wait for an earlier repetition's audits ---------------------
      # Only a count is used: a failed exec or query prints nothing, and that
      # is not "none running".
      elapsed=0
      until busy="$(in_flight)" && [ "$busy" = 0 ]; do
        if [ "$elapsed" -ge ${local.busy_wait} ]; then
          echo "ERROR: $${busy:-an unreadable count of} first-run audit(s) still running on ${var.host_cluster_name} after $${elapsed}s. An audit in flight is not started again, so this repetition could not be graded." >&2
          exit 1
        fi
        sleep ${local.poll}
        elapsed=$((elapsed + ${local.poll}))
      done

      # ---- 3. Arm ---------------------------------------------------------
      arming=1
      armed="$(printf '%s' '${local.arm_b64}' | base64 -d | agent_py "${local.home}" "${local.hermes}" "$(date -u +%Y%m%d%H%M%S)")"
      printf '%s\n' "$armed"

      # ---- 4. Wait for the job's first run --------------------------------
      # Whether or not it ends in time, the verifier decides; this only keeps
      # its window from opening before the stage has had its turn.
      if [[ "$armed" != *"${local.no_job}"* ]]; then
        elapsed=0
        until ran="$(printf '%s' '${local.ran_b64}' | base64 -d | agent_py "${local.home}")" && [ "$${ran:-0}" -ge 1 ]; do
          if [ "$elapsed" -ge ${local.ran_wait} ]; then
            echo "The oobe job has not finished a run $${elapsed}s after the arm; leaving it to the verifier." >&2
            break
          fi
          sleep ${local.ran_poll}
          elapsed=$((elapsed + ${local.ran_poll}))
        done
      fi
    EOT
  }

  provisioner "local-exec" {
    when        = destroy
    on_failure  = continue
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

      printf '%s' '${self.triggers.disarm_b64}' | base64 -d | \
        kubectl exec -i -n "${self.triggers.namespace}" "deployment/${self.triggers.deployment}" \
        -c "${self.triggers.container}" --pod-running-timeout=${self.triggers.pod_wait}s -- \
        ${self.triggers.python} - "${self.triggers.home}"
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
