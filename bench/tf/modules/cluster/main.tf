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

# This dispatch module instantiates only one concrete cluster sub-module and
# declares no concrete-provider requirements of its own: each sub-module owns
# its provider (google in ./gke, tehcyx/kind in ./kind), so a KinD-only run does
# not pull in the GCP provider plugin.

module "gke" {
  source                   = "./gke"
  count                    = var.infra_provider == "gcp" ? 1 : 0
  project_id               = var.project_id
  location                 = var.location != "" ? var.location : "us-central1-a"
  cluster_name             = var.cluster_name
  node_count               = var.node_count
  machine_type             = var.machine_type != "" ? var.machine_type : "e2-standard-2"
  kubernetes_version       = var.kubernetes_version
  enable_workload_identity = var.enable_workload_identity
  agent_service_account    = var.agent_service_account
  enable_iap_ssh           = var.enable_iap_ssh
  gpu_type                 = var.gpu_type
  gpu_count                = var.gpu_count
}

# Only the gke sub-module is vendored: it carries kube-agents changes (resource
# labels, orphan reaping) that cannot be expressed from the caller side. This
# one has no local changes, so it stays upstream rather than becoming a copy
# nobody maintains and nobody notices drifting.
module "kind" {
  source          = "git::https://github.com/kubernetes-sigs/devops-bench.git//tf/modules/cluster/kind?ref=4670d76dcc497e8f515d51c2bb6bad6ced7100b6"
  count           = var.infra_provider == "kind" ? 1 : 0
  cluster_name    = var.cluster_name
  kubeconfig_path = var.kubeconfig_path
  node_image      = var.node_image
  project_id      = var.project_id
  location        = var.location != "" ? var.location : "local"
  node_count      = var.node_count
}

