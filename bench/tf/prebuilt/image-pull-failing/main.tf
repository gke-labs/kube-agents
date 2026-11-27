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

# The scenario driver for bench/tasks/obtainability-image-pull-failing.
#
# It plants two one-replica Deployments whose image pull the registry refuses
# and then waits until each has failed for longer than the audit's minimum,
# so the Workload Reliability audit the case runs next reads a failure it
# names (obtainability 3.28, `image-pull-failing`, ten minutes). The two pull
# paths are the two the check tells apart from the pod's own spec: the node
# identity (no imagePullSecrets), and a named pull secret whose credential is
# an inert literal. No fixture edits the host's IAM: the image paths name
# projects that cannot exist, so the refusal is the same on every host.
#
# The wait is this stack's own and real: the check's minimum is the product's
# default and no setting shortens it for a test. It costs each repetition
# about eleven minutes.
#
# Isolation class: namespace-scoped, in its own namespace. The namespace, its
# labels, the leftover pre-clear and the teardown (which cascades to the
# Secret) are the namespaced-plant module's.

terraform {
  required_version = ">= 1.5.0"
}

locals {
  stack = "image-pull-failing"
  ci_labels = {
    "managed-by"  = "kube-agents-bench"
    "build-id"    = var.prow_build_id != "" ? var.prow_build_id : "local"
    "pull-number" = var.prow_pull_number != "" ? var.prow_pull_number : "none"
  }
  # The project id in each image path is longer than GCP allows, so no
  # project of that name can exist. The pull secret's credential is an inert
  # literal: a username and a password that were never valid anywhere, base64
  # of "_json_key:expired-key-material".
  manifest = <<-MANIFEST
    apiVersion: v1
    kind: Secret
    metadata:
      name: ${var.pull_secret_name}
      labels:
        managed-by: kube-agents-bench
    type: kubernetes.io/dockerconfigjson
    stringData:
      .dockerconfigjson: '{"auths":{"us-east4-docker.pkg.dev":{"username":"_json_key","password":"expired-key-material","auth":"X2pzb25fa2V5OmV4cGlyZWQta2V5LW1hdGVyaWFs"}}}'
    ---
    apiVersion: apps/v1
    kind: Deployment
    metadata:
      name: ${var.node_identity_workload}
      labels:
        app: ${var.node_identity_workload}
        managed-by: kube-agents-bench
    spec:
      replicas: 1
      selector:
        matchLabels:
          app: ${var.node_identity_workload}
      template:
        metadata:
          labels:
            app: ${var.node_identity_workload}
        spec:
          automountServiceAccountToken: false
          containers:
            - name: app
              image: ${var.node_identity_image}
              resources:
                requests:
                  cpu: 10m
                  memory: 16Mi
    ---
    apiVersion: apps/v1
    kind: Deployment
    metadata:
      name: ${var.pull_secret_workload}
      labels:
        app: ${var.pull_secret_workload}
        managed-by: kube-agents-bench
    spec:
      replicas: 1
      selector:
        matchLabels:
          app: ${var.pull_secret_workload}
      template:
        metadata:
          labels:
            app: ${var.pull_secret_workload}
        spec:
          automountServiceAccountToken: false
          imagePullSecrets:
            - name: ${var.pull_secret_name}
          containers:
            - name: app
              image: ${var.pull_secret_image}
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
  namespace             = var.defect_namespace
  stack                 = local.stack
  labels                = local.ci_labels
  manifest              = local.manifest
}

# The wait for the failure's age, after the plant. A new plant is a new wait.
resource "terraform_data" "failed_pull_age" {
  depends_on       = [module.plant]
  triggers_replace = [sha256(local.manifest), var.failed_pull_age_seconds]

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    command     = "bash '${path.module}/wait_for_failed_pull_age.sh'"
    environment = {
      WAIT_HOST_CLUSTER         = var.host_cluster_name
      WAIT_HOST_LOCATION        = var.host_cluster_location
      WAIT_PROJECT              = var.project_id
      WAIT_NAMESPACE            = var.defect_namespace
      WAIT_WORKLOADS            = "${var.node_identity_workload} ${var.pull_secret_workload}"
      WAIT_AGE_SECONDS          = var.failed_pull_age_seconds
      WAIT_SEEN_TIMEOUT_SECONDS = var.failed_pull_seen_timeout_seconds
    }
  }
}

output "cluster_name" {
  value = module.plant.cluster_name
}

output "cluster_location" {
  value = module.plant.cluster_location
}
