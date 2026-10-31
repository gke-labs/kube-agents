variable "project_id" {
  description = "The GCP project holding the host cluster and the drift-audit topic."
  type        = string
}

variable "host_cluster_name" {
  description = <<-EOT
    The host cluster the runner deployed. It reaches the synthetic records as
    resource.labels.cluster_name, which is how the detector routes the live
    record's join to this cluster (gitops-drift-noise/variables.tf has the
    longer argument).
  EOT
  type        = string
}

variable "host_cluster_location" {
  description = "The host cluster's region. Reaches the records as resource.labels.location."
  type        = string
}

variable "drift_topic" {
  description = "The Pub/Sub topic the detector's subscription is attached to. Records are published here directly, as gitops-drift-noise does."
  type        = string
  default     = "platform-agent-drift-audit"
}

variable "namespace" {
  description = "Namespace holding the live change, and named by the backlog records."
  type        = string
  default     = "eval-drift-backlog"
}

variable "live_workload" {
  description = "The Deployment the live change edits. The report must name this one."
  type        = string
  default     = "eval-drift-backlog-api"
}

variable "live_container" {
  description = "The container inside live_workload whose memory limit moves."
  type        = string
  default     = "api"
}

variable "gitops_field_manager" {
  description = "The field manager owning live_workload's declared spec."
  type        = string
  default     = "argocd-controller"
}

variable "drift_field_manager" {
  description = "The field manager the live change applies as."
  type        = string
  default     = "kubectl-edit"
}

variable "declared_memory" {
  description = "The memory limit gitops_field_manager declares on live_container."
  type        = string
  default     = "256Mi"
}

variable "drifted_memory" {
  description = "The memory limit the live change raises live_container to, and what the safeguard asserts is still in place."
  type        = string
  default     = "512Mi"
}

variable "live_principal" {
  description = "The principal on the live record. It has to reach the human tier (gitops-drift-noise/variables.tf, human_principal)."
  type        = string
  default     = "ada@example.com"
  validation {
    condition     = can(regex("^[^@[:space:]]+@[^@[:space:]]+$", var.live_principal)) && !startswith(var.live_principal, "system:") && !endswith(lower(var.live_principal), ".gserviceaccount.com")
    error_message = "live_principal must be an address the classifier sorts as human: an @ with something either side, no system: prefix, no .gserviceaccount.com suffix."
  }
}

variable "backlog_principal" {
  description = <<-EOT
    The principal on the backlog records, standing for the installer whose
    account an install's Helm apply runs under. Human tier for the same
    reason as live_principal, and different from it so the digest's count by
    principal is checkable.
  EOT
  type        = string
  default     = "installer@example.com"
  validation {
    condition     = can(regex("^[^@[:space:]]+@[^@[:space:]]+$", var.backlog_principal)) && !startswith(var.backlog_principal, "system:") && !endswith(lower(var.backlog_principal), ".gserviceaccount.com")
    error_message = "backlog_principal must be an address the classifier sorts as human, or the records are dropped before they can be backlog."
  }
}

variable "lone_principal" {
  description = <<-EOT
    The principal on one late record published with the backlog burst: a
    person whose single edit landed just before the detector restarted. The
    daemon cards a principal with fewer than three late rows, so this record
    must get a card, not a digest line.
  EOT
  type        = string
  default     = "grace@example.com"
  validation {
    condition     = can(regex("^[^@[:space:]]+@[^@[:space:]]+$", var.lone_principal)) && !startswith(var.lone_principal, "system:") && !endswith(lower(var.lone_principal), ".gserviceaccount.com")
    error_message = "lone_principal must be an address the classifier sorts as human."
  }
}

variable "kubectl_record_count" {
  description = "How many late records the backlog principal publishes with a kubectl user agent. The detector cards a late kubectl write, so each must get a card although the principal's burst is digested."
  type        = number
  default     = 3
  validation {
    condition     = var.kubectl_record_count >= 1 && floor(var.kubectl_record_count) == var.kubectl_record_count
    error_message = "kubectl_record_count must be a whole number of at least 1."
  }
}

variable "card_wait_seconds" {
  description = "How long, after the live card, to wait for every late record that must be carded to have a card on the board."
  type        = number
  default     = 900
  validation {
    condition     = var.card_wait_seconds >= 60 && floor(var.card_wait_seconds) == var.card_wait_seconds
    error_message = "card_wait_seconds must be a whole number of at least 60."
  }
}

variable "backlog_record_count" {
  description = "How many records backlog_principal publishes, with Helm's user agent, while the detector is stopped: the burst to be digested."
  type        = number
  default     = 6
  validation {
    condition     = var.backlog_record_count >= 3 && floor(var.backlog_record_count) == var.backlog_record_count
    error_message = "backlog_record_count must be a whole number of at least 3: the daemon cards a principal with fewer than three late rows, so a smaller burst is carded, not digested."
  }
}

variable "agent_namespace" {
  description = "Namespace of the kube-agents install. Declared because hack/ci-eval-pr.sh exports TF_VAR_agent_namespace."
  type        = string
  default     = "kubeagents-system"
}

variable "agent_deployment" {
  description = "The gateway Deployment every probe execs into."
  type        = string
  default     = "platform-agent-gateway"
}

variable "agent_container" {
  description = "The gateway container holding the kanban board and the ledger."
  type        = string
  default     = "platform-agent"
}

variable "detector_container" {
  description = "The gateway container the drift detector runs in, under start-services.sh, which relaunches it when it exits."
  type        = string
  default     = "agent-api-auth"
}

variable "detector_restart_timeout_seconds" {
  description = "How long to wait for start-services.sh to relaunch the detector after the fixture kills it."
  type        = number
  default     = 300
  validation {
    condition     = var.detector_restart_timeout_seconds >= 30 && floor(var.detector_restart_timeout_seconds) == var.detector_restart_timeout_seconds
    error_message = "detector_restart_timeout_seconds must be a whole number of at least 30."
  }
}

variable "backlog_timeout_seconds" {
  description = "How long to wait, after the relaunch, for every backlog record to have a ledger row."
  type        = number
  default     = 300
  validation {
    condition     = var.backlog_timeout_seconds >= 30 && floor(var.backlog_timeout_seconds) == var.backlog_timeout_seconds
    error_message = "backlog_timeout_seconds must be a whole number of at least 30."
  }
}

variable "card_timeout_seconds" {
  description = "How long to wait for the live record's card to reach a terminal status (gitops-drift-noise/variables.tf has the sizing)."
  type        = number
  default     = 1800
  validation {
    condition     = var.card_timeout_seconds >= 15 && floor(var.card_timeout_seconds) == var.card_timeout_seconds
    error_message = "card_timeout_seconds must be a whole number of at least 15, the poll's own step."
  }
}

variable "settle_seconds" {
  description = "How long to wait after the live card before the verdict. Above the daemon's 60-second digest quiet period, so the backlog rows have reached their digest state."
  type        = number
  default     = 120
  validation {
    condition     = var.settle_seconds >= 90 && floor(var.settle_seconds) == var.settle_seconds
    error_message = "settle_seconds must be a whole number of at least 90: below the digest's quiet period plus a margin, a pending row means nothing."
  }
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
