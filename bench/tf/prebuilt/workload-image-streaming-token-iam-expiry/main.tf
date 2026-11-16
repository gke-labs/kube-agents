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

# The scenario driver for bench/tasks/workload-image-streaming-token-iam-expiry.
#
# It plants a two-replica Deployment that pulls an Artifact Registry image
# with no imagePullSecrets, so the pull runs as the node service account
# (through the Image Streaming service where the cluster has it on), and that
# identity holds no artifactregistry.reader on the registry's project: the
# pods sit in ErrImagePull. That is the signal a check would read; the
# condition itself (a role stripped from the host's node service account) is
# never planted, since no fixture edits the host's IAM. The compliance audit
# has no check that names the pattern: the case carries expected_fail: true.
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
  # No imagePullSecrets, by design: the pull path is the node identity, which
  # is what the registry refuses. The project id in the image path is longer
  # than GCP allows, so no project of that name can exist and the refusal is
  # the same on every host.
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
              image: ${var.image}
              resources:
                requests:
                  cpu: 10m
                  memory: 16Mi
  MANIFEST
}

module "plant" {
  source = "../../modules/namespaced-plant"

  host_cluster_name     = var.host_cluster_name
  host_cluster_location = var.host_cluster_location
  project_id            = var.project_id
  namespace             = var.namespace
  stack                 = "workload-image-streaming-token-iam-expiry"
  labels                = local.ci_labels
  manifest              = local.manifest
}

output "cluster_name" {
  value = module.plant.cluster_name
}

output "cluster_location" {
  value = module.plant.cluster_location
}
