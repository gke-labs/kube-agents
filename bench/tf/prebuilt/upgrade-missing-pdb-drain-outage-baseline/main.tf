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

# The scenario driver for bench/tasks/upgrade-missing-pdb-drain-outage-baseline.
#
# It plants a three-replica Deployment with no PodDisruptionBudget, the
# pattern the obtainability audit's no-pdb check (SOP 3.3) names: a node
# upgrade's drain can evict every replica at once. A baseline: the case
# expects a pass.
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
      replicas: 3
      selector:
        matchLabels:
          app: ${var.target_resource_name}
      template:
        metadata:
          labels:
            app: ${var.target_resource_name}
        spec:
          automountServiceAccountToken: false
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
  stack                 = "upgrade-missing-pdb-drain-outage-baseline"
  labels                = local.ci_labels
  manifest              = local.manifest
}

output "cluster_name" {
  value = module.plant.cluster_name
}

output "cluster_location" {
  value = module.plant.cluster_location
}
