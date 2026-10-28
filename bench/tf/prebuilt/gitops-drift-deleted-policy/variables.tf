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

# The deployer scans this directory's *.tf only, so every variable that has to
# reach the stack is declared here. Two rules it enforces are worth knowing
# before adding one: an INJECTED variable this stack does not declare is
# dropped silently (which is why `cluster_name`, `node_count` and
# `machine_type` are absent -- this stack builds no cluster and would only be
# confused by them), while a variable named in a task.yaml `variables:` block
# and NOT declared here raises ConfigError. So the task file and this one are a
# matched pair.

# The cluster the Platform Agent pod runs in. The Session KV daemon this stack
# posts to binds loopback inside that pod, so the only way in is a kubectl exec
# against it; and the deleted policy's namespace is planted on the same cluster
# because the card tells the agent to read the live state before concluding
# anything, and a namespace on a cluster the agent cannot reach would make the
# card unanswerable.
variable "host_cluster_name" {
  type        = string
  description = "Name of the cluster the Platform Agent runs in; the drift is planted here."
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

# Where the agent install lives. The exec below lands in the pod behind this
# Deployment.
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

# The container the daemon runs in, and therefore the container the exec has to
# land in -- not because the daemon is unreachable from the others (every
# container in a pod shares one network namespace) but because SESSION_KV_API_KEY
# is on this container's environment and nowhere else, and reading the key from
# inside the pod is what keeps it off the runner and out of the CI log.
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

# ---------------------------------------------------------------------------
# The planted drift. Every value below is also written into
# bench/tasks/gitops-drift-deleted-policy-triage/task.yaml -- in its prompt,
# which names the namespace the agent looks for, and in its safeguards, which
# assert the policy is still gone and the workload untouched. Change one and
# change both.
# ---------------------------------------------------------------------------

# Static, for the reason the sibling stack's equivalent is static: Boskos
# leases one project per run so no two runs share a host cluster, and the
# runner's task loop is sequential. The card poll does not rely on that anyway
# -- it matches the run's own insert id, minted per apply.
variable "drift_namespace" {
  type        = string
  description = "Namespace the protected workload and its policy are planted in, and torn down with"
  default     = "eval-drift-policy"
}

# Carries no diagnostic content, deliberately. The report_contains objective
# keyed on this name proves the report identified the right object; a name like
# "eval-default-deny" would also answer the question the impact objective asks,
# which the agent is supposed to reach by reading what the namespace looks like
# now rather than by transcribing a name.
variable "policy_name" {
  type        = string
  description = "Name of the NetworkPolicy applied by the GitOps manager and then deleted out of band"
  default     = "eval-workload-netpol"
}

# The workload the policy was protecting. Present so the namespace has
# something whose exposure the deletion changes: the impact the report has to
# weigh is that these pods now accept traffic from anywhere in the cluster, and
# an empty namespace would make that sentence vacuous.
variable "workload_name" {
  type        = string
  description = "Name of the Deployment the deleted policy was protecting"
  default     = "eval-policy-workload"
}

variable "workload_replicas" {
  type        = number
  description = "Replicas of workload_name. The task's safeguard asserts this value came out unchanged."
  default     = 2
  validation {
    condition     = var.workload_replicas >= 1 && floor(var.workload_replicas) == var.workload_replicas
    error_message = "workload_replicas must be a whole number of at least 1. At 0 the Deployment applies, `kubectl rollout status` reports success immediately because every condition it waits on compares against spec.replicas, and the plant goes on to inject a record whose card asks the agent to weigh an exposure that is true of no pods."
  }
}

# The field manager standing in for the GitOps controller. It owns the policy
# and the workload at plant time, which is what makes the deletion out-of-band
# rather than merely recent.
#
# Its ownership of the POLICY is not observable after the deletion -- nothing
# can read managedFields off an object that no longer exists, which is exactly
# why the record below carries `join: no_object`. It is applied under this
# manager anyway, so the scenario is coherent rather than merely asserted.
variable "gitops_field_manager" {
  type        = string
  description = "Server-side-apply field manager that owned the policy and owns the workload"
  default     = "argocd-controller"
}

# The principal the audit entry would have carried. Deliberately not a
# *.gserviceaccount.com address: the detector's own classifier drops automation
# principals upstream of the daemon, so a record that reached the inject route
# is one it judged to be a person, and a fixture that says otherwise would
# describe a record that cannot exist.
variable "drift_principal" {
  type        = string
  description = "Principal the planted record names as making the deletion; the real audit entry carries the identity that runs the stack"
  default     = "eval-operator@example.com"
  validation {
    condition     = !can(regex("[\"$\\\\`[:cntrl:]]", var.drift_principal))
    error_message = "drift_principal must not contain a double quote, dollar sign, backslash, backtick or a control character (a newline or tab included): it is spliced into a double-quoted bash word in the drift_build_payload call, so a quote or a backtick ends or re-opens that word and a $name is expanded by bash before the value reaches the builder. The JSON itself is safe -- build_payload.py places every argument with json.dumps -- so what this guards is the shell layer in front of it."
  }
}

# Identify the CI run that planted this, so a janitor can find what a run killed
# before teardown left behind. Both are empty outside CI.
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
