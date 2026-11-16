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

# The scenario driver for bench/tasks/security-wi-ksa-gsa-iam-binding-missing.
#
# It plants a ServiceAccount whose iam.gke.io/gcp-service-account annotation
# names a Google service account in a project whose id is longer than GCP
# allows, so the account exists nowhere and no roles/iam.workloadIdentityUser binding for this namespace and
# account can exist behind it, and a one-replica Deployment that runs under
# the account. That is the pair a check would read: the annotation on the
# Kubernetes side, the missing member on the IAM side. Nothing in IAM is
# created or edited: the fixture is namespace objects only. The compliance
# audit has no check that names the pattern: the case carries
# expected_fail: true.
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
  # The pod mounts no API-server token: a Workload Identity workload reaches
  # Google Cloud through the metadata server, not through that token, and
  # the mount would only draw the audit's unbound-token check (2.14) onto a
  # workload this case grades on another check.
  manifest = <<-MANIFEST
    apiVersion: v1
    kind: ServiceAccount
    metadata:
      name: ${var.target_resource_name}
      labels:
        app: ${var.workload_name}
        managed-by: kube-agents-bench
      annotations:
        iam.gke.io/gcp-service-account: ${var.gsa_email}
    ---
    apiVersion: apps/v1
    kind: Deployment
    metadata:
      name: ${var.workload_name}
      labels:
        app: ${var.workload_name}
        managed-by: kube-agents-bench
    spec:
      replicas: 1
      selector:
        matchLabels:
          app: ${var.workload_name}
      template:
        metadata:
          labels:
            app: ${var.workload_name}
        spec:
          serviceAccountName: ${var.target_resource_name}
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
  stack                 = "security-wi-ksa-gsa-iam-binding-missing"
  labels                = local.ci_labels
  manifest              = local.manifest
}

output "cluster_name" {
  value = module.plant.cluster_name
}

output "cluster_location" {
  value = module.plant.cluster_location
}
