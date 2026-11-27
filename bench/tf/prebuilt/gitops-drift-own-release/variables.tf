variable "project_id" {
  description = "The GCP project holding the host cluster and the drift-audit topic."
  type        = string
}

variable "host_cluster_name" {
  description = <<-EOT
    The host cluster the runner deployed. It reaches the synthetic records as
    resource.labels.cluster_name, which is how the detector routes each
    record's join to this cluster (gitops-drift-noise/variables.tf has the
    longer argument). The own-release rule applies to this cluster only.
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
  description = "Namespace holding the planted workload outside the release, and the verdict ConfigMap."
  type        = string
  default     = "eval-drift-own-release"
}

variable "live_workload" {
  description = "The planted Deployment outside the release. A write to it by Helm's client must still get a card."
  type        = string
  default     = "eval-drift-own-release-api"
}

variable "live_container" {
  description = "The container whose memory limit changes hands on the planted Deployment."
  type        = string
  default     = "api"
}

variable "gitops_field_manager" {
  description = "The field manager the planted Deployment is first applied under."
  type        = string
  default     = "argocd-controller"
}

variable "drift_field_manager" {
  description = "The field manager that takes the memory limit on the planted Deployment: Helm's, to match the user agent on its record."
  type        = string
  default     = "helm"
}

variable "declared_memory" {
  description = "The memory limit the planted Deployment is first applied with."
  type        = string
  default     = "256Mi"
}

variable "drifted_memory" {
  description = "The memory limit drift_field_manager moves it to."
  type        = string
  default     = "512Mi"
}

variable "installer_principal" {
  description = <<-EOT
    The principal on every record, standing for the person whose account an
    upgrade's writes run under. One principal for the release writes and the
    two controls, so what separates a digest line from a card is the object
    and the client, not who wrote. It has to reach the human tier
    (gitops-drift-noise/variables.tf, human_principal).
  EOT
  type        = string
  default     = "installer@example.com"
  validation {
    condition     = can(regex("^[^@[:space:]]+@[^@[:space:]]+$", var.installer_principal)) && !startswith(var.installer_principal, "system:") && !endswith(lower(var.installer_principal), ".gserviceaccount.com")
    error_message = "installer_principal must be an address the classifier sorts as human, or the records are dropped before the rule sees them."
  }
}

variable "release_record_count" {
  description = "How many records name objects of the install's own Helm release with Helm's user agents. One more, a kubectl write to a kube-agents CRD, is always published with them."
  type        = number
  default     = 4
  validation {
    condition     = var.release_record_count >= 2 && floor(var.release_record_count) == var.release_record_count
    error_message = "release_record_count must be a whole number of at least 2: with the CRD record that is three, and the daemon cards a principal with fewer than three rows in a digest."
  }
}

variable "release_revision" {
  description = <<-EOT
    The revision in the name of the Helm release record Secret whose create
    the fixture publishes first and whose update it publishes last, as Helm
    creates one at the start of every upgrade and updates it at the end.
    Those two records are what tell the daemon when an upgrade by
    installer_principal is under way. No Secret is created: the detector
    reads the name off the record. Far above any real revision, so the name
    is never a real record's.
  EOT
  type        = number
  default     = 900001
  validation {
    condition     = var.release_revision >= 1 && floor(var.release_revision) == var.release_revision
    error_message = "release_revision must be a whole number of at least 1: Helm names a release record by its revision."
  }
}

variable "burst_principal" {
  description = <<-EOT
    The principal on a burst of writes to release objects with Helm's user
    agent and no upgrade behind it: a person changing kube-agents' own
    objects by hand. Each record must get a card, although the count would
    pass the daemon's rule of three. Human tier, and different from
    installer_principal, who has an upgrade under way.
  EOT
  type        = string
  default     = "grace@example.com"
  validation {
    condition     = can(regex("^[^@[:space:]]+@[^@[:space:]]+$", var.burst_principal)) && !startswith(var.burst_principal, "system:") && !endswith(lower(var.burst_principal), ".gserviceaccount.com")
    error_message = "burst_principal must be an address the classifier sorts as human."
  }
}

variable "burst_record_count" {
  description = "How many records burst_principal publishes. At least three, so that the daemon's rule of three alone would digest them and only the missing upgrade cards them."
  type        = number
  default     = 3
  validation {
    condition     = var.burst_record_count >= 3 && floor(var.burst_record_count) == var.burst_record_count
    error_message = "burst_record_count must be a whole number of at least 3: fewer are carded by the rule of three whatever this change does."
  }
}

variable "product_crd" {
  description = "The kube-agents CRD the kubectl record names. The installer applies the CRDs with kubectl, so this record must be digested although kubectl sent it."
  type        = string
  default     = "platformagents.kubeagents.x-k8s.io"
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
  description = "The gateway container the drift detector runs in."
  type        = string
  default     = "agent-api-auth"
}

variable "record_timeout_seconds" {
  description = "How long to wait for every release record to have a ledger row."
  type        = number
  default     = 300
  validation {
    condition     = var.record_timeout_seconds >= 30 && floor(var.record_timeout_seconds) == var.record_timeout_seconds
    error_message = "record_timeout_seconds must be a whole number of at least 30."
  }
}

variable "card_timeout_seconds" {
  description = "How long to wait for the planted workload's card to reach a terminal status (gitops-drift-noise/variables.tf has the sizing)."
  type        = number
  default     = 1800
  validation {
    condition     = var.card_timeout_seconds >= 15 && floor(var.card_timeout_seconds) == var.card_timeout_seconds
    error_message = "card_timeout_seconds must be a whole number of at least 15, the poll's own step."
  }
}

variable "card_wait_seconds" {
  description = "How long, after the planted workload's card, to wait for the kubectl control, each record of the burst and the write after the upgrade's end to have a card on the board."
  type        = number
  default     = 900
  validation {
    condition     = var.card_wait_seconds >= 60 && floor(var.card_wait_seconds) == var.card_wait_seconds
    error_message = "card_wait_seconds must be a whole number of at least 60."
  }
}

variable "settle_seconds" {
  description = "How long to wait after the cards before the verdict. Above the daemon's 60-second digest quiet period, so the release rows have reached their digest state."
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
