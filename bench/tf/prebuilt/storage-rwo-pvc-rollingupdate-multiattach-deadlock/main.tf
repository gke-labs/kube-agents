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

# The scenario driver for bench/tasks/storage-rwo-pvc-rollingupdate-multiattach-deadlock.
#
# It plants a ReadWriteOnce claim and a one-replica Deployment that mounts it
# and writes no update strategy, so the server defaults RollingUpdate with a
# maxSurge of one: the pattern the obtainability audit's rwo-claim-contended
# check (SOP 3.21, rollout arm) names. A baseline: the case expects a pass.
#
# Isolation class: namespace-scoped. The namespace, its labels, the leftover
# pre-clear and the teardown are the namespaced-plant module's; this file
# holds the manifest. The claim binds when the pod schedules (standard-rwo is
# WaitForFirstConsumer); the disk goes with the namespace.

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
    apiVersion: v1
    kind: PersistentVolumeClaim
    metadata:
      name: ${var.claim_name}
      labels:
        app: ${var.target_resource_name}
        managed-by: kube-agents-bench
    spec:
      accessModes:
        - ReadWriteOnce
      storageClassName: ${var.storage_class_name}
      resources:
        requests:
          storage: 1Gi
    ---
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
          automountServiceAccountToken: false
          containers:
            - name: writer
              image: registry.k8s.io/pause:3.10
              resources:
                requests:
                  cpu: 10m
                  memory: 16Mi
              volumeMounts:
                - name: data
                  mountPath: /data
          volumes:
            - name: data
              persistentVolumeClaim:
                claimName: ${var.claim_name}
  MANIFEST
}

module "plant" {
  source = "../../modules/namespaced-plant"

  host_cluster_name     = var.host_cluster_name
  host_cluster_location = var.host_cluster_location
  project_id            = var.project_id
  namespace             = var.namespace
  stack                 = "storage-rwo-pvc-rollingupdate-multiattach-deadlock"
  labels                = local.ci_labels
  manifest              = local.manifest
}

output "cluster_name" {
  value = module.plant.cluster_name
}

output "cluster_location" {
  value = module.plant.cluster_location
}
