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
# the task.yaml files that use this stack (bench/tasks/bootstrap-discovery-fanout,
# bench/tasks/bootstrap-discovery-capped) are a matched set.

variable "host_cluster_name" {
  type        = string
  description = "Name of the cluster the Platform Agent runs in; the discovery sweep is re-armed there."
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
  description = "Deployment running the Platform Agent pod, which holds the onboarding markers and the board"
  default     = "platform-agent-gateway"
}

variable "agent_container" {
  type        = string
  description = "Container inside agent_deployment that runs hermes"
  default     = "platform-agent"
}

# The onboarding reports live on the shell sandbox, so an earlier run's
# INVENTORY files are cleared there too. An install without a
# sandbox matches no pod and skips that step.
variable "sandbox_selector" {
  type        = string
  description = "Label selector for the shell sandbox pods in agent_namespace"
  default     = "app=platform-agent-shell"
}

variable "sandbox_container" {
  type        = string
  description = "Container inside the shell sandbox pods"
  default     = "shell"
}

# The first-run cluster card cap (#3702): bootstrap_handoff.first_run_cluster_cards
# reads it from .first_run_cluster_cards in the agent's data dir, defaults to 6
# and clamps to 1..24 (FIRST_RUN_CLUSTER_CARDS_MAX). The eval fleet has fewer
# Cluster Agents than the default, so a case that grades the cap sets it lower
# here. Null writes no file and leaves an operator's alone; either way, a cap an
# earlier run of this stack left behind is removed (main.tf, step 2).
variable "first_run_cluster_cards" {
  type        = number
  description = "Cluster Agent cards one first-run sweep files, written to the agent's .first_run_cluster_cards before the re-arm; null writes nothing"
  default     = null

  validation {
    # coalesce, because a null reaching floor() errors rather than failing the condition.
    condition     = floor(coalesce(var.first_run_cluster_cards, 1)) == coalesce(var.first_run_cluster_cards, 1) && coalesce(var.first_run_cluster_cards, 1) >= 1 && coalesce(var.first_run_cluster_cards, 1) <= 24
    error_message = "first_run_cluster_cards must be null or a whole number from 1 to 24, the range bootstrap_handoff.first_run_cluster_cards honours; past it the gate clamps the value and the case would grade a cap it did not set."
  }
}
