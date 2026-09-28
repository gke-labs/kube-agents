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

# The scenario driver for bench/tasks/obtainability-planted-orphan-service.
#
# It plants ONE ClusterIP Service in ONE otherwise empty namespace on the host
# cluster, whose selector matches no pod: the shape the obtainability-audit
# SOP's §3.16 `service-selects-nothing` flags from the cluster's own
# EndpointSlices. The seeded fleet carries no such Service, so without this
# plant the check has nothing to find.
#
# Nothing runs. There is no workload behind the selector — that absence is
# the defect — so the plant pulls no image, schedules no pod, and opens no
# port; a ClusterIP allocates an in-cluster address and nothing else. The
# namespace holds no workload either, so the SOP's "a workload deliberately
# at spec.replicas: 0" exclusion cannot apply and no near-miss selector
# exists for a remediation to latch onto.
#
# Host cluster, not a per-run task cluster, for prebuilt/ai-model-defects'
# reason: the audit sweeps the leased project's clusters and the host is the
# one cluster every eval project has. The credential-fetch/teardown structure
# is that stack's.

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
  ns      = var.defect_namespace
  service = var.service_name

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
      local.service,
      var.selector_value,
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

      # Destroy deletes the namespace with --wait=false, and the next
      # repetition can apply seconds later: applying into a namespace still
      # Terminating is refused, so wait the old one out first.
      if [ "$(kubectl get namespace "${local.ns}" -o jsonpath='{.status.phase}' 2>/dev/null)" = "Terminating" ]; then
        kubectl wait --for=delete "namespace/${local.ns}" --timeout=180s
      fi
      kubectl create namespace "${local.ns}" --dry-run=client -o yaml | kubectl apply -f -
      kubectl label namespace "${local.ns}" --overwrite \
        managed-by="${local.ci_labels["managed-by"]}" \
        build-id="${local.ci_labels["build-id"]}" \
        pull-number="${local.ci_labels["pull-number"]}"

      kubectl apply -f - <<'MANIFEST'
      apiVersion: v1
      kind: Service
      metadata:
        name: ${local.service}
        namespace: ${local.ns}
      spec:
        type: ClusterIP
        selector:
          app: ${var.selector_value}
        ports:
          - name: http
            port: 80
            targetPort: 8080
      MANIFEST

      echo "Planted ${local.service} (selects app=${var.selector_value}, which nothing carries) in ${local.ns} on ${var.host_cluster_name}."
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
