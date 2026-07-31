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

# TODO: cluster_name and kubeconfig_path defaults are static, not per-run-unique.
# Concurrent runs that don't each get an explicit override collide on the same
# cluster name / kubeconfig file. Needs per-run-unique value generation from
# whatever orchestrates runs; not solvable at this stack's variable-default layer.
variable "kubeconfig_path" {
  type        = string
  description = "Path to write the kubeconfig file"
  default     = "~/.kube/config"
}

