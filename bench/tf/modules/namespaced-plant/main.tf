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

# namespaced-plant: a namespace this stack owns on the host cluster, the
# objects a case plants in it, and their removal.
#
# The shell lives once, in namespaced_plant.sh beside this file, and keeps
# these rules: a leftover namespace is read once and told apart as gone,
# this stack's (cleared) or foreign (a stop, never deleted); the namespace is
# created with its labels in one request; a plant that fails after the
# namespace exists removes it on the way out, since Terraform skips a tainted
# resource's destroy provisioner; the destroy reads ownership once, treats a
# read error as an error, and deletes the namespace alone, which cascades to
# everything planted in it. tests/test_namespaced_plant.py runs the script
# against stub kubectl and gcloud.

terraform {
  required_version = ">= 1.5.0"
  required_providers {
    null = {
      source  = "hashicorp/null"
      version = ">= 3.0.0"
    }
  }
}

locals {
  script = "${path.module}/namespaced_plant.sh"
}

resource "null_resource" "plant" {
  triggers = {
    namespace     = var.namespace
    stack         = var.stack
    host_cluster  = var.host_cluster_name
    host_location = var.host_cluster_location
    host_project  = var.project_id
    manifest      = sha256(var.manifest)
    script        = filesha256(local.script)
  }

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    command     = "bash '${local.script}' plant"
    environment = {
      NP_HOST_CLUSTER  = var.host_cluster_name
      NP_HOST_LOCATION = var.host_cluster_location
      NP_PROJECT       = var.project_id
      NP_NAMESPACE     = var.namespace
      NP_STACK         = var.stack
      NP_MANAGED_BY    = var.labels["managed-by"]
      NP_BUILD_ID      = var.labels["build-id"]
      NP_PULL_NUMBER   = var.labels["pull-number"]
      NP_MANIFEST      = var.manifest
    }
  }

  provisioner "local-exec" {
    when        = destroy
    on_failure  = continue
    interpreter = ["/bin/bash", "-c"]
    command     = "bash \"$NP_SCRIPT\" destroy"
    environment = {
      NP_SCRIPT        = "${path.module}/namespaced_plant.sh"
      NP_HOST_CLUSTER  = self.triggers.host_cluster
      NP_HOST_LOCATION = self.triggers.host_location
      NP_PROJECT       = self.triggers.host_project
      NP_NAMESPACE     = self.triggers.namespace
      NP_STACK         = self.triggers.stack
    }
  }
}
