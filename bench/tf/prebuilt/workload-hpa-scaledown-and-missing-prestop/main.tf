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

# The scenario driver for bench/tasks/workload-hpa-scaledown-and-missing-prestop.
#
# It plants a two-replica Deployment whose containers declare no preStop hook
# and a HorizontalPodAutoscaler on it whose scale-down behavior has no
# stabilization window and removes every replica a period allows. No
# obtainability check reads the scale-down behavior today; the case carries
# expected_fail: true and the planted pair is what a fix would have to name.
#
# Isolation class: namespace-scoped. Teardown removes what this stack created and
# deletes the namespace only when this stack created it.

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
  ns     = var.namespace
  target = var.target_resource_name
  hpa    = "${var.target_resource_name}-hpa"
  ci_labels = {
    "managed-by"  = "kube-agents-bench"
    "build-id"    = var.prow_build_id != "" ? var.prow_build_id : "local"
    "pull-number" = var.prow_pull_number != "" ? var.prow_pull_number : "none"
  }
}

resource "null_resource" "scenario" {
  triggers = {
    namespace     = local.ns
    target        = local.target
    hpa           = local.hpa
    host_cluster  = var.host_cluster_name
    host_location = var.host_cluster_location
    host_project  = var.project_id
  }

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    command     = <<-EOT
      set -euo pipefail
      kubeconfig_dir="$(mktemp -d)"
      trap 'rm -rf "$kubeconfig_dir"' EXIT
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

      # A namespace of this name that this stack did not plant is a stop, not a
      # target; one this stack planted and left Terminating (destroy deletes
      # with --wait=false) is cleared before the plant. One read tells the
      # three states apart: absent (the read fails with NotFound: go on),
      # present with this stack's label (clear it), present without it (stop).
      leftover_owner="$(kubectl get namespace "${local.ns}" \
        -o jsonpath='{.metadata.labels.created-by-stack}' 2>"$kubeconfig_dir/ns-read.err")" && ns_present=1 || ns_present=0
      if [ "$ns_present" = 0 ] && ! grep -qi "not found" "$kubeconfig_dir/ns-read.err"; then
        echo "ERROR: could not read namespace ${local.ns} on ${var.host_cluster_name}: $(cat "$kubeconfig_dir/ns-read.err")" >&2
        exit 1
      fi
      if [ "$ns_present" = 1 ]; then
        if [ "$leftover_owner" != "workload-hpa-scaledown-and-missing-prestop" ]; then
          echo "ERROR: ${local.ns} already exists on ${var.host_cluster_name} and was not created by this stack (created-by-stack='$leftover_owner'); it will not be deleted. Remove it by hand if it is stale." >&2
          exit 1
        fi
        echo "Found a leftover ${local.ns} from an earlier run; deleting it before planting."
        if ! kubectl delete namespace "${local.ns}" --ignore-not-found --wait=true --timeout=180s; then
          echo "ERROR: could not clear the leftover ${local.ns} within 180s; its current state follows." >&2
          kubectl get namespace "${local.ns}" -o yaml >&2 || true
          exit 1
        fi
      fi
      # Created and labelled in one request, so the namespace never exists
      # without the label the guard above and the destroy key on.
      kubectl apply -f - <<'NAMESPACE'
      apiVersion: v1
      kind: Namespace
      metadata:
        name: ${local.ns}
        labels:
          managed-by: ${local.ci_labels["managed-by"]}
          build-id: "${local.ci_labels["build-id"]}"
          pull-number: "${local.ci_labels["pull-number"]}"
          created-by-stack: workload-hpa-scaledown-and-missing-prestop
      NAMESPACE
      kubectl apply -f - <<'MANIFEST'
      apiVersion: apps/v1
      kind: Deployment
      metadata:
        name: ${local.target}
        namespace: ${local.ns}
        labels:
          app: ${local.target}
          managed-by: kube-agents-bench
      spec:
        replicas: 2
        selector:
          matchLabels:
            app: ${local.target}
        template:
          metadata:
            labels:
              app: ${local.target}
          spec:
            automountServiceAccountToken: false
            containers:
              - name: app
                image: registry.k8s.io/pause:3.10
                ports:
                  - containerPort: 8080
                resources:
                  requests:
                    cpu: 10m
                    memory: 16Mi
                  limits:
                    memory: 32Mi
      ---
      apiVersion: autoscaling/v2
      kind: HorizontalPodAutoscaler
      metadata:
        name: ${local.hpa}
        namespace: ${local.ns}
        labels:
          app: ${local.target}
          managed-by: kube-agents-bench
      spec:
        scaleTargetRef:
          apiVersion: apps/v1
          kind: Deployment
          name: ${local.target}
        minReplicas: 2
        maxReplicas: 10
        metrics:
          - type: Resource
            resource:
              name: cpu
              target:
                type: Utilization
                averageUtilization: 60
        behavior:
          scaleDown:
            stabilizationWindowSeconds: 0
            policies:
              - type: Percent
                value: 100
                periodSeconds: 15
      MANIFEST
      echo "Planted scenario workload-hpa-scaledown-and-missing-prestop on ${var.host_cluster_name}."
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
      # One delete: the namespace cascades to the Deployment and the autoscaler,
      # so no earlier object delete can abort the script before it. The label is
      # read once, with a read error told apart from "not this stack's", so a
      # namespace this stack owns is never skipped in silence.
      ns_owner="$(kubectl get namespace "${self.triggers.namespace}" \
        -o jsonpath='{.metadata.labels.created-by-stack}' 2>"$kubeconfig_dir/ns-read.err")" && ns_present=1 || ns_present=0
      if [ "$ns_present" = 1 ]; then
        if [ "$ns_owner" = "workload-hpa-scaledown-and-missing-prestop" ]; then
          kubectl delete namespace "${self.triggers.namespace}" --ignore-not-found --wait=false
        else
          echo "WARNING: ${self.triggers.namespace} on ${self.triggers.host_cluster} was not created by this stack (created-by-stack='$ns_owner'); left in place." >&2
        fi
      elif ! grep -qi "not found" "$kubeconfig_dir/ns-read.err"; then
        echo "ERROR: could not read namespace ${self.triggers.namespace} on ${self.triggers.host_cluster}; it may still hold the planted workload: $(cat "$kubeconfig_dir/ns-read.err")" >&2
        exit 1
      fi
    EOT
  }
}

output "cluster_name" {
  value = var.host_cluster_name
}

output "cluster_location" {
  value = var.host_cluster_location
}
