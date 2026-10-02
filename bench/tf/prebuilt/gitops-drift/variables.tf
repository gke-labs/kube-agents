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

# The cluster the Platform Agent pod runs in. Unlike prebuilt/autoops-incident,
# which needs this cluster because it is the only one k8s-event-watcher is
# watching, this stack needs it for a simpler reason: the Session KV daemon it
# posts to binds loopback inside that pod, so the only way in is a kubectl exec
# against that pod. The drifted object is planted on the same cluster because
# the card tells the agent to read the live object, and an object on a cluster
# the agent cannot reach would make the card unanswerable.
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

# Loopback, and the daemon binds nothing else: deploy/shared/docker-entrypoint.sh
# starts its uvicorn bound to the loopback interface on the port below, which is
# why the only way to reach it is an exec into the pod. A variable rather than a
# literal for the same reason agent_container is one: a port change in deploy/
# should surface here as a connection refused naming the port, not as a mystery.
variable "daemon_url" {
  type        = string
  description = "Base URL of the Session KV daemon, as seen from inside the agent pod"
  default     = "http://127.0.0.1:8699" # sanitizer: allow the daemon's own loopback bind, reachable only from inside the agent pod
}

# ---------------------------------------------------------------------------
# The planted drift. Every value below is also written into
# bench/tasks/gitops-drift-out-of-band-triage/task.yaml -- in its prompt, which
# names the namespace the agent looks for, and in its safeguards, which assert
# the replica count and the image came out unchanged. Change one and change
# both; the task file's `variables:` block sits directly above the checks that
# repeat them, so the pair is visible in one screen.
# ---------------------------------------------------------------------------

# Static, for the reason prebuilt/autoops-incident's equivalent is static:
# Boskos leases one project per run so no two runs share a host cluster, and
# the runner's task loop is sequential. The card poll does not rely on that
# anyway -- it matches the run's own insert id, which is minted per apply.
variable "drift_namespace" {
  type        = string
  description = "Namespace the drifted workload is planted in, and torn down with"
  default     = "eval-gitops-drift"
}

# Carries no diagnostic content, for the reason autoops-incident's workload_name
# does not: the report_contains objective keyed on this name proves the report
# identified the right object, and a name like "eval-scaled-by-hand" would also
# satisfy the objective asking what changed.
variable "workload_name" {
  type        = string
  description = "Name of the Deployment whose replica count was changed out of band"
  default     = "eval-drift-workload"
}

# The two numbers are the scenario. The Deployment is applied server-side at
# declared_replicas by the field manager named below, so that manager owns
# `.spec.replicas` in managedFields; the out-of-band edit then takes that field
# at live_replicas under a different manager. Both halves are real -- the agent
# reads the live object and finds exactly what the card describes -- which is
# what stops the case grading a report against a fiction.
variable "declared_replicas" {
  type        = number
  description = "What the GitOps manager applied, and therefore what git declares."
  default     = 1
}

variable "live_replicas" {
  type        = number
  description = "What the out-of-band edit left live. Must differ from declared_replicas."
  default     = 3
}

# The field manager standing in for the GitOps controller. A manager name the
# agent can recognise as a controller rather than a person, because the card
# asks it to weigh the declared state against the live one and the declared
# side needs an owner.
variable "gitops_field_manager" {
  type        = string
  description = "Server-side-apply field manager that owns the declared spec"
  default     = "argocd-controller"
}

# The field manager the out-of-band edit runs under. Passed explicitly rather
# than left to kubectl's default, so the name in the payload's ownership block
# is one this stack chose and can assert, not one a kubectl version picked.
variable "drift_field_manager" {
  type        = string
  description = "Field manager the out-of-band edit claims .spec.replicas under"
  default     = "kubectl-edit"
}

# The principal the audit entry would have carried. Deliberately not a
# *.gserviceaccount.com address: the detector's own classifier drops automation
# principals upstream of the daemon, so a record that reached the inject route
# is one it judged to be a person, and a fixture that says otherwise would
# describe a record that cannot exist.
variable "drift_principal" {
  type        = string
  description = "Authenticated identity the audit entry recorded as making the change"
  default     = "eval-operator@example.com"
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
