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

# The scenario driver for bench/tasks/workload-private-registry-pull-secret-expired.
#
# It plants a dockerconfigjson Secret with a credential that no longer works
# (an inert literal, never a token that was once valid) and a two-replica
# Deployment that pulls a private image through it, so the pods sit in
# ErrImagePull. The compliance audit has no check that names the pattern:
# the case carries expected_fail: true, and the planted state is what a fix
# would have to detect.
#
# Isolation class: namespace-scoped, in its own namespace so the baseline
# case's namespace left Terminating on the same host cluster cannot stop
# this plant. The namespace, its labels, the leftover pre-clear and the
# teardown (which cascades to the Secret) are the namespaced-plant module's;
# this file holds the manifest.

terraform {
  required_version = ">= 1.5.0"
}

locals {
  ci_labels = {
    "managed-by"  = "kube-agents-bench"
    "build-id"    = var.prow_build_id != "" ? var.prow_build_id : "local"
    "pull-number" = var.prow_pull_number != "" ? var.prow_pull_number : "none"
  }
  # The credential is an inert literal: a username and a password that were
  # never valid anywhere, base64 of "_json_key:expired-key-material".
  manifest = <<-MANIFEST
    apiVersion: v1
    kind: Secret
    metadata:
      name: private-reg-creds
      labels:
        managed-by: kube-agents-bench
    type: kubernetes.io/dockerconfigjson
    stringData:
      .dockerconfigjson: '{"auths":{"us-east4-docker.pkg.dev":{"username":"_json_key","password":"expired-key-material","auth":"X2pzb25fa2V5OmV4cGlyZWQta2V5LW1hdGVyaWFs"}}}'
    ---
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
          imagePullSecrets:
            - name: private-reg-creds
          containers:
            - name: app
              image: us-east4-docker.pkg.dev/private-registry-project/apps/private-image-worker:1.4.2
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
  stack                 = "workload-private-registry-pull-secret-expired"
  labels                = local.ci_labels
  manifest              = local.manifest
}

output "cluster_name" {
  value = module.plant.cluster_name
}

output "cluster_location" {
  value = module.plant.cluster_location
}
