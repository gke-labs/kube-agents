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

# The scenario driver for bench/tasks/autoops-missing-service-triage, a copy
# of prebuilt/autoops-crashloop-incident with a different plant. Read that
# stack's header and prebuilt/autoops-incident's step comments first; comments
# here are only where this one differs.
#
# WHERE IT DIFFERS: the container prints that it cannot reach a database
# Service and exits 1. No Service of that name exists in the namespace, so the
# cause is visible in the first minute (the pod spec's command, the container
# log and `kubectl get svc`). The task grades what the triage does after that:
# gke-labs#3078's worker had this cause at minute one and then spent 21
# minutes hunting for the workload's GitOps source.
#
# A separate directory for the reason the sibling gives: devops-bench applies
# a stack in place, so two tasks sharing one directory share one state.

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
  kubectl  = "kubectl --namespace=${var.incident_namespace}"
  ns       = var.incident_namespace
  workload = var.workload_name

  ci_labels = {
    "managed-by"  = "kube-agents-bench"
    "build-id"    = var.prow_build_id != "" ? var.prow_build_id : "local"
    "pull-number" = var.prow_pull_number != "" ? var.prow_pull_number : "none"
  }
}

# The workload: busybox that looks its database Service up before it starts
# and exits 1 when the lookup fails, as a service with a hard dependency does.
# The Service never exists, so it crash-loops; with the Service present it
# would run. The command is therefore not the bug: a triage that blames it
# instead of the missing Service read the surface. (The first version simply
# echoed the error and exited, which read as a fake failure and graded whether
# the model saw through it rather than the triage.) Exits fast, so BackOff accumulates
# quickly; the watcher gate accepts the crash-loop half of the BackOff family
# (k8s-operator/cmd/k8s-event-watcher/filter_test.go — the image-pull half is
# deliberately left alone, which is why this plant crashes at runtime rather
# than failing to pull).
resource "null_resource" "incident" {
  triggers = {
    namespace     = local.ns
    host_cluster  = var.host_cluster_name
    host_location = var.host_cluster_location
    host_project  = var.project_id

    manifest = sha256(join("|", [
      local.ns,
      local.workload,
      var.missing_service_name,
      var.host_cluster_name,
    ]))
  }

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    command     = <<-EOT
      set -euo pipefail

      # ---- 0. Point kubectl at the cluster the watcher is on ----------------
      # Own kubeconfig, own credentials; see prebuilt/autoops-incident step 0
      # for the full chain of why the ambient context cannot be trusted here.
      kubeconfig_dir="$(mktemp -d)"

      # Failure-path cleanup, the incumbent's (prebuilt/autoops-incident,
      # step 0, which argues it in full): a plant that fails after creating
      # the namespace deletes it, so the next run starts clean; guarded on
      # `planted_ns`, which step 1 sets right before the create, so a
      # namespace this run did not make is never deleted from here.
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
      # A Prow deadline delivers SIGTERM, and bash does not run an EXIT trap
      # when the shell dies from an untrapped signal; steps 2 and 3 can hold
      # this script for twelve minutes, so the deadline kill is a real path.
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
      # The incumbent's step 0b, for the incumbent's reason: against a leftover
      # ${local.ns} the apply below reports `unchanged`, the old pod keeps its
      # UID, the watcher dedups {UID, reason} against a window opened hours
      # ago, and step 3 times out with everything looking healthy. Gated on the
      # managed-by label step 1 writes: a namespace of this name we did not
      # plant is a stop, not a target.
      if kubectl get namespace "${local.ns}" >/dev/null 2>&1; then
        leftover_owner="$(kubectl get namespace "${local.ns}" \
          -o jsonpath='{.metadata.labels.managed-by}' 2>/dev/null || true)"
        if [ "$leftover_owner" != "${local.ci_labels["managed-by"]}" ]; then
          echo "ERROR: ${local.ns} already exists on ${var.host_cluster_name} but is not labelled managed-by=${local.ci_labels["managed-by"]} (found '$leftover_owner'). This stack did not create it, so it will not delete it. Remove it by hand if it is stale." >&2
          exit 1
        fi
        echo "Found a leftover ${local.ns} from an earlier run; deleting it before planting."
        if ! kubectl delete namespace "${local.ns}" --ignore-not-found --wait=true --timeout=180s; then
          echo "ERROR: could not clear the leftover ${local.ns} within 180s, so this run cannot plant a fresh pod and the watcher would dedup against the old one. Its current state follows." >&2
          kubectl get namespace "${local.ns}" -o yaml >&2 || true
          exit 1
        fi
      fi

      # Recorded before anything is planted, so the step-3 log poll cannot
      # match a `fire` line left by an earlier run against this namespace name.
      started_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

      # ---- 1. Plant it ------------------------------------------------------
      # Set before the create, not after (the incumbent's rule): a create that
      # half-succeeds still leaves a namespace this run must remove.
      planted_ns=1
      kubectl create namespace "${local.ns}" --dry-run=client -o yaml | kubectl apply -f -
      kubectl label namespace "${local.ns}" --overwrite \
        managed-by="${local.ci_labels["managed-by"]}" \
        build-id="${local.ci_labels["build-id"]}" \
        pull-number="${local.ci_labels["pull-number"]}"

      kubectl apply -f - <<'MANIFEST'
      apiVersion: apps/v1
      kind: Deployment
      metadata:
        name: ${local.workload}
        namespace: ${local.ns}
        labels:
          app: ${local.workload}
      spec:
        replicas: 1
        selector:
          matchLabels:
            app: ${local.workload}
        template:
          metadata:
            labels:
              app: ${local.workload}
          spec:
            automountServiceAccountToken: false
            containers:
              - name: app
                image: busybox:1.36
                command: ["sh", "-c", "nslookup ${var.missing_service_name}.${local.ns}.svc.cluster.local >/dev/null 2>&1 || { echo \"FATAL: cannot reach database at ${var.missing_service_name}.${local.ns}.svc:5432\" >&2; exit 1; }; exec sleep 3600"]
                resources:
                  requests:
                    cpu: 10m
                    memory: 16Mi
                  limits:
                    memory: 32Mi
      MANIFEST

      # ---- 2. Wait for the debounce to clear --------------------------------
      # Same gate as the sibling: the watcher holds the crash-loop family
      # until Event.Count reaches --backoff-min-count (default 3).
      elapsed=0
      until [ "$(${local.kubectl} get events \
            --field-selector reason=BackOff \
            -o jsonpath='{range .items[*]}{.count}{"\n"}{end}' 2>/dev/null \
            | sort -n | tail -1 | grep -E '^[0-9]+$' || echo 0)" -ge 3 ]; do
        if [ "$elapsed" -ge 420 ]; then
          echo "ERROR: no BackOff event on ${local.ns} reached count 3 after $${elapsed}s, so the watcher's leading-edge debounce never cleared and no incident will be raised. The planted workload is not crash-looping as intended." >&2
          ${local.kubectl} get pods -o wide >&2 || true
          ${local.kubectl} describe deployment "${local.workload}" >&2 || true
          ${local.kubectl} get events --sort-by=.lastTimestamp >&2 || true
          exit 1
        fi
        sleep 10
        elapsed=$((elapsed + 10))
      done
      echo "BackOff reached the debounce threshold after $${elapsed}s."

      # ---- 3. Wait for the watcher to open the incident ---------------------
      # Read from the watcher's own log; --since-time so a recycled lease's
      # old 'fire' line for this static namespace name cannot satisfy it.
      elapsed=0
      until kubectl logs "deployment/${var.agent_deployment}" \
              -n "${var.agent_namespace}" -c "${var.agent_container}" \
              --since-time="$started_at" --tail=-1 2>/dev/null \
            | grep -q "fire .*pod=${local.ns}/"; do
        if [ "$elapsed" -ge 300 ]; then
          echo "ERROR: the workload is crash-looping past the debounce, but k8s-event-watcher logged no 'fire' for a pod in ${local.ns} within $${elapsed}s. The incident was detected by Kubernetes and not by the watcher, so nothing downstream will run. Its recent log follows." >&2
          kubectl logs "deployment/${var.agent_deployment}" -n "${var.agent_namespace}" \
            -c "${var.agent_container}" --since-time="$started_at" --tail=200 >&2 || true
          exit 1
        fi
        sleep 10
        elapsed=$((elapsed + 10))
      done
      echo "k8s-event-watcher opened an incident for ${local.ns} after $${elapsed}s."
    EOT
  }

  # Namespace-scoped by design; the destroy fetches its own credentials for
  # the sibling stack's reason (a tainted resource's destroy can run with the
  # ambient context pointed anywhere).
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

      kubectl delete namespace "${self.triggers.namespace}" --ignore-not-found --wait=false
    EOT
  }
}

# Passed straight through: the subject cluster already exists and this stack
# builds none. devops-bench reads these outputs unconditionally after up()
# and re-points the ambient kubeconfig at cluster_name — which is exactly the
# cluster the task's safeguards read.
output "cluster_name" {
  value = var.host_cluster_name
}

output "cluster_location" {
  value = var.host_cluster_location
}
