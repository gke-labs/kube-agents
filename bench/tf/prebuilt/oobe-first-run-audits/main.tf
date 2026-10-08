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
# (a fresh install has the job until its scan settles, its own chain has started
# the last audit and the next tick has removed it), failing after `own_wait`. An earlier run's arm left behind is disarmed first. After the arm it
# waits, up to `chain_wait`, for the stage to finish: it marks the audits one after
# another, which outlasts the verifier's two-minute window. On an image without the
# job there is nothing to wait for.
#
# Waiting on running audits is the runner's job, not this stack's: it holds the
# four streams' locks for the unit (the case's `audit_streams`), and every unit on
# one of them, the next repetition of this case included, first waits, up to its
# bound, for a run the install started there or a pending stage has still to start
# (hack/ci-eval-pr.sh: wait_platform_runs). Run by hand, without the runner, the
# stage itself holds its first mark until an earlier repetition's audit has ended.
#
# The teardown, and the exit trap on a failed apply, run disarm.py: both markers
# go back as they were and the `oobe` job comes out if this stack put it there.
# The audits the stage started are left to finish. Each writes its ledger issue in
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
  home       = "/opt/data"
  hermes     = "/opt/hermes/.venv/bin/hermes"
  python     = "/opt/hermes/.venv/bin/python3"
  arm_b64    = base64encode(file("${path.module}/arm.py"))
  disarm_b64 = base64encode(file("${path.module}/disarm.py"))
  own_b64    = base64encode(file("${path.module}/own_stage.py"))
  # A fresh CI install's scan settles about 17 minutes after boot, then its chain runs three
  # audits and claims the fourth (one to 45 minutes each). Held under the infra lock, which
  # gives each stack-bearing case 30 minutes per contender, so a longer wait starves them.
  own_wait = 3600
  # arm.py prints this when the image ships no oobe job.
  no_job = "ships no oobe job"
  # The chain runs the four audits one after another (1-15 minutes each), and the stage is
  # done once the last has started.
  chain_wait = 3600
  poll       = 30
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

      # ---- 1. Finish an earlier run's teardown ----------------------------
      disarm

      # ---- 2. Wait for the install's own first-run stage ------------------
      # Arming over it would point the job at the stand-in cards, start the
      # audits beside the real scan and use up the install's own first run.
      elapsed=0
      until own="$(printf '%s' '${local.own_b64}' | base64 -d | agent_py "${local.home}")" && [ "$own" = clear ]; do
        if [ "$elapsed" -ge ${local.own_wait} ]; then
          echo "ERROR: the install's own first-run stage on ${var.host_cluster_name} is still $${own:-unreadable} after $${elapsed}s: its onboarding scan or its own first-run chain has not finished, and this case would cut across it." >&2
          exit 1
        fi
        sleep ${local.poll}
        elapsed=$((elapsed + ${local.poll}))
      done

      # ---- 3. Arm ---------------------------------------------------------
      arming=1
      armed="$(printf '%s' '${local.arm_b64}' | base64 -d | agent_py "${local.home}" "${local.hermes}" "$(date -u +%Y%m%d%H%M%S)")"
      printf '%s\n' "$armed"

      # ---- 4. Wait for the chain --------------------------------------------
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
    # Best effort, with no errexit: each step that fails says so and the next still
    # runs. A disarm that cannot run is finished by the next apply's step 1.
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
        || echo "WARNING: could not fetch credentials for ${self.triggers.host_cluster}; the disarm below will likely fail too." >&2

      printf '%s' '${self.triggers.disarm_b64}' | base64 -d \
        | kubectl exec -i -n "${self.triggers.namespace}" "deployment/${self.triggers.deployment}" \
          -c "${self.triggers.container}" --pod-running-timeout=${self.triggers.pod_wait}s -- \
          ${self.triggers.python} - "${self.triggers.home}" "${self.triggers.hermes}" \
        || echo "WARNING: could not disarm the oobe stage on ${self.triggers.host_cluster}; the next run of this case disarms it before it arms." >&2
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
