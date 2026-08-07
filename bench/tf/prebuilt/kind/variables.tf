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

variable "cluster_name" {
  type    = string
  default = "devops-bench-kind"
}

variable "location" {
  type    = string
  default = "local"
}

# Relative to the directory tofu runs in, which the deployer copies per run
# under --parallel. Upstream defaults this to ~/.kube/config, which merges the
# throwaway cluster into the developer's real kubeconfig and repoints
# current-context at it -- and does so even under run isolation, since a
# literal path ignores the per-run KUBECONFIG the deployer exports.
#
# TODO: cluster_name is still a static default, so concurrent runs that do not
# each override it collide on one cluster name.
variable "kubeconfig_path" {
  type        = string
  description = "Path to write the kubeconfig file"
  default     = "./kind-kubeconfig.yaml"
}

