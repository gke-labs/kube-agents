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
# reach the stack is declared here. An INJECTED variable this stack does not
# declare is dropped silently (which is why `cluster_name`, `node_count` and
# `machine_type` are absent -- this stack builds no cluster), while a variable
# named in a task.yaml `variables:` block and NOT declared here raises
# ConfigError. So the task file and this one are a matched pair.

# The cluster the Platform Agent pod runs in. The Session KV daemon this stack
# posts to binds loopback inside that pod, so the only way in is a kubectl exec
# against it; and the workload is planted on the same cluster because the card
# tells the agent to read the live object before concluding anything.
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
# The planted drift. Most of what follows is also written into
# bench/tasks/gitops-drift-load-bearing-change-triage/task.yaml -- in its
# prompt, which names the namespace, and in its safeguards, which assert the
# loosened setting and the replica count came out unchanged. Change one of
# those and change both.
#
# workload_image and workload_command are the exceptions: the task names
# neither, and both are declared here because the scenario depends on their
# values rather than because anything asserts them.
# ---------------------------------------------------------------------------

variable "drift_namespace" {
  type        = string
  description = "Namespace the workload is planted in, and torn down with"
  default     = "eval-drift-privilege"
}

# Carries no diagnostic content, deliberately: the report_contains objective
# keyed on this name proves the report identified the right object, and a name
# like "eval-needs-root" would answer the question the judge is asking.
variable "workload_name" {
  type        = string
  description = "Name of the Deployment whose runAsNonRoot setting was changed out of band"
  default     = "eval-privilege-workload"
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

# The field manager standing in for the GitOps controller. It owns the declared
# spec, and the out-of-band edit takes one field from it -- the transfer is
# what the detector's join reads off the live object.
variable "gitops_field_manager" {
  type        = string
  description = "Server-side-apply field manager that owns the declared spec"
  default     = "argocd-controller"
}

# Passed explicitly rather than left to kubectl's default, so the name in the
# payload's ownership block is one this stack chose and can assert, not one a
# kubectl version picked.
variable "drift_field_manager" {
  type        = string
  description = "Field manager the out-of-band edit claims the securityContext field under"
  default     = "kubectl-edit"
}

# The principal the audit entry would have carried. Deliberately not a
# *.gserviceaccount.com address: the detector's classifier drops automation
# principals upstream of the daemon, so a record that reached the inject route
# is one it judged to be a person, and a fixture that says otherwise would
# describe a record that cannot exist.
variable "drift_principal" {
  type        = string
  description = "Authenticated identity the audit entry recorded as making the change"
  default     = "eval-operator@example.com"
  validation {
    condition     = !can(regex("[\"$\\\\`[:cntrl:]]", var.drift_principal))
    error_message = "drift_principal must not contain a double quote, dollar sign, backslash, backtick or a control character (a newline or tab included): it is spliced into the unquoted PAYLOAD heredoc, which is JSON built by the shell. A quote makes the inner payload unparseable and the daemon's 400 is then reported as 'the request did not reach the drift route'; a $name is expanded by bash at run time."
  }
}

# The image is load-bearing in the literal sense: it must run as root, because
# the whole scenario is that the DECLARED state cannot run it. busybox
# publishes no USER directive, so its processes are UID 0, and a kubelet
# handed runAsNonRoot: true with no runAsUser refuses to start the container at
# all -- CreateContainerConfigError, "container has runAsNonRoot and image will
# run as root".
#
# A variable rather than a literal so a reviewer can see that the choice is the
# scenario and not an arbitrary pick: swapping in an image with a non-root USER
# makes the plant's own assertion in step 2 fail loudly rather than silently
# producing a healthy workload and a case with no evidence in it.
variable "workload_image" {
  type        = string
  description = "Container image for workload_name. MUST run as root for the declared state to be unrunnable."
  default     = "busybox:1.36"
}

# The command has to NEED root, not merely default to it, and that distinction
# is the whole reason this stack was rebuilt (#3102, measured 2026-10-15).
#
# With a command like `sleep 3600`, which runs perfectly well as any uid, the
# scenario has a third way out: keep runAsNonRoot true and add
# runAsUser: 1000. That is a better answer than either revert or codify, the
# agent found it on two runs out of two, and a case grading "revert or codify"
# cannot score it -- worse, the agent labelled the same fix "Codify" once and
# "Revert" the next time, so a judge keyed on the taxonomy graded the label
# rather than the content.
#
# Writing into /etc closes that door with the filesystem rather than with a
# rule: /etc is root-owned and mode 755, so uid 1000 gets EACCES, the `&&`
# short-circuits, and the container exits non-zero. Step 3 plants exactly that
# attempt as revision 2 of the rollout, so the history itself shows the third
# option was tried and failed, and revert-or-codify is what is genuinely left.
variable "workload_command" {
  type        = string
  description = "Shell command for workload_name. MUST fail as a non-root uid, so the only working state is root."
  default     = "touch /etc/needs-root && sleep 3600"

  # Terraform substitutes this value before bash or the YAML parser sees it,
  # and it lands in two places that cannot defend themselves: a double-quoted
  # YAML flow scalar in the step-1 manifest, where a `"` ends the scalar and
  # the apply dies on a parse error naming no variable, and a double-quoted
  # bash string in step 3's progress line, where a `$(...)` is a substitution
  # the RUNNER executes. Neither is reachable from a task.yaml today -- no
  # case sets this and the default contains neither character -- so this is a
  # guard for the next person to edit the command, and it fails at plan time
  # with a sentence instead of at apply time with a YAML error.
  validation {
    condition     = !can(regex("[\"$\\\\`]", var.workload_command))
    error_message = "workload_command must not contain a double quote, dollar sign, backslash or backtick: it is spliced into a YAML flow scalar and into a double-quoted shell string, and either character escapes its context. Rewrite the command without them, or change the rendering in main.tf to a YAML block scalar first."
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
