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

# Fire one run of a built-in AgentSchedule (var.schedule) before the turn, and
# wait it out, so a case can grade what that run did. The AgentSchedule
# counterpart of prebuilt/audit-chat-report, which fires a Hermes cron job.
#
# run_now.py has the steps: wait for any run of the schedule in flight to
# settle, record the fire time in the state file below (the gateway pod's
# clock, read before the fire), set the schedule's
# `kubeagents.x-k8s.io/run-now` annotation to a fresh token, wait for the run
# it started to end and for the gateway to say how its report was delivered,
# and record the task, its outcome and its delivery in the state file.
#
# What a case reads: `since_state_file` on audit_report_stored,
# shadow_report_stored, findings_queue_untouched and schedule_report_not_posted
# takes the state file's `fired_at` as its lower bound; the schedule's
# status.lastOutcome is the fired run's, since nothing else runs it before the
# turn is graded.
#
# Access: kubectl get and annotate on the schedule, and kubectl exec into the
# gateway's agent container for the clock, the state file and the findings
# queue snapshot (the Session KV route, with the container's own key, as the
# findings checks read it), with the runner's credentials for the host
# cluster. No Secret is read.
#
# Teardown: a failed apply (a wait that timed out, a SIGTERM) first stops the
# fired run if it is still in flight, with the schedule's
# `kubeagents.x-k8s.io/stop` annotation, then removes the state file and the
# run-now annotation before it exits (Terraform taints the resource and skips
# its destroy provisioner); the destroy removes both on the success path. The
# run-now annotation is removed only while it holds a token run_now.py set.

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
  # Read by the store and side-effect checks as `since_state_file`.
  state_file = "/opt/data/.bench-builtin-schedule-run-now.json"
  # The script inlined, so the destroy runs the copy the apply ran, as
  # audit-chat-report carries arm.py.
  run_now_b64 = base64encode(file("${path.module}/run_now.py"))
}

resource "null_resource" "run_now" {
  triggers = {
    host_cluster  = var.host_cluster_name
    host_location = var.host_cluster_location
    host_project  = var.project_id
    namespace     = var.agent_namespace
    deployment    = var.agent_deployment
    container     = var.agent_container
    schedule      = var.schedule
    state_file    = local.state_file
    run_now_b64   = local.run_now_b64
  }

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    command     = <<-EOT
      set -euo pipefail
      kubeconfig_dir="$(mktemp -d)"
      printf '%s' '${local.run_now_b64}' | base64 -d > "$kubeconfig_dir/run_now.py"
      run_now() {
        python3 "$kubeconfig_dir/run_now.py" --namespace "${var.agent_namespace}" \
          --deployment "${var.agent_deployment}" --container "${var.agent_container}" "$@"
      }
      on_exit() {
        status=$?
        trap '' TERM INT
        set +e
        if [ "$status" -ne 0 ]; then
          echo "Apply failed (exit $status); undoing what it did." >&2
          run_now clean --stop-run "${var.schedule}" "${local.state_file}" >&2 \
            || echo "Could not clean up; the next apply's fire overwrites the state file and the annotation." >&2
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

      run_now idle "${var.schedule}" "${var.idle_wait}"
      run_now fire "${var.schedule}" "${local.state_file}"
      run_now wait "${var.schedule}" "${local.state_file}" "${var.run_wait}" "${var.delivery_wait}"
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
      printf '%s' '${self.triggers.run_now_b64}' | base64 -d > "$kubeconfig_dir/run_now.py"

      project="${self.triggers.host_project}"
      if [ -z "$project" ]; then
        project="$(gcloud config get-value project 2>/dev/null || true)"
      fi
      gcloud container clusters get-credentials "${self.triggers.host_cluster}" \
        --location "${self.triggers.host_location}" --project "$project" --quiet \
        || echo "WARNING: could not fetch credentials for ${self.triggers.host_cluster}." >&2

      python3 "$kubeconfig_dir/run_now.py" --namespace "${self.triggers.namespace}" \
        --deployment "${self.triggers.deployment}" --container "${self.triggers.container}" \
        clean "${self.triggers.schedule}" "${self.triggers.state_file}" \
        || echo "WARNING: could not remove the state file or the annotation; the next apply overwrites both." >&2
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
