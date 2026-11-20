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
  description = "Name of the cluster the Platform Agent runs in; the events are recorded as this cluster's."
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

variable "event_namespace" {
  type        = string
  description = "Namespace the two recorded events name. Nothing is planted in it; the task's objective requires both workload names in the reply."
  default     = "eval-alert-history"
}

variable "workload_names" {
  type        = list(string)
  description = "The two workloads the recorded events name, one event each. Repeated in the task's objective."
  default     = ["eval-alert-history-api", "eval-alert-history-worker"]
}

variable "event_reason" {
  type        = string
  description = "Event reason of both records. Any reason is Info on a Normal-type event (get_severity_details in session_kv_server.py)."
  default     = "BackOff"
}

variable "event_message" {
  type        = string
  description = "Event message of both records."
  default     = "Back-off pulling image \"registry.example.invalid/app:1.2.3\""
}
