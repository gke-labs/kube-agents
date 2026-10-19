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

# The deployer drops an injected variable this stack does not declare and
# raises on a task.yaml variable it does not declare, so this file and
# bench/tasks/monitoring-alert-policy-read/task.yaml are a matched pair.

# The deployer reads cluster_name and cluster_location from the outputs, so
# the stack hands back the host cluster the runner injects. The policy itself
# is project-scoped and names no cluster.
variable "host_cluster_name" {
  type        = string
  description = "Name of the cluster the Platform Agent runs in"
}

variable "host_cluster_location" {
  type        = string
  description = "Region or zone of host_cluster_name"
}

variable "project_id" {
  type        = string
  description = "GCP project of the kube-agents install; the alert policy is created there"
  default     = ""
}

# The verifier reads this name in the agent's answer, so it must not be a
# phrase a policy-less answer could contain by chance.
variable "policy_display_name" {
  type        = string
  description = "Display name of the planted alert policy"
  default     = "ka-bench orbital-checkout restart storm"
}

# Identify the CI run that created the policy; see
# bench/tf/prebuilt/gpu-stress-test/variables.tf. Both are empty outside CI.
variable "prow_build_id" {
  type        = string
  description = "Prow BUILD_ID of the run that created this fixture"
  default     = ""
}

variable "prow_pull_number" {
  type        = string
  description = "Prow PULL_NUMBER of the run that created this fixture"
  default     = ""
}
