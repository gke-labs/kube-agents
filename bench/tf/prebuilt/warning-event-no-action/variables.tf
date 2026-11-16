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
  description = "Name of the cluster the Platform Agent runs in; the workload is planted here."
}

variable "host_cluster_location" {
  type        = string
  description = "Region or zone of host_cluster_name."
}

variable "project_id" {
  type        = string
  description = "GCP Project ID holding host_cluster_name"
  default     = ""
}

variable "agent_namespace" {
  type        = string
  description = "Namespace of the kube-agents install on host_cluster_name"
  default     = "kubeagents-system"
}

variable "agent_deployment" {
  type        = string
  description = "Deployment running the Platform Agent pod that hosts the Session KV daemon"
  default     = "platform-agent-gateway"
}

variable "agent_container" {
  type        = string
  description = "Container inside agent_deployment carrying the Session KV daemon and its API key"
  default     = "platform-agent"
}

variable "daemon_url" {
  type        = string
  description = "Base URL of the Session KV daemon, as seen from inside the agent pod"
  default     = "http://127.0.0.1:8699" # sanitizer: allow the daemon's own loopback bind, reachable only from inside the agent pod
}

variable "incident_namespace" {
  type        = string
  description = "Namespace the healthy workload is planted in, and torn down with"
  default     = "eval-no-action-triage"
}

variable "workload_name" {
  type        = string
  description = "Name of the healthy Deployment whose pod the Warning event names"
  default     = "eval-healthy-workload"
}

variable "workload_image" {
  type        = string
  description = "Image of the healthy workload: it starts, stays Running, and raises no events of its own"
  default     = "registry.k8s.io/pause:3.10"
}

variable "event_reason" {
  type        = string
  description = "Reason of the injected Warning event: a node-level sandbox-start problem that clears by itself"
  default     = "NetworkNotReady"
}

variable "event_message" {
  type        = string
  description = "Message of the injected Warning event, as kubelet writes it for this reason"
  default     = "network is not ready: container runtime network not ready: NetworkReady=false reason:NetworkPluginNotReady message:Network plugin returns error: cni plugin not initialized"
}

variable "event_count" {
  type        = number
  description = "The injected event's own repeat count: a short problem, not a recurring one"
  default     = 2
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
