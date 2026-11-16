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

# The scenario driver for bench/tasks/observability-fluentbit-dcgm-cpu-throttling-oom.
#
# It plants a one-replica Deployment named fluent-bit whose container wants a
# whole core (a busybox shell busy loop) under a 50m CPU limit, so the kernel
# throttles it on nearly every CFS period from the moment it starts. The pod
# never restarts and reads Running: that is the failure a throttled log or
# metrics collector shows, telemetry delayed or dropped behind a healthy pod
# list. No audit reads a container's CPU usage against its CPU limit: the case
# carries expected_fail: true, and the planted state is what a fix would have
# to detect. The OOM restart-loop half of the same gap issue is the sibling
# case observability-gmp-kube-state-metrics-oom.
#
# Isolation class: namespace-scoped, in its own namespace. The namespace, its
# labels, the leftover pre-clear and the teardown are the namespaced-plant
# module's; this file holds the manifest. Requests and limits are equal on
# both dimensions so an Autopilot host (which rewrites requests that fall
# under its minimums, and limits that differ from requests where bursting is
# off) keeps the 50m the case's safeguard reads. The busy loop costs the host
# 50m of one node for the run and raises no event.

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
        app.kubernetes.io/name: fluent-bit
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
            app.kubernetes.io/name: fluent-bit
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
              command: ["sh", "-c", "while :; do :; done"]
              resources:
                requests:
                  cpu: 50m
                  memory: 64Mi
                limits:
                  cpu: 50m
                  memory: 64Mi
  MANIFEST
}

module "plant" {
  source = "../../modules/namespaced-plant"

  host_cluster_name     = var.host_cluster_name
  host_cluster_location = var.host_cluster_location
  project_id            = var.project_id
  namespace             = var.namespace
  stack                 = "observability-fluentbit-dcgm-cpu-throttling-oom"
  labels                = local.ci_labels
  manifest              = local.manifest
}

output "cluster_name" {
  value = module.plant.cluster_name
}

output "cluster_location" {
  value = module.plant.cluster_location
}
