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

variable "target_resource_name" {
  type        = string
  description = "Name of the cluster-scoped ValidatingWebhookConfiguration the case's checks name."
  default     = "strict-policy-webhook"
}

variable "backend_namespace" {
  type        = string
  description = "Namespace the webhook's clientConfig.service names. The stack creates neither the namespace nor the Service, so the backend does not exist; the case's backend safeguard names the same value."
  default     = "seeded-security"
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
