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
  description = "Name of the cluster the scenario is planted on."
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

variable "namespace" {
  type        = string
  description = "Namespace the scenario's objects are planted in. The case's checks name the same namespace."
}

variable "stack" {
  type        = string
  description = "The calling stack's name, written as the namespace's created-by-stack label. The pre-clear and the destroy act only on a namespace that carries it."
}

variable "labels" {
  type        = map(string)
  description = "The managed-by, build-id and pull-number labels the namespace carries. Values are written quoted: on the lane the build id is an integer."
}

variable "manifest" {
  type        = string
  description = "The namespaced objects to create after the namespace exists, one YAML document stream rendered by the root stack."
}

variable "settle_seconds" {
  type        = number
  description = "Seconds the apply waits after the plant before it returns. For a case that grades the alert the plant raises. Keep it well under the max_age_sec the calling case sets on its fold check, which counts back from the turn that follows the apply."
  default     = 0

  validation {
    condition     = var.settle_seconds >= 0 && var.settle_seconds <= 600 && floor(var.settle_seconds) == var.settle_seconds
    error_message = "settle_seconds must be a whole number of seconds between 0 and 600."
  }
}
