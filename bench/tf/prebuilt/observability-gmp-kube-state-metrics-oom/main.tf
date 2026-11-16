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

# The scenario driver for bench/tasks/observability-gmp-kube-state-metrics-oom.
#
# It plants a one-replica Deployment named kube-state-metrics whose container
# allocates past its 64Mi memory limit as soon as it starts (busybox's `tail`
# buffering /dev/zero, the seeded fleet's payments-api recipe), so the pod is
# OOM-killed within a minute and restarts in a loop. No audit reads a
# container's OOMKilled last termination: the case carries expected_fail: true,
# and the planted state is what a fix would have to detect.
#
# Isolation class: namespace-scoped, in its own namespace. The namespace, its
# labels, the leftover pre-clear and the teardown are the namespaced-plant
# module's; this file holds the manifest. Memory request and limit are equal
# so an Autopilot host (which sets limits to requests when they differ) keeps
# the 64Mi the case's safeguard reads.

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
      name: "${var.target_resource_name}"
      labels:
        app: "${var.target_resource_name}"
        app.kubernetes.io/name: kube-state-metrics
        managed-by: kube-agents-bench
    spec:
      replicas: 1
      selector:
        matchLabels:
          app: "${var.target_resource_name}"
      template:
        metadata:
          labels:
            app: "${var.target_resource_name}"
            app.kubernetes.io/name: kube-state-metrics
        spec:
          automountServiceAccountToken: false
          securityContext:
            runAsNonRoot: true
            runAsUser: 65534
            seccompProfile:
              type: RuntimeDefault
          containers:
            - name: "${var.target_resource_name}"
              image: busybox:1.36
              command: ["sh", "-c", "tail /dev/zero"]
              resources:
                requests:
                  cpu: 10m
                  memory: 64Mi
                limits:
                  memory: 64Mi
  MANIFEST
}

module "plant" {
  source = "../../modules/namespaced-plant"

  host_cluster_name     = var.host_cluster_name
  host_cluster_location = var.host_cluster_location
  project_id            = var.project_id
  namespace             = var.namespace
  stack                 = "observability-gmp-kube-state-metrics-oom"
  labels                = local.ci_labels
  manifest              = local.manifest
}

output "cluster_name" {
  value = module.plant.cluster_name
}

output "cluster_location" {
  value = module.plant.cluster_location
}
