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

# The scenario driver for bench/tasks/triage-start-burst.
#
# It plants `pod_count` bare pods at once in one namespace on the cluster the
# Platform Agent pod runs in, each pulling an image tag that does not exist, so
# each raises one image-pull Warning. It then waits until k8s-event-watcher has
# logged a `fire` line for every one of them: the daemon answered `injected`
# for each, so each has an admitted ledger row and a triage start queued behind
# it. What the case grades -- whether every one of those starts sent its turn,
# a Hermes session with a message -- is left to the verifier.
#
# It builds nothing, for the reason bench/tf/prebuilt/autoops-incident/main.tf
# gives at length: the host cluster is the only cluster the watcher is sure to
# be watching inside the run. The same file explains the credential fetch, the
# leftover-namespace guard, the exit and TERM traps, the destroy provisioner
# and the two outputs; this stack copies them and does not repeat the argument.

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
  kubectl = "kubectl --namespace=${var.burst_namespace}"
  ns      = var.burst_namespace
  prefix  = var.pod_name_prefix

  ci_labels = {
    "managed-by"  = "kube-agents-bench"
    "build-id"    = var.prow_build_id != "" ? var.prow_build_id : "local"
    "pull-number" = var.prow_pull_number != "" ? var.prow_pull_number : "none"
  }

  # An image tag allows [A-Za-z0-9_.-] only.
  tag_run = replace(local.ci_labels["build-id"], "/[^A-Za-z0-9_.-]/", "-")

  # One tag per pod and per run. The watcher keys dedup on pod UID
  # (k8s-event-watcher/types.go, EventKey), so a shared tag would fire per pod
  # too; distinct tags keep any per-image handling between the kubelet, the
  # registry mirror and the watcher from folding two pods' failures into one.
  # The terminal "not found" a missing tag returns fires on the first event
  # (terminalPullMarkers in k8s-event-watcher/pullfailure.go), so no pod waits
  # out a debounce.
  pods = [for i in range(var.pod_count) : {
    apiVersion = "v1"
    kind       = "Pod"
    metadata = {
      name      = "${local.prefix}-${i}"
      namespace = local.ns
      labels    = { app = local.prefix }
    }
    spec = {
      automountServiceAccountToken  = false
      terminationGracePeriodSeconds = 0
      containers = [{
        name            = "puller"
        image           = "busybox:does-not-exist-${local.tag_run}-${i}"
        imagePullPolicy = "IfNotPresent"
        resources = {
          requests = { cpu = "10m", memory = "16Mi" }
          limits   = { memory = "16Mi" }
        }
      }]
    }
  }]

  # One List, created by one `kubectl create`, so the pods are admitted within
  # a second or two of each other and their Warnings arrive together.
  manifest = jsonencode({ apiVersion = "v1", kind = "List", items = local.pods })
}

resource "null_resource" "burst" {
  triggers = {
    # Destroy-time provisioners may read only `self`.
    namespace     = local.ns
    agent_ns      = var.agent_namespace
    agent_deploy  = var.agent_deployment
    cli_container = var.agent_cli_container
    host_cluster  = var.host_cluster_name
    host_location = var.host_cluster_location
    host_project  = var.project_id
    manifest      = sha256(local.manifest)
  }

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    command     = <<-EOT
      set -euo pipefail

      # ---- 0. Point kubectl at the cluster the watcher is on ----------------
      kubeconfig_dir="$(mktemp -d)"

      # Clean up a namespace this run made when the plant fails: Terraform
      # taints the resource and skips the destroy provisioner, so nothing else
      # would. Guarded on planted_ns so a namespace step 0b refused is kept.
      planted_ns=""
      on_exit() {
        status=$?
        set +e
        if [ "$status" -ne 0 ] && [ -n "$planted_ns" ]; then
          echo "Plant failed (exit $status). State of ${local.ns} before cleanup:" >&2
          ${local.kubectl} get pods -o wide >&2
          ${local.kubectl} get events --sort-by=.lastTimestamp >&2
          echo "Deleting ${local.ns} so the next run starts from a clean namespace." >&2
          kubectl delete namespace "${local.ns}" --ignore-not-found --wait=false >&2
        fi
        rm -rf "$kubeconfig_dir"
      }
      trap on_exit EXIT
      # A Prow deadline sends SIGTERM, and bash runs no EXIT trap on an
      # untrapped signal.
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

      # ---- 0b. Clear what an earlier run left behind ------------------------
      # Only a namespace this stack labelled: it runs on the shared cluster the
      # install lives on.
      if kubectl get namespace "${local.ns}" >/dev/null 2>&1; then
        leftover_owner="$(kubectl get namespace "${local.ns}" \
          -o jsonpath='{.metadata.labels.managed-by}' 2>/dev/null || true)"
        if [ "$leftover_owner" != "${local.ci_labels["managed-by"]}" ]; then
          echo "ERROR: ${local.ns} already exists on ${var.host_cluster_name} but is not labelled managed-by=${local.ci_labels["managed-by"]} (found '$leftover_owner'). This stack did not create it, so it will not delete it. Remove it by hand if it is stale." >&2
          exit 1
        fi
        echo "Found a leftover ${local.ns} from an earlier run; deleting it before planting."
        if ! kubectl delete namespace "${local.ns}" --ignore-not-found --wait=true --timeout=180s; then
          echo "ERROR: could not clear the leftover ${local.ns} within 180s. Its current state follows." >&2
          kubectl get namespace "${local.ns}" -o yaml >&2 || true
          exit 1
        fi
      fi

      # Taken before the plant, so the log read below cannot count a fire line
      # an earlier run left for a pod of the same name.
      started_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

      # ---- 1. Plant the burst -----------------------------------------------
      planted_ns=1
      kubectl create namespace "${local.ns}" --dry-run=client -o yaml | kubectl apply -f -
      kubectl label namespace "${local.ns}" --overwrite \
        managed-by="${local.ci_labels["managed-by"]}" \
        build-id="${local.ci_labels["build-id"]}" \
        pull-number="${local.ci_labels["pull-number"]}"

      kubectl create -f - <<'MANIFEST'
      ${local.manifest}
      MANIFEST
      echo "Planted ${var.pod_count} pods in ${local.ns}."

      # ---- 2. Wait for the watcher to fire for every pod --------------------
      # The same log source and the same since-time read as autoops-incident's
      # step 3. A `fire` line is written only after the daemon answered
      # `injected` (Dispatch in k8s-event-watcher/main.go), which it does after
      # writing the alert's admitted ledger row and queueing its triage start.
      # Counted by distinct pod name, so a pod whose alert was re-offered and
      # fired twice is not counted twice. The leading space keeps the dry-run
      # `would-fire` line out.
      watcher_log() {
        kubectl logs "deployment/${var.agent_deployment}" \
          -n "${var.agent_namespace}" -c "${var.agent_container}" \
          --since-time="$started_at" --tail=-1 2>/dev/null
      }
      elapsed=0
      while :; do
        log="$(watcher_log || true)"
        fired="$(printf '%s\n' "$log" \
          | grep -oE "(^| )fire [^ ]+ pod=${local.ns}/${local.prefix}-[0-9]+ " \
          | grep -oE "pod=${local.ns}/${local.prefix}-[0-9]+" | sort -u | wc -l | tr -d ' ' || true)"
        if [ "$${fired:-0}" -ge "${var.pod_count}" ]; then
          break
        fi
        # A refused alert never fires, so waiting out the timeout would only
        # delay the same answer. The daemon refuses when the day's Warning
        # alert limit is spent (ALERT_DAILY_LIMIT_WARNING, default 5 a UTC
        # day); hack/ci-deploy.sh turns it off for eval installs.
        if printf '%s\n' "$log" | grep -qE "quota-suppressed [^ ]+ pod=${local.ns}/"; then
          echo "ERROR: the daemon refused alerts for pods in ${local.ns} against its daily Warning alert limit ($${fired:-0} of ${var.pod_count} fired). This case needs ALERT_DAILY_LIMIT_WARNING set to 0 (off) or to at least ${var.pod_count} plus what the install has spent today. The refusals follow." >&2
          printf '%s\n' "$log" | grep -E "quota-suppressed [^ ]+ pod=${local.ns}/" >&2 || true
          exit 1
        fi
        if [ "$elapsed" -ge "${var.fire_wait_seconds}" ]; then
          echo "ERROR: k8s-event-watcher logged a fire line for $${fired:-0} of the ${var.pod_count} pods planted in ${local.ns} within $${elapsed}s. The burst did not reach the daemon whole. Its recent log follows." >&2
          printf '%s\n' "$log" | tail -200 >&2
          exit 1
        fi
        sleep 10
        elapsed=$((elapsed + 10))
      done
      echo "k8s-event-watcher fired for all ${var.pod_count} pods in ${local.ns} after $${elapsed}s."

      # ---- Let the starts finish ---------------------------------------------
      # A fire means the daemon admitted the alert and queued its start; the
      # turn itself can begin minutes later, because the daemon starts a few at
      # a time. The wait is the product's own queue limit, so a start the
      # product calls on time is graded as started; the objective converges for
      # only 120 s after the agent's turn, so the wait is here. A fixed wait,
      # not a poll of the start: what started is the check's question, not the
      # plant's.
      echo "Waiting ${var.start_settle_seconds}s for the triage starts to finish."
      sleep ${var.start_settle_seconds}
    EOT
  }

  # The success-path teardown. The failure path is the exit trap above.
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

      # The burst's triage cards run on for minutes after the check, and the
      # next case would share the gateway with them: archive the unfinished
      # ones. Best effort; the namespace delete below runs either way.
      kubectl exec -n "${self.triggers.agent_ns}" "deployment/${self.triggers.agent_deploy}" \
        -c "${self.triggers.cli_container}" -- sh -c \
        'for t in $(hermes kanban list 2>/dev/null | grep "${self.triggers.namespace}/" | grep -E " (todo|ready|running|triage) " | grep -o "t_[0-9a-f]*"); do hermes kanban reclaim "$t" >/dev/null 2>&1; hermes kanban archive "$t" >/dev/null 2>&1; done' \
        || echo "Could not archive the burst's cards; they finish on their own." >&2

      kubectl delete namespace "${self.triggers.namespace}" --ignore-not-found --wait=false
    EOT
  }
}

# Passed straight through; devops-bench requires both (autoops-incident's
# header says why).
output "cluster_name" {
  value = var.host_cluster_name
}

output "cluster_location" {
  value = var.host_cluster_location
}
