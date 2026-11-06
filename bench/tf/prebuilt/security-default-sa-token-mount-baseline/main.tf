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

# The scenario driver for bench/tasks/security-default-sa-token-mount-baseline.
#
# It plants a one-replica Deployment that runs under the namespace's default
# ServiceAccount with automountServiceAccountToken on, the pattern the
# compliance audit's default-sa-automount check (SOP 2.7) names; the
# namespace's default account is left as the controller creates it, with the
# field unset. The collector lists the workload; the case carries
# expected_fail: true because the ledger body drops every major and minor
# finding once the critical findings fill its budget.
#
# Isolation class: namespace-scoped, in its own namespace so another case's
# namespace left Terminating on the same host cluster cannot stop this plant.
# The namespace, its labels, the leftover pre-clear and the teardown are the
# namespaced-plant module's; this file holds the manifest.

terraform {
  required_version = ">= 1.5.0"
}

locals {
  ci_labels = {
    "managed-by"  = "kube-agents-bench"
    "build-id"    = var.prow_build_id != "" ? var.prow_build_id : "local"
    "pull-number" = var.prow_pull_number != "" ? var.prow_pull_number : "none"
  }
  manifest = <<-MANIFEST
    apiVersion: apps/v1
    kind: Deployment
    metadata:
      name: ${var.target_resource_name}
      labels:
        app: ${var.target_resource_name}
        managed-by: kube-agents-bench
    spec:
      replicas: 1
      selector:
        matchLabels:
          app: ${var.target_resource_name}
      template:
        metadata:
          labels:
            app: ${var.target_resource_name}
        spec:
          serviceAccountName: default
          automountServiceAccountToken: true
          containers:
            - name: app
              image: registry.k8s.io/pause:3.10
              resources:
                requests:
                  cpu: 10m
                  memory: 16Mi
                limits:
                  memory: 32Mi
  MANIFEST
}

module "plant" {
  source = "../../modules/namespaced-plant"

  host_cluster_name     = var.host_cluster_name
  host_cluster_location = var.host_cluster_location
  project_id            = var.project_id
  namespace             = var.namespace
  stack                 = "security-default-sa-token-mount-baseline"
  labels                = local.ci_labels
  manifest              = local.manifest
}

output "cluster_name" {
  value = module.plant.cluster_name
}

output "cluster_location" {
  value = module.plant.cluster_location
}
