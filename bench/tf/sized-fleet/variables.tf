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

variable "project_id" {
  description = "GCP project the sized fleet is built in: the scale project, not an eval pool project."
  type        = string
}

variable "size" {
  description = "Fleet size, a key of `factors` in bench/scale/fleet-sizes.yaml (S, M or L)."
  type        = string
  validation {
    condition     = contains(["S", "M", "L"], var.size)
    error_message = "size must be S, M or L."
  }
}

variable "region" {
  description = "Region of the fleet's network, router and NAT."
  type        = string
  default     = "us-central1"
}

variable "zone" {
  description = "Zone of every cluster. Zonal on purpose: the fleet exists to be read by the agent, and a regional control plane triples the node count for nothing."
  type        = string
  default     = "us-central1-f"
}

variable "node_service_account" {
  description = "Email of the nodes' service account, created once outside this stack (README.md) so the identity that applies the stack needs no right to create service accounts."
  type        = string
}

variable "run_id" {
  description = "The run that built the fleet, as the `kube-agents-scale-run` resource label: the cost record finds the run's instances, disks and clusters by it."
  type        = string
  default     = "manual"
  validation {
    condition     = can(regex("^[a-z0-9_-]{1,63}$", var.run_id))
    error_message = "run_id must be a GCP label value: lowercase letters, digits, '_' and '-', at most 63."
  }
}
