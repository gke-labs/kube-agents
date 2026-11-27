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

# The scenario driver for two cases: run one scheduled fleet audit for real.
# bench/tasks/fleet-audit-chat-report-is-the-report reads what the chat relay
# stored for the run's report, which is what the channel got;
# bench/tasks/fleet-audit-scheduled-run-files-the-finding reads the report the
# run stored on the install.
#
# The run must be a full one that speaks: a silent run sends the channel
# nothing to grade, and a run closed from a stored document reports what an
# earlier run found. Before it fires, the stack renames files on the shell
# sandbox (to `<name>.bench-moved`). Each `latest.json` in the stream's report
# store: with no trusted memory of the previous run, `finish` answers
# `silent_ok: false` whatever the fleet holds (the fleet-audit skill's
# references/clean-run.md). And the digest record the last scheduled run
# left: without it `precheck` reads a first run and closes nothing, where
# with it an unchanged fleet is closed from the newest stored document, which
# the run ring keeps when `latest.json` is gone. A run that reaches `finish`
# writes a new `latest.json`, and a new digest record when it had a collector
# manifest; the renamed copy of each file the run wrote anew is deleted, and
# the teardown, or the exit trap on a failed apply, puts back the copy of
# each it did not (store.sh), so the stream keeps its memory.
#
# Where the report store's files are depends on FLEET_AUDIT_REPORT_STORE, which
# the operator renders on the sandbox's shell container and which is read here
# from the pod spec (report_store). Unset, empty or `file`: on the sandbox's own
# volume beside the digest record, and store.sh runs once, in the sandbox.
# `broker` (#6110): the shell writes through the credential broker, whose files
# are under the same root on the gateway pod's data volume, so store.sh renames
# each `latest.json` in the agent container and the digest record, which stays
# in the sandbox's scratch, in the sandbox. Before that, seed.py has the broker
# copy in the sandbox's local copy of each stream, which the scripts would
# otherwise do on their first touch and so bring the renamed `latest.json`
# back. Any other value fails the apply before it changes anything, and the
# destroy before it puts anything back.
#
# arm.py (beside this file) marks the job due with Hermes' own `trigger_job`, as
# the `oobe` job's first-run audits stage does, and waits for the run to end. The
# profile's cron tick claims the mark and the scheduler delivers the run through
# the job's `deliver` leg, so the report takes the path every scheduled report
# takes. A run of the job already claimed or running is waited out first, up to
# `own_wait`, and only a run claimed after the mark is read as the fired one
# (both from the scheduler's own `cron/executions.db`). An eval run pauses the
# clock-started audits: the fire lifts that pause, and the wait, the teardown
# and the exit trap on a failed apply each put it back.
#
# On an install with a chat platform the report is posted to the home channel,
# and the audit writes its ledger issue in the install's GitOps repository.
#
# The teardown pauses the job again where that is still owed, removes the
# state file, and restores or deletes the renamed files as above.

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
  seed_b64  = base64encode(file("${path.module}/seed.py"))
  # audit_report.py: REPORTS_DIR.
  store = "/opt/data/fleet-audit/reports/${var.audit_job}"
  # manifest_delta.py: SCRATCH_DIR and DIGEST_FILE_TEMPLATE.
  digest   = "/opt/data/scratch/last_scheduled_digest_${var.audit_job}.json"
  pod_wait = 5
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
    digest            = local.digest
    arm_b64           = local.arm_b64
    store_b64         = local.store_b64
    seed_b64          = local.seed_b64
  }

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    command     = <<-EOT
      set -euo pipefail

      kubeconfig_dir="$(mktemp -d)"
      # Terraform taints a resource whose create-time provisioner failed and
      # skips its destroy-time provisioners, so a failure after the rename puts
      # the store back here, and a failure after the fire pauses the job again
      # where the fire lifted an eval run's pause. errexit stays in force
      # inside a trap, hence `set +e`.
      moved=""
      fired=""
      store_kind=""
      on_exit() {
        status=$?
        trap '' TERM INT
        set +e
        if [ "$status" -ne 0 ] && [ -n "$fired" ]; then
          arm clean "${local.home}" >&2 || echo "WARNING: could not clean the arm state; the next apply removes it first." >&2
        fi
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
      # `file` or `broker`, from the env the operator rendered on the sandbox's
      # shell container; any other value is an error that names it.
      report_store() {
        local out value
        # The value on the first line; a valueFrom on the entry or any envFrom on
        # the container after it, which the spec does not resolve.
        out="$(kubectl get pod -n "${var.agent_namespace}" "${var.sandbox_pod}" \
          -o jsonpath='{.spec.containers[?(@.name=="${var.sandbox_container}")].env[?(@.name=="FLEET_AUDIT_REPORT_STORE")].value}{"\n"}{.spec.containers[?(@.name=="${var.sandbox_container}")].env[?(@.name=="FLEET_AUDIT_REPORT_STORE")].valueFrom}{.spec.containers[?(@.name=="${var.sandbox_container}")].envFrom}')" || return 1
        value="$(printf '%s\n' "$out" | sed -n 1p)"
        if [ -n "$(printf '%s\n' "$out" | sed -n '2,$p')" ]; then
          echo "ERROR: FLEET_AUDIT_REPORT_STORE on the ${var.sandbox_container} container of ${var.sandbox_pod} is set indirectly (valueFrom or envFrom), so which report store to use is unknown." >&2
          return 1
        fi
        case "$value" in
          "" | file) echo file ;;
          broker) echo broker ;;
          *)
            echo "ERROR: FLEET_AUDIT_REPORT_STORE='$value' on the ${var.sandbox_container} container of ${var.sandbox_pod} is neither 'file' nor 'broker', so which report store to use is unknown." >&2
            return 1
            ;;
        esac
      }
      store() {
        if [ "$store_kind" != broker ]; then
          printf '%s' '${local.store_b64}' | base64 -d | \
            kubectl exec -i -n "${var.agent_namespace}" "${var.sandbox_pod}" -c "${var.sandbox_container}" -- \
              sh -s "$1" "${local.store}" "${local.digest}"
          return
        fi
        # Both halves, even when the first fails, so a restore puts back all it can.
        local status=0
        printf '%s' '${local.store_b64}' | base64 -d | \
          kubectl exec -i -n "${var.agent_namespace}" "deployment/${var.agent_deployment}" \
            -c "${var.agent_container}" --pod-running-timeout=${local.pod_wait}s -- \
            sh -s "$1" "${local.store}" "" || status=1
        printf '%s' '${local.store_b64}' | base64 -d | \
          kubectl exec -i -n "${var.agent_namespace}" "${var.sandbox_pod}" -c "${var.sandbox_container}" -- \
            sh -s "$1" "" "${local.digest}" || status=1
        return "$status"
      }
      seed() {
        printf '%s' '${local.seed_b64}' | base64 -d | \
          kubectl exec -i -n "${var.agent_namespace}" "${var.sandbox_pod}" -c "${var.sandbox_container}" -- \
            python3 - "${var.audit_job}" "${local.store}"
      }

      store_kind="$(report_store)"

      arm clean "${local.home}"

      arm idle "${local.home}" "${var.audit_job}" "${var.own_wait}"

      if [ "$store_kind" = broker ]; then
        seed
      fi
      moved=1
      store move

      fired=1
      arm fire "${local.home}" "${local.python}" "${var.audit_job}"
      arm wait "${local.home}" "${var.audit_job}" "${var.run_wait}" ${var.unfinished_run_is_a_result ? "result" : ""}
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

      # As in the apply: `file` or `broker` from the shell container's env in the
      # sandbox pod's spec. Any other value, one set indirectly (a valueFrom on the
      # entry, or any envFrom on the container), or a spec that cannot be read,
      # puts nothing back rather than guess where the renamed files are.
      out="$(kubectl get pod -n "${self.triggers.namespace}" "${self.triggers.sandbox_pod}" \
        -o jsonpath='{.spec.containers[?(@.name=="${self.triggers.sandbox_container}")].env[?(@.name=="FLEET_AUDIT_REPORT_STORE")].value}{"\n"}{.spec.containers[?(@.name=="${self.triggers.sandbox_container}")].env[?(@.name=="FLEET_AUDIT_REPORT_STORE")].valueFrom}{.spec.containers[?(@.name=="${self.triggers.sandbox_container}")].envFrom}')" \
        || { echo "ERROR: could not read the spec of ${self.triggers.sandbox_pod}; the renamed store files stay renamed." >&2; exit 1; }
      store_kind="$(printf '%s\n' "$out" | sed -n 1p)"
      if [ -n "$(printf '%s\n' "$out" | sed -n '2,$p')" ]; then
        echo "ERROR: FLEET_AUDIT_REPORT_STORE on the ${self.triggers.sandbox_container} container of ${self.triggers.sandbox_pod} is set indirectly (valueFrom or envFrom), so which report store to use is unknown; the renamed store files stay renamed." >&2
        exit 1
      fi
      case "$store_kind" in
        "" | file)
          printf '%s' '${self.triggers.store_b64}' | base64 -d \
            | kubectl exec -i -n "${self.triggers.namespace}" "${self.triggers.sandbox_pod}" -c "${self.triggers.sandbox_container}" -- \
              sh -s restore "${self.triggers.store}" "${self.triggers.digest}" \
            || echo "WARNING: could not restore the renamed store files." >&2
          ;;
        broker)
          printf '%s' '${self.triggers.store_b64}' | base64 -d \
            | kubectl exec -i -n "${self.triggers.namespace}" "deployment/${self.triggers.deployment}" \
              -c "${self.triggers.container}" --pod-running-timeout=${self.triggers.pod_wait}s -- \
              sh -s restore "${self.triggers.store}" "" \
            || echo "WARNING: could not restore the renamed store files." >&2
          printf '%s' '${self.triggers.store_b64}' | base64 -d \
            | kubectl exec -i -n "${self.triggers.namespace}" "${self.triggers.sandbox_pod}" -c "${self.triggers.sandbox_container}" -- \
              sh -s restore "" "${self.triggers.digest}" \
            || echo "WARNING: could not restore the renamed digest record." >&2
          ;;
        *)
          echo "ERROR: FLEET_AUDIT_REPORT_STORE='$store_kind' on the ${self.triggers.sandbox_container} container of ${self.triggers.sandbox_pod} is neither 'file' nor 'broker', so which report store to use is unknown; the renamed store files stay renamed." >&2
          exit 1
          ;;
      esac
    EOT
  }
}

output "cluster_name" {
  value = var.host_cluster_name
}

output "cluster_location" {
  value = var.host_cluster_location
}
