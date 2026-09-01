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

# The scenario driver for bench/tasks/autoops-warning-event-triage.
#
# Every other prebuilt stack here builds a cluster and plants something on it.
# This one builds nothing. The incident has to happen on the cluster the
# Platform Agent pod is running on, because that is the only cluster
# k8s-event-watcher is guaranteed to be watching while the case runs.
#
# It is not a single-cluster watcher. It runs as a peer process inside that
# pod's envoy-credential-proxy container, started by
# deploy/shared/start-services.sh with BOTH --in-cluster and --profiles-dir,
# and those sources are additive: the host cluster, plus every Cluster Agent
# profile on the PVC naming a cluster the direct entry does not already cover
# (#497; see the README of k8s-operator/cmd/k8s-event-watcher, Section 4,
# "Option C: Multi-Cluster Fan-In").
#
# What makes the host cluster the only usable target here is timing, twice
# over. A per-run task cluster gets a Cluster Agent profile only from the
# hourly cluster-agent-reconcile tick (agents/chat/defaults/cron/jobs.json),
# and the watcher reads that directory once, at startup, so a profile written
# mid-run is picked up only from the watcher process's next start -- a pod
# restart, or a crash the start-services.sh supervisor loop restarts it from.
# An incident on a per-run task cluster therefore goes undetected unless both
# of those land inside the run, which is a race rather than a design, and one
# the case loses on almost every run while waiting out its timeout for a card
# nobody filed.
#
# So this stack takes the host cluster as an input, applies one Deployment into
# one namespace on it, waits until the pipeline has demonstrably started, and
# deletes the namespace on destroy. `teardown: true` in the task file is what
# runs that destroy.
#
# WHY THE OUTPUTS EXIST even though nothing is provisioned: devops-bench calls
# deployer.get_cluster_info() unconditionally after up() for any non-noop
# deployer (evalharness/default.py), and TFDeployer's implementation reads the
# `cluster_name` and `cluster_location` outputs and hands them to
# GCPProvider.ensure_cluster_credentials, which shells out to `gcloud container
# clusters get-credentials` with check=True. Omitting them raises ConfigError.
# Here that call is not just tolerated but wanted: it leaves the ambient
# kubeconfig pointed at the host cluster, which is the cluster the task's
# safeguards read.

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
  # Every kubectl below runs against a kubeconfig this stack fetches for itself
  # in step 0, never the ambient one. The ambient current-context is the wrong
  # cluster by the time this apply runs -- see step 0 for the chain -- and
  # writing to a file of our own also leaves it undisturbed for whatever runs
  # next, which is the idiom hack/ci-eval-pr.sh already uses for the seeded
  # fleet's per-role kubeconfigs.
  kubectl = "kubectl --namespace=${var.incident_namespace}"

  # Kept in one place because the poll, the manifest and the teardown all name
  # them, and a rename that reaches two of the three fails as a timeout rather
  # than as an error.
  ns       = var.incident_namespace
  workload = var.workload_name

  ci_labels = {
    "managed-by"  = "kube-agents-bench"
    "build-id"    = var.prow_build_id != "" ? var.prow_build_id : "local"
    "pull-number" = var.prow_pull_number != "" ? var.prow_pull_number : "none"
  }
}

# The workload. busybox running one bounded `dd` allocation that does not fit
# the container's memory limit, which is the same shape as the seeded fleet's
# payments-api crashloop fixture (bench/tf/fleet/defects-a.tf) and is
# deliberately NOT that fixture: cluster-agent-crashloop-debug already owns it,
# and two cases sharing one planted pod share its dedup entry and its failures.
#
# `dd bs=<n>M count=1` allocates a single buffer of exactly that size and then
# exits, so the overshoot is bounded and legible in the pod spec itself. That
# boundedness is the point. An unbounded allocator (`tail /dev/zero`) has only
# one sound remediation -- no limit fits it -- and the lettered-options
# objective would then be grading the agent on padding its answer. With 96MiB
# against a 64Mi limit both fixes are real: raise the limit, or shrink the
# allocation.
#
# What the agent has to find is not in the event. The Warning kubelet emits is
# `BackOff`, "Back-off restarting failed container", which says a container is
# restarting and not why; OOMKilled and exit code 137 live in the pod's
# lastState.terminated. That gap is the diagnosis the case is testing.
resource "null_resource" "incident" {
  triggers = {
    # Destroy-time provisioners may read only `self`, so everything the
    # teardown needs is copied in here -- including the cluster coordinates,
    # because the destroy has to fetch its own credentials for the same reason
    # step 0 does and cannot reach `var`.
    namespace     = local.ns
    host_cluster  = var.host_cluster_name
    host_location = var.host_cluster_location
    host_project  = var.project_id

    # Re-plant when any of it changes. Without this the resource is inert after
    # the first apply, and a local run that edited the manifest would keep
    # scoring the old one. Inert in CI, where state starts fresh every run.
    manifest = sha256(join("|", [
      local.ns,
      local.workload,
      tostring(var.memory_limit_mib),
      tostring(var.allocate_mib),
      var.host_cluster_name,
    ]))
  }

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    command     = <<-EOT
      set -euo pipefail

      # ---- 0. Point kubectl at the cluster the watcher is on ----------------
      # Fetched here rather than inherited, because the ambient current-context
      # is NOT the host cluster by the time this apply runs. hack/ci-eval-pr.sh
      # points it there once before the task loop, and then devops-bench moves
      # it: evalharness/default.py calls deployer.get_cluster_info()
      # unconditionally after up(), TFDeployer's implementation hands the stack's
      # cluster_name output to GCPProvider.ensure_cluster_credentials, and that
      # shells `gcloud container clusters get-credentials` with no --kubeconfig.
      # gpu-stress-test-diagnosis sits two entries above this one in TASKS, is
      # also deployer: tofu, and outputs its own per-run task cluster -- so the
      # context in hand here names that cluster. bench/kube_agents_bench/harness.py
      # documents the same mechanism, which is why it pins the agent connection
      # with --context.
      #
      # An incident planted on the wrong cluster is never seen inside the run:
      # the watcher fans in over the host cluster plus the Cluster Agent
      # profiles present when it started, and a per-run task cluster is in
      # neither set -- see the header of this file for why. The old form of
      # this step asserted the context instead of setting it, which would have
      # failed the apply on every presubmit run.
      #
      # A plain get-credentials is the right call and needs no DNS-endpoint
      # handling: it is exactly what hack/ci-eval-pr.sh does for this same
      # cluster and what GCPProvider does for every other stack.
      # A directory, not `mktemp` on its own: get-credentials refuses to load an
      # existing empty file, so it warns, writes a dated `.backup` beside it that
      # nothing then cleans up, and prints a WARNING that reads like a failure in
      # a CI log. Handing it a path that does not exist yet skips all three.
      kubeconfig_dir="$(mktemp -d)"
      trap 'rm -rf "$kubeconfig_dir"' EXIT
      KUBECONFIG="$kubeconfig_dir/config"
      export KUBECONFIG

      project="${var.project_id}"
      if [ -z "$project" ]; then
        # GCPProvider.resolve_variables injects project_id for every task, so
        # this is the local-run path rather than the CI one.
        project="$(gcloud config get-value project 2>/dev/null || true)"
      fi
      if [ -z "$project" ]; then
        echo "ERROR: no project id. Pass -var project_id=... or set a gcloud default project; this stack needs one to fetch credentials for ${var.host_cluster_name}." >&2
        exit 1
      fi

      gcloud container clusters get-credentials "${var.host_cluster_name}" \
        --location "${var.host_cluster_location}" --project "$project" --quiet

      # Recorded before anything is planted, so the step-3 log poll cannot match
      # a `fire` line left by an earlier run against this same namespace name.
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
              - name: allocator
                image: busybox:1.36
                command: ["sh", "-c", "dd if=/dev/zero of=/dev/null bs=${var.allocate_mib}M count=1 && sleep 3600"]
                resources:
                  requests:
                    cpu: 10m
                    memory: ${var.memory_limit_mib}Mi
                  limits:
                    memory: ${var.memory_limit_mib}Mi
      MANIFEST

      # ---- 2. Wait for the debounce to clear --------------------------------
      # The watcher holds the crash-loop family until Event.Count reaches
      # --backoff-min-count, so it does not open an incident for a container
      # that recovers on its own. The stack must therefore not return the
      # moment the first BackOff appears: the agent turn would start minutes
      # before the card exists.
      #
      # 3 is the default (WATCHER_BACKOFF_MIN_COUNT in
      # deploy/shared/start-services.sh, passed as the flag). An install that
      # raised it does not break this loop -- step 3 below is the real gate and
      # waits for the watcher's own verdict -- it just spends the extra time
      # there rather than here.
      #
      # Every value this loop needs comes from the workload's own namespace, so
      # it is coupled to nothing but Kubernetes.
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
      # Read from the watcher's own log rather than from the gateway's
      # /api/sessions, which the design note in #901 suggested: that route needs
      # a bearer token and a port-forward, and the harness does not establish
      # its port-forward until the agent turn, which is after this apply. The
      # log line is the same seam observed one layer earlier, and `kubectl logs`
      # is all it costs.
      #
      # k8s-event-watcher is a peer process inside the credential proxy
      # container, not a container of its own, which is why -c names that one.
      #
      # --since-time, not --since: the namespace name is static, so a wall-clock
      # window would also match the `fire` line from an earlier run against a
      # recycled Boskos lease and return before this run's incident exists.
      #
      # The fire line carries no cluster (k8s-event-watcher/main.go), and the
      # watcher fans in over several, so in principle this matches a pod of
      # that name on any watched cluster. Nothing reaches it today: only this
      # stack plants ${local.ns}, the destroy removes it, and the task loop is
      # sequential. A leftover namespace from a failed teardown on another
      # watched cluster, or the concurrency of #637, would.
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

      # The card is filed within seconds of that line and then takes minutes to
      # run. Waiting for it HERE would mean reading the Platform Agent pod's
      # SQLite board over `kubectl exec`, coupling a bench fixture to an
      # internal file path; the task's prompt polls the board with kanban_show
      # instead, which is the read path the product actually offers.
    EOT
  }

  # Namespace-scoped by design, and this is the half that keeps it that way.
  # The presubmit isolation rule admits a mutating case only if it is read-only
  # or namespace-scoped, and a scenario that leaves its namespace behind is
  # neither by the second run.
  #
  # It fetches credentials the same way step 0 does, and for a sharper reason: a
  # create-time provisioner that failed taints the resource, and _teardown runs
  # `tofu destroy` from a `finally` on that path too, so this can execute with
  # the ambient context pointed anywhere at all. Deleting a namespace by name on
  # the wrong cluster is the kind of thing --ignore-not-found makes survivable
  # rather than safe.
  provisioner "local-exec" {
    when        = destroy
    on_failure  = continue
    interpreter = ["/bin/bash", "-c"]
    command     = <<-EOT
      set -euo pipefail
      # A directory, not `mktemp` on its own: get-credentials refuses to load an
      # existing empty file, so it warns, writes a dated `.backup` beside it that
      # nothing then cleans up, and prints a WARNING that reads like a failure in
      # a CI log. Handing it a path that does not exist yet skips all three.
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

# Passed straight through: the subject cluster already exists and this stack is
# not building one. See the header for why they are mandatory anyway.
output "cluster_name" {
  value = var.host_cluster_name
}

output "cluster_location" {
  value = var.host_cluster_location
}
