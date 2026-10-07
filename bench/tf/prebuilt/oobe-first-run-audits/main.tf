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
# Before it arms, the apply waits for the install's own first-run stage to finish
# (a fresh install has the job pending until its own scan settles), failing after
# `own_wait`. It then waits until none of the four audits is still running
# from an earlier repetition, since an audit already in flight is not started
# again when it is marked due; it fails if they are still running after
# `busy_wait`. An earlier run's arm left behind is disarmed first. After the arm it
# waits, up to `chain_wait`, for the stage to finish: it marks the audits one after
# another, which outlasts the verifier's two-minute window. On an image without the
# job there is nothing to wait for.
#
# The teardown, and the exit trap on a failed apply, run disarm.py: both markers
# go back as they were and the `oobe` job comes out if this stack put it there.
# The teardown then waits, up to `busy_wait`, for the audits the stage started
# to finish, marked ones not yet claimed included: the runner holds their four streams' locks until devops-bench returns
# (the case's `audit_streams`), so an audit case on one of them never runs beside
# them. Each writes its ledger issue in the install's GitOps repository and posts
# its summary where it always does.

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
  audits     = "fleet-wide-cost-analysis compliance-audit obtainability-audit stockout-prevention"
  arm_b64    = base64encode(file("${path.module}/arm.py"))
  disarm_b64 = base64encode(file("${path.module}/disarm.py"))
  busy_b64   = base64encode(file("${path.module}/in_flight.py"))
  own_b64    = base64encode(file("${path.module}/own_stage.py"))
  # One infra-lock deadline (hack/ci-eval-pr.sh): a fresh install's own scan usually settles
  # inside it, and holding the lock longer stalls every other stack-bearing case.
  own_wait = 1800
  # arm.py prints this when the image ships no oobe job.
  no_job = "ships no oobe job"
  # The chain runs the four audits one after another (1-15 minutes each), and the stage is
  # done once the last has started.
  chain_wait = 3600
  # Each audit takes 9-15 minutes on its own (#985), and the chain runs one at a time, so
  # the one still going when the chain is done finishes well inside this.
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
    hermes        = local.hermes
    disarm_b64    = local.disarm_b64
    busy_b64      = local.busy_b64
    audits        = local.audits
    busy_wait     = local.busy_wait
    poll          = local.poll
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
        printf '%s' '${local.disarm_b64}' | base64 -d | agent_py "${local.home}" "${local.hermes}"
      }
      in_flight() {
        printf '%s' '${local.busy_b64}' | base64 -d | agent_py "${local.home}" ${local.audits}
      }

      # ---- 1. Finish an earlier run's teardown ----------------------------
      disarm

      # ---- 2. Wait for the install's own first-run stage ------------------
      # Arming over it would point the job at the stand-in cards, start the
      # audits beside the real scan and use up the install's own first run.
      elapsed=0
      until own="$(printf '%s' '${local.own_b64}' | base64 -d | agent_py "${local.home}")" && [ "$own" = clear ]; do
        if [ "$elapsed" -ge ${local.own_wait} ]; then
          echo "ERROR: the install's own first-run stage on ${var.host_cluster_name} is still $${own:-unreadable} after $${elapsed}s: its onboarding scan has not settled, and this case would cut across it." >&2
          exit 1
        fi
        sleep ${local.poll}
        elapsed=$((elapsed + ${local.poll}))
      done

      # ---- 3. Wait for an earlier repetition's audits ---------------------
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

      # ---- 4. Arm ---------------------------------------------------------
      arming=1
      armed="$(printf '%s' '${local.arm_b64}' | base64 -d | agent_py "${local.home}" "${local.hermes}" "$(date -u +%Y%m%d%H%M%S)")"
      printf '%s\n' "$armed"

      # ---- 5. Wait for the chain --------------------------------------------
      # The stage marks the four audits one after another and is done once the last
      # has started. Whether or not it gets there in time, the verifier decides; this
      # only keeps its two-minute window from opening before the chain has run.
      if [[ "$armed" != *"${local.no_job}"* ]]; then
        elapsed=0
        until own="$(printf '%s' '${local.own_b64}' | base64 -d | agent_py "${local.home}")" && [ "$own" = clear ]; do
          if [ "$elapsed" -ge ${local.chain_wait} ]; then
            echo "The oobe chain has not finished $${elapsed}s after the arm; leaving it to the verifier." >&2
            break
          fi
          sleep ${local.poll}
          elapsed=$((elapsed + ${local.poll}))
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

      agent_py() {
        kubectl exec -i -n "${self.triggers.namespace}" "deployment/${self.triggers.deployment}" \
          -c "${self.triggers.container}" --pod-running-timeout=${self.triggers.pod_wait}s -- \
          ${self.triggers.python} - "$@"
      }

      # Disarmed first, so the oobe job marks nothing more; then the runner,
      # which releases the four streams' locks once this returns, waits for
      # the audit still going, or marked and not yet claimed, to end. A count
      # that cannot be read is not "none running"; past the bound it stops
      # waiting.
      # A failed disarm still waits: with the stage armed, the count below
      # includes what it goes on marking, and the wait is bounded either way.
      printf '%s' '${self.triggers.disarm_b64}' | base64 -d | agent_py "${self.triggers.home}" "${self.triggers.hermes}" \
        || echo "WARNING: could not disarm the oobe stage; waiting for its audits anyway." >&2

      elapsed=0
      until busy="$(printf '%s' '${self.triggers.busy_b64}' | base64 -d | agent_py "${self.triggers.home}" ${self.triggers.audits})" && [ "$busy" = 0 ]; do
        if [ "$elapsed" -ge ${self.triggers.busy_wait} ]; then
          echo "WARNING: $${busy:-an unreadable count of} first-run audit(s) still running after $${elapsed}s; releasing the locks anyway." >&2
          break
        fi
        sleep ${self.triggers.poll}
        elapsed=$((elapsed + ${self.triggers.poll}))
      done
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
