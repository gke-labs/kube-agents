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
# Isolation class: namespace-scoped. The namespace, its labels, the leftover
# pre-clear and the teardown are the namespaced-plant module's; this file
# holds the manifest.

terraform {
  required_version = ">= 1.5.0"
}

locals {
  hpa = "${var.target_resource_name}-hpa"
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
      replicas: 2
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
      labels:
        app: ${var.target_resource_name}
        managed-by: kube-agents-bench
    spec:
      scaleTargetRef:
        apiVersion: apps/v1
        kind: Deployment
        name: ${var.target_resource_name}
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
}

module "plant" {
  source = "../../modules/namespaced-plant"

  host_cluster_name     = var.host_cluster_name
  host_cluster_location = var.host_cluster_location
  project_id            = var.project_id
  namespace             = var.namespace
  stack                 = "workload-hpa-scaledown-and-missing-prestop"
  labels                = local.ci_labels
  manifest              = local.manifest
}

output "cluster_name" {
  value = module.plant.cluster_name
}

output "cluster_location" {
  value = module.plant.cluster_location
}
