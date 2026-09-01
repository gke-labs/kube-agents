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

# The scenario driver for bench/tasks/autoops-crashloop-config-triage — the
# sibling of prebuilt/autoops-incident, and everything structural here is
# that stack's pattern: the incident must happen on the cluster the Platform
# Agent pod runs in (k8s-event-watcher reads events --in-cluster and sees no
# other cluster), the stack fetches its own credentials because the ambient
# context is some earlier tofu task's cluster by the time this apply runs,
# and it does not return until the watcher has demonstrably opened the
# incident. Read that stack's header and step comments first; comments here
# are only where the two differ.
#
# WHERE THEY DIFFER: the failure class. The sibling plants an OOM kill — the
# container allocates past its memory limit. This one plants a STARTUP
# CONFIG failure: the container's only command reads a file that does not
# exist, exits 1, and crash-loops. Both surface as the same kubelet Warning
# (`BackOff`, "Back-off restarting failed container"), so the watcher
# pipeline is identical — what differs is everything AFTER the event: the
# evidence lives in the container log (the failing path is printed there and
# nowhere else) and in the pod spec's command, not in
# lastState.terminated.reason, and the correct triage names a missing file
# rather than a memory limit. That gap is what the task grades: a triage
# tuned to "crashloop means OOM" cannot produce the planted path.
#
# A separate DIRECTORY rather than a variable on the sibling: devops-bench
# applies a stack in place, so two tasks sharing one directory would share
# one Terraform state, and the second task's apply would adopt — and its
# destroy would tear down — the first task's objects. Under the fan-out the
# two tasks may run in one presubmit; the infra mutex serializes their
# applies and the namespaces are distinct, so their incidents and cards
# cannot be confused.

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

# The workload: busybox whose only command reads a path nothing mounts. `cat`
# prints "can't open '<path>'" to stderr and exits 1, so the failing path —
# the planted evidence noun — is in the container log and in the pod spec's
# command, and nowhere in any event. Exits fast, so BackOff accumulates
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
      var.missing_config_path,
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
      trap 'rm -rf "$kubeconfig_dir"' EXIT
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

      started_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

      # ---- 1. Plant it ------------------------------------------------------
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
                command: ["sh", "-c", "cat ${var.missing_config_path} && sleep 3600"]
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
