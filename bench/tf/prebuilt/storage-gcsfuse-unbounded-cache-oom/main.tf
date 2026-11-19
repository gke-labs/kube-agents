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

# The scenario driver for bench/tasks/storage-gcsfuse-unbounded-cache-oom.
#
# It plants a one-replica Deployment that asks for the gcsfuse CSI sidecar
# (`gke-gcsfuse/volumes: "true"`) and mounts a bucket through the driver with
# no sidecar memory-limit annotation, so the injected sidecar has no memory
# limit: the shape no obtainability check names today. The volume carries no
# `fileCacheCapacity`, which leaves the file cache off. A declared gap: the
# case expects its ledger objective to fail.
#
# Isolation class: namespace-scoped. The namespace, its labels, the leftover
# pre-clear and the teardown are the namespaced-plant module's; this file
# holds the manifest. The bucket does not exist and the namespace's default
# ServiceAccount has no Workload Identity binding, so the mount fails and the
# pod never becomes ready; the check reads the template, not the pod.

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
          annotations:
            gke-gcsfuse/volumes: "true"
        spec:
          automountServiceAccountToken: false
          containers:
            - name: reader
              image: registry.k8s.io/pause:3.10
              resources:
                requests:
                  cpu: 10m
                  memory: 16Mi
              volumeMounts:
                - name: bucket
                  mountPath: /data
          volumes:
            - name: bucket
              csi:
                driver: gcsfuse.csi.storage.gke.io
                volumeAttributes:
                  bucketName: ${var.bucket_name}
  MANIFEST
}

module "plant" {
  source = "../../modules/namespaced-plant"

  host_cluster_name     = var.host_cluster_name
  host_cluster_location = var.host_cluster_location
  project_id            = var.project_id
  namespace             = var.namespace
  stack                 = "storage-gcsfuse-unbounded-cache-oom"
  labels                = local.ci_labels
  manifest              = local.manifest
}

output "cluster_name" {
  value = module.plant.cluster_name
}

output "cluster_location" {
  value = module.plant.cluster_location
}
