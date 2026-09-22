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

# The scenario driver for bench/tasks/ai-security-planted-model-audit.
#
# It plants ONE Deployment in ONE namespace on the host cluster: a fake model
# server whose spec carries the two defects the ai-security-audit SOP flags
# mechanically — an image on a floating tag whose path matches the SOP's §2
# model-image regex (which is also what classifies the workload as an AI
# workload at all), and a model-registry credential as a literal env value.
# The seeded fleet deliberately carries no AI-classified workload
# (inference-server runs pinned busybox), so without this plant the stream
# has nothing to find and — on a project where it has never filed — writes no
# ledger at all ("clean and has no open ledger; nothing to do",
# audit_report.py), which is ungradable. The plant is what makes the ledger
# deterministic.
#
# spec.replicas: 0, deliberately. Every check this case grades is a SPEC
# check, the SOP's §2 discriminator reads the workload dump (no replica
# filter), and zero replicas means the multi-GB vLLM image is never pulled,
# no server ever runs, no port is ever open — the inference-endpoint-public
# check has no Service to find and the plant costs the host cluster nothing.
#
# Host cluster, not a per-run task cluster, because the audit sweeps the
# leased project's clusters and the host is the one cluster every eval
# project has; the credential-fetch/teardown structure is
# prebuilt/autoops-incident's (see that stack's step 0 for why the ambient
# context cannot be inherited). No waits: the plant is inert, nothing has to
# demonstrably start.

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
  ns       = var.defect_namespace
  workload = var.workload_name

  ci_labels = {
    "managed-by"  = "kube-agents-bench"
    "build-id"    = var.prow_build_id != "" ? var.prow_build_id : "local"
    "pull-number" = var.prow_pull_number != "" ? var.prow_pull_number : "none"
  }
}

resource "null_resource" "defect" {
  triggers = {
    namespace     = local.ns
    host_cluster  = var.host_cluster_name
    host_location = var.host_cluster_location
    host_project  = var.project_id

    manifest = sha256(join("|", [
      local.ns,
      local.workload,
      var.model_image,
      var.plaintext_env_name,
      var.plaintext_env_value,
      var.host_cluster_name,
    ]))
  }

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    command     = <<-EOT
      set -euo pipefail

      # Own kubeconfig, own credentials; the ambient context is some earlier
      # tofu task's cluster by the time this apply runs.
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
        replicas: 0
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
              - name: server
                image: ${var.model_image}
                env:
                  - name: ${var.plaintext_env_name}
                    value: ${var.plaintext_env_value}
                resources:
                  requests:
                    cpu: 10m
                    memory: 16Mi
                  limits:
                    memory: 32Mi
      MANIFEST

      echo "Planted ${local.workload} (replicas 0) in ${local.ns} on ${var.host_cluster_name}."
    EOT
  }

  # Namespace-scoped teardown, own credentials, same shape and reasons as
  # prebuilt/autoops-incident's destroy provisioner.
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

# The subject cluster already exists; devops-bench reads these outputs
# unconditionally after up() and re-points the ambient kubeconfig at
# cluster_name — the cluster the task's safeguards read.
output "cluster_name" {
  value = var.host_cluster_name
}

output "cluster_location" {
  value = var.host_cluster_location
}
