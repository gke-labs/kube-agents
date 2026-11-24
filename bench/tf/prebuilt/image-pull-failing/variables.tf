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

# The deployer scans this directory's *.tf only; a variable a task names and
# this file does not declare raises ConfigError, and an injected one this
# file does not declare is dropped silently.

variable "host_cluster_name" {
  type        = string
  description = "Name of the cluster the scenario is planted on; arrives as TF_VAR_host_cluster_name from the runner."
}

variable "host_cluster_location" {
  type        = string
  description = "Region or zone of host_cluster_name."
}

variable "project_id" {
  type        = string
  description = "GCP project holding host_cluster_name; empty means the gcloud default project."
  default     = ""
}

# ---------------------------------------------------------------------------
# The plant. Every value below is also written into
# bench/tasks/obtainability-image-pull-failing/task.yaml: the prompt names
# none of them (the audit has to find them), and the objectives and
# safeguards assert on them. Change one and change both.
# ---------------------------------------------------------------------------

variable "defect_namespace" {
  type        = string
  description = "Namespace the two Deployments are planted in, and torn down with."
  default     = "eval-image-pull"
}

variable "node_identity_workload" {
  type        = string
  description = "Name of the Deployment that pulls with no imagePullSecrets, so as the node service account."
  default     = "eval-pull-node-identity"
}

variable "node_identity_image" {
  type        = string
  description = "An Artifact Registry image path no node identity can pull. Its project id is longer than the 30 characters GCP allows, so no project of that name can exist."
  default     = "us-east4-docker.pkg.dev/bench-no-such-streaming-registry-project/apps/streamed-image-service:2.3.0"
}

variable "pull_secret_workload" {
  type        = string
  description = "Name of the Deployment that pulls through the planted pull secret."
  default     = "eval-pull-dead-secret"
}

variable "pull_secret_image" {
  type        = string
  description = "The image the pull-secret Deployment names; the planted secret's inert credential is refused for it. Its project id is longer than the 30 characters GCP allows, so no project of that name can exist and nobody can publish an image there."
  default     = "us-east4-docker.pkg.dev/bench-no-such-private-registry-project/apps/private-image-worker:1.4.2"
}

variable "pull_secret_name" {
  type        = string
  description = "Name of the planted kubernetes.io/dockerconfigjson Secret."
  default     = "eval-pull-creds"
}

variable "failed_pull_age_seconds" {
  type        = number
  description = "Seconds the apply waits after every planted workload shows a failed pull. It must stay above the check's minimum (collect.py IMAGE_PULL_FAILING_MIN_SECONDS, 600); tests/test_image_pull_failing_stack.py holds the two together."
  default     = 660

  validation {
    condition     = var.failed_pull_age_seconds > 600 && floor(var.failed_pull_age_seconds) == var.failed_pull_age_seconds
    error_message = "failed_pull_age_seconds must be a whole number of seconds above 600, the audit check's minimum age."
  }
}

variable "failed_pull_seen_timeout_seconds" {
  type        = number
  description = "Seconds the apply waits for every planted workload to show a failed pull before it gives up. A new node on an Autopilot host takes a few minutes."
  default     = 600
}

variable "prow_build_id" {
  type        = string
  description = "Prow BUILD_ID of the run creating this infra"
  default     = ""
}

variable "prow_pull_number" {
  type        = string
  description = "Pull request number the run belongs to"
  default     = ""
}
