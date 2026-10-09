variable "project_id" {
  description = "GCP Project ID hosting the audit logs, the topic, and the subscription"
  type        = string
}

variable "cluster_names" {
  description = "GKE clusters to export audit logs for. Empty (the default) exports every cluster in the project, leaving the detector to route on resource.labels.cluster_name."
  type        = list(string)
  default     = []
}

variable "exclude_machine_lease_heartbeats" {
  description = "Drop coordination.k8s.io Lease writes made by machine identities (system: principals and *.iam.gserviceaccount.com service accounts) at the sink. These are leader-election and node heartbeats, never GitOps-managed, and measured at ~96% of all mutating calls. Lease writes by human principals still pass through. Set false to export the unfiltered stream for debugging."
  type        = bool
  default     = true
}

variable "topic_name" {
  description = "Pub/Sub topic the Log Router sink publishes audit entries to"
  type        = string
  default     = "platform-agent-drift-audit"
}

variable "topic_publishers" {
  description = "Extra members granted roles/pubsub.publisher on the topic, beyond the sink's own writer identity. Empty on a real install: the Log Router is the only thing that should be able to put an audit record on this topic, and anything that can publish here can make the detector report drift that never happened. The evaluation pool sets it to its CI runners, whose drift cases publish synthetic records so the detector's classifier is exercised end to end rather than bypassed -- a bench fixture cannot reach the classifier any other way, because every identity it can authenticate as is a service account the classifier is right to drop. Do not add the agent's own service account: the detector would then be reading a stream its own pod can write."
  type        = list(string)
  default     = []

  validation {
    condition     = alltrue([for member in var.topic_publishers : can(regex("^(serviceAccount|user|group|principal|principalSet):", member))])
    error_message = "each topic_publishers entry must be a fully qualified IAM member (serviceAccount:, user:, group:, principal: or principalSet:)."
  }
}

variable "subscription_name" {
  description = "Pub/Sub subscription the drift detector pulls audit entries from"
  type        = string
  default     = "platform-agent-drift-audit-sub"
}

variable "sink_name" {
  description = "Name of the Log Router sink exporting GKE audit logs to the topic"
  type        = string
  default     = "platform-agent-drift-audit-sink"
}

variable "sink_drain_duration" {
  description = "How long a destroy waits, after deleting the sink, before removing the topic and the sink's publish grant. Cloud Logging stops exporting some minutes after the sink is gone, and an export that lands in that gap mails every project owner a sink configuration error; the wait is a timer because Logging offers nothing to wait on. The 120s default is a chosen margin, not a measured convergence time: Google documents no bound, so lengthening it buys margin and shortening it trades destroy time for the chance of that email. Paid once per destroy and never on apply. A change to this takes effect only once an apply has recorded it: time_sleep reads destroy_duration from state when it is destroyed, because a provider's delete is handed prior state and no configuration, so raising it and going straight to a destroy waits the old value. Apply first, then destroy."
  type        = string
  default     = "120s"

  # Narrower than a Go duration on purpose: time_sleep takes a number followed
  # by exactly one of ms, s, m or h, and refuses both the multi-unit form
  # ("2m30s") and the sub-millisecond units Go accepts. Matching the provider
  # here fails the variable with a usable message rather than failing inside
  # the provider after the plan.
  #
  # An empty string is refused with the rest. Unlike the override below it has
  # no sensible reading -- there is no "no duration" -- so the message says how
  # to get the default back rather than the condition admitting it.
  validation {
    condition     = can(regex("^[0-9]+(\\.[0-9]+)?(ms|s|m|h)$", var.sink_drain_duration))
    error_message = "sink_drain_duration must be a number followed by one of ms, s, m or h, e.g. 120s or 2m. time_sleep does not accept the multi-unit form (2m30s). An empty value is not the default: a blank TF_VAR_ line exports \"\" and overrides the default, so remove the line rather than blanking it."
  }
}

variable "logging_identity_propagation_duration" {
  description = "How long the apply waits, after asking Service Usage to mint the project's Logging service agent, before granting it roles/pubsub.publisher on the topic. A project that did not already have the agent cannot bind it the instant the call returns, and the grant fails with \"Service account service-<project-number>@gcp-sa-logging.iam.gserviceaccount.com does not exist\" -- measured at about one in five projects. The wait is a timer because IAM reports a missing account and a not-yet-propagated one identically, so there is nothing to poll. The 60s default is a chosen margin, not a measured propagation time; raise it if the apply still fails that way, and the raised value is paid on the re-apply -- the wait is keyed on this variable as well as on the identity, because raising create_duration on its own is an in-place update that runs no delay. Paid on the first apply that carries this resource -- the first apply of the module on a new install, the next apply of any kind on an install that already had it -- and after that only on an apply that re-mints the identity or changes this value, the two things the wait is keyed on. Lower it only on a project whose Logging agent already exists, where the wait buys nothing -- and before the first apply that carries the wait, because lowering it afterwards re-creates the wait and pays the new lower figure once rather than refunding the one already paid."
  type        = string
  default     = "60s"

  # Same form as sink_drain_duration above, for the same reason: time_sleep
  # takes a number and exactly one of ms, s, m or h.
  validation {
    condition     = can(regex("^[0-9]+(\\.[0-9]+)?(ms|s|m|h)$", var.logging_identity_propagation_duration))
    error_message = "logging_identity_propagation_duration must be a number followed by one of ms, s, m or h, e.g. 60s or 2m. time_sleep does not accept the multi-unit form (1m30s). An empty value is not the default: a blank TF_VAR_ line exports \"\" and overrides the default, so remove the line rather than blanking it."
  }
}

variable "sink_writer_identity_override" {
  description = "The principal to grant roles/pubsub.publisher on the topic, overriding the service-<project-number>@gcp-sa-logging.iam.gserviceaccount.com the module derives. Include the \"serviceAccount:\" prefix. The module derives the identity rather than reading it off the sink so the grant can precede the sink, and a sink carrying some other writer identity would otherwise fail the sink's postcondition on every subsequent plan with no way out short of editing the module. Set this to whatever Logging reports for the sink and both the grant and the postcondition follow it. Leave null unless an apply has told you to set it."
  type        = string
  default     = null

  # "" is admitted alongside null because it is how an operator turns the
  # override off. The composition reaches this through a TF_VAR_ line in
  # install.env, and blanking that line rather than deleting it exports ""
  # through `set -a` -- which overrides the default, as lifecycle.sh says of
  # the same pattern on drift_pubsub_subscription. Refusing it would answer
  # someone switching the override off by telling them to add a prefix to it.
  # expected_sink_writer_identity coalesces "" to the derived identity, which
  # is what they meant.
  validation {
    condition     = var.sink_writer_identity_override == null || var.sink_writer_identity_override == "" || can(regex("^serviceAccount:.+@.+$", var.sink_writer_identity_override))
    error_message = "sink_writer_identity_override must carry the \"serviceAccount:\" prefix, as writer_identity does. Leave it unset, or empty, to use the identity the module derives."
  }
}

variable "ack_deadline_seconds" {
  description = "How long the detector has to ack a message before Pub/Sub redelivers it. The ack follows the managedFields join, and synchronous pull does not extend the deadline underneath a running handler, so this has to cover a whole batch's live-object lookups. The detector caps a batch's join with its own --batch-join-budget flag, defaulting to 30s, half this default; the two are not wired together — the detector reads this value at startup and warns when its budget takes more than half of it, but it does not adopt it — so lowering this below 60 means passing a smaller --batch-join-budget to the detector to match."
  type        = number
  default     = 60

  validation {
    condition     = var.ack_deadline_seconds >= 10 && var.ack_deadline_seconds <= 600
    error_message = "ack_deadline_seconds must be between 10 and 600."
  }
}

variable "message_retention_duration" {
  description = "How long Pub/Sub retains unacked messages, as a duration string. Defaults to 2678400s (31 days), the subscription maximum; Pub/Sub's own default is 7 days. Retention that lapses drops drift events silently, hence the ceiling."
  type        = string
  default     = "2678400s"
}

variable "retry_minimum_backoff" {
  description = "Lower bound of the exponential backoff applied to redelivery after a nack"
  type        = string
  default     = "10s"
}

variable "retry_maximum_backoff" {
  description = "Upper bound of the exponential backoff applied to redelivery after a nack"
  type        = string
  default     = "600s"
}
