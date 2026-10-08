# The planted state for gitops-drift-noise-filtered-triage, as variables.
#
# These are the spec under PLANTED STATE REQUIRED in the case's task.yaml,
# moved here as that section asks: the prompt, the verification_spec and this
# file all name the same objects, and declaring them once is what stops the
# three drifting apart. A change to any value here changes the case, so the
# defaults are the case and an override is a local experiment.
#
# The sibling stack (bench/tf/prebuilt/gitops-drift) is the model for the
# shape. What it is not a model for is the route: it posts its record straight
# to the Session KV daemon's `gitops-drift` inject, which starts at the last
# step of the chain. This case grades the classifier, so its records go to the
# drift-audit Pub/Sub topic and arrive at the detector the way a real audit
# record does.

variable "project_id" {
  description = "The GCP project holding the host cluster and the drift-audit topic."
  type        = string
}

variable "host_cluster_name" {
  description = <<-EOT
    The host cluster the runner deployed. No task may choose it.

    This reaches the synthetic records as resource.labels.cluster_name, and
    getting it wrong is not a cosmetic error: parseAuditEntry takes the
    record's cluster identity from resource.labels (audit.go) and the joiner
    looks that identity up in its credentials map (join.go). A record naming a
    cluster the detector holds no credentials for is forwarded without
    ownership, the card reads "Field ownership: not read", and the report has
    nothing to name the field managers from -- which expected_output requires.
  EOT
  type        = string
}

variable "host_cluster_location" {
  description = "The host cluster's region. Reaches the records as resource.labels.location."
  type        = string
}

variable "drift_topic" {
  description = <<-EOT
    The Pub/Sub topic the detector's subscription is attached to.

    The fixture publishes to the topic rather than making real cluster writes
    and waiting for the Log Router: the export lag is minutes on top of the
    run's budget, and every identity a bench run can authenticate as is a
    .gserviceaccount.com one the classifier is right to drop, so a real write
    would never reach the human tier this case grades.
  EOT
  type        = string
  default     = "platform-agent-drift-audit"
}

# ─── The two namespaces ──────────────────────────────────────────────────────
#
# Neither name says which is which. The prompt carries both, and a name like
# "noise" would hand the agent the answer to the thing being measured.

variable "human_namespace" {
  description = "Namespace holding the one change a person made."
  type        = string
  default     = "eval-drift-payments"
}

variable "churn_namespace" {
  description = "Namespace holding the controller and autoscaler activity."
  type        = string
  default     = "eval-drift-platform"
}

# ─── The three workloads ─────────────────────────────────────────────────────
#
# All three carry the eval-drift- prefix so a sweep can tell this case's
# objects from another's. The namespace name is a prefix of the human change's
# workload name (eval-drift-payments / eval-drift-payments-api) on purpose:
# see the case header on why a report that names a namespace cannot be graded
# as naming a workload.

variable "human_workload" {
  description = "The Deployment a person changed. The report must name this one and no other."
  type        = string
  default     = "eval-drift-payments-api"
}

variable "human_container" {
  description = "The container inside human_workload whose memory limit moves."
  type        = string
  default     = "api"
}

variable "churn_workloads" {
  description = <<-EOT
    The Deployments the controllers churn. The forbidden_phrases check keys on
    these two names: a report that lists either alongside the real change has
    been handed unfiltered noise, which is the regression this case catches.
  EOT
  type        = list(string)
  default     = ["eval-drift-ledger-worker", "eval-drift-checkout-web"]
}

# ─── The change under test ───────────────────────────────────────────────────

variable "gitops_field_manager" {
  description = <<-EOT
    The field manager owning human_workload's declared spec. The report must
    name this one as the owner the drifting write took the field from.
  EOT
  type        = string
  default     = "argocd-controller"
}

variable "drift_field_manager" {
  description = "The field manager the out-of-band write applies as."
  type        = string
  default     = "kubectl-edit"
}

variable "declared_memory" {
  description = "The memory limit gitops_field_manager declares on human_container."
  type        = string
  default     = "256Mi"
}

variable "drifted_memory" {
  description = <<-EOT
    The memory limit the human change raises human_container to, and what the
    safeguard asserts is still in place when the run ends.
  EOT
  type        = string
  default     = "512Mi"
}

variable "human_principal" {
  description = <<-EOT
    The principal on the human record, and the whole of what puts it in the
    human tier.

    Classify returns on the first match: empty, then a `system:` prefix, then
    the --automation-principals allowlist, then a .gserviceaccount.com suffix,
    and only after all four does it consult isHuman (classify.go). This value
    survives all four and reaches isHuman, which with no --human-domains
    configured asks only for a domain -- an "@" with something either side.
    The detector copies protoPayload.authenticationInfo.principalEmail into
    AuditRecord.Principal verbatim (audit.go) with nothing cross-checking it
    against the live object, which is why a synthetic record can carry it.
  EOT
  type        = string
  default     = "ada@example.com"

  validation {
    condition     = can(regex("^[^@[:space:]]+@[^@[:space:]]+$", var.human_principal))
    error_message = "human_principal needs an @ with something either side, or isHuman rejects it and the record never reaches the inject."
  }

  validation {
    condition     = !startswith(var.human_principal, "system:") && !endswith(var.human_principal, ".gserviceaccount.com")
    error_message = "human_principal must survive Classify's prefix and suffix tests; a system: prefix or a .gserviceaccount.com suffix is sorted as automation before isHuman is consulted."
  }
}

variable "churn_principals" {
  description = <<-EOT
    The principals on the eleven automation records, spread across the two
    shapes Classify drops before isHuman: a `system:` prefix and a
    .gserviceaccount.com suffix. Eleven records are minted from this list by
    cycling it, so its length need not be eleven.
  EOT
  type        = list(string)
  default = [
    "system:serviceaccount:kube-system:replicaset-controller",
    "system:serviceaccount:kube-system:horizontal-pod-autoscaler",
    "system:serviceaccount:kube-system:deployment-controller",
    "cluster-autoscaler@container-engine-robot.iam.gserviceaccount.com",
  ]

  validation {
    condition = alltrue([
      for p in var.churn_principals :
      startswith(p, "system:") || endswith(p, ".gserviceaccount.com")
    ])
    error_message = "every churn principal must be one Classify drops: a system: prefix or a .gserviceaccount.com suffix. One that reaches the human tier would file a card the case grades as unfiltered noise."
  }
}

variable "churn_record_count" {
  description = <<-EOT
    How many automation records accompany the one human record.

    Eleven, so the human change is a small minority of the burst and a
    pipeline that forwards everything is obvious in the report rather than
    marginal.
  EOT
  type        = number
  default     = 11

  validation {
    condition     = var.churn_record_count >= 1
    error_message = "churn_record_count must be at least 1; with no churn the case measures delivery, which is the sibling's job."
  }
}

# ─── Reading the result back ─────────────────────────────────────────────────

variable "agent_container" {
  description = "The container on the gateway pod holding the kanban board, read by the poll."
  type        = string
  default     = "platform-agent"
}

variable "poll_timeout_seconds" {
  description = <<-EOT
    How long the stack waits for a card carrying the human record's insertId.

    The stack does not return until that card is on the board, and the insertId
    is the lever: the board is shared and persists between runs, and the plant
    is identical every repetition, so without a run-scoped poll the agent can
    reproduce the PREVIOUS repetition's card and pass every check on the
    repetition this case exists to red. _drift_task_body renders the id into
    the card body verbatim (`insertId=<id>`, session_kv_server.py), so the
    literal is what makes the poll run-scoped.
  EOT
  type        = number
  default     = 900
}

variable "prow_build_id" {
  description = "Prow's build id, for the CI labels. Empty outside CI."
  type        = string
  default     = ""
}

variable "prow_pull_number" {
  description = "Prow's pull number, for the CI labels. Empty outside CI."
  type        = string
  default     = ""
}
