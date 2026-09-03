# Cloud Logging -> Pub/Sub delivery path for the drift detector.
#
# GKE audit logs cannot be read from the Kubernetes API. The control plane is
# managed, so the API server's audit backend is not ours to configure, and the
# stream surfaces only in Cloud Logging. This module builds the route out of
# Cloud Logging and into a subscription the detector pulls from.
#
# Why this sink filters what it does -- and deliberately does not filter more --
# is recorded at each decision below, next to the code it explains.

locals {
  # Mutating calls against GKE clusters, from the Admin Activity audit log.
  # Cloud Logging ANDs newline-separated expressions.
  #
  # Principals are broadly NOT filtered here. The detector classifies them
  # itself and needs the unfiltered volume visible to measure its own noise
  # profile; filtering in the sink would discard the denominators and make a
  # mistuned automation allowlist impossible to debug. The lease carve-out
  # below is the one deliberate exception.
  base_filter = <<-EOT
    logName="projects/${var.project_id}/logs/cloudaudit.googleapis.com%2Factivity"
    resource.type="k8s_cluster"
    protoPayload.methodName=~"create|patch|update|delete"
  EOT

  # Leader-election and node-heartbeat Leases dominate this stream and carry no
  # drift signal. A Lease is created at runtime by the controller that holds it,
  # never applied from a manifest, so there is no Git-side object for it to
  # diverge from. Measured over a 15-minute window on a two-cluster project,
  # leases.update plus leases.create were 9,558 of 10,000 sampled mutating
  # calls -- 95.6%.
  #
  # That 10,000 is the query's row cap, not the window's true total, so daily
  # figures do not come from it. They come from a separate untruncated count of
  # the surviving stream: 623 non-lease calls per 15 minutes, about 60k/day,
  # against roughly 1.35M/day unfiltered.
  #
  # The exclusion is scoped by principal rather than dropping leases outright,
  # so that a person running `kubectl patch lease` still reaches the detector.
  # That is not GitOps drift (nothing declared it), but it can knock an active
  # controller off its lock, and silently discarding it is hard to defend.
  #
  # Both principal clauses are load-bearing. "^system:" alone leaves the GKE
  # service agent behind: in the same window container-engine-robot accounted
  # for 287 lease writes, which would have inflated the surviving stream by 65%.
  # Matching any *.iam.gserviceaccount.com covers it and every future service
  # agent without another edit here.
  #
  # Kept to a single line on purpose: Cloud Logging treats a newline as an
  # implicit AND, which would break the OR grouping if this were wrapped.
  machine_lease_exclusion = <<-EOT
    NOT (protoPayload.methodName=~"coordination\.v1\.leases" AND (protoPayload.authenticationInfo.principalEmail=~"^system:" OR protoPayload.authenticationInfo.principalEmail=~"\.iam\.gserviceaccount\.com$"))
  EOT

  lease_filter = var.exclude_machine_lease_heartbeats ? trimspace(local.machine_lease_exclusion) : ""

  # An empty cluster_names means every cluster in the project: one sink for the
  # fleet, with the detector routing on resource.labels.cluster_name the way the
  # event watcher already routes on its own per-cluster identity.
  cluster_list   = join(" OR ", [for name in var.cluster_names : "\"${name}\""])
  cluster_filter = length(var.cluster_names) > 0 ? "resource.labels.cluster_name=(${local.cluster_list})" : ""

  sink_filter = join("\n", compact([
    trimspace(local.base_filter),
    local.lease_filter,
    local.cluster_filter,
  ]))
}

resource "google_pubsub_topic" "drift_audit" {
  #checkov:skip=CKV_GCP_83:Drift audit topic uses default Google-managed encryption keys
  project = var.project_id
  name    = var.topic_name
}

resource "google_pubsub_subscription" "drift_audit" {
  project = var.project_id
  name    = var.subscription_name
  topic   = google_pubsub_topic.drift_audit.id

  ack_deadline_seconds       = var.ack_deadline_seconds
  message_retention_duration = var.message_retention_duration

  # Pub/Sub deletes a subscription after 31 days without pull activity. That is
  # harmless while the detector runs and quietly destructive when it does not:
  # a paused rollout, or a topic provisioned ahead of the consumer that reads
  # from it, should not take the subscription with it.
  expiration_policy {
    ttl = ""
  }

  # The detector nacks what it cannot parse, so a payload-shape change from GCP
  # is loud rather than silently acked away. Backoff keeps that from becoming a
  # hot redelivery loop.
  #
  # There is deliberately no dead_letter_policy. Without message ordering a pull
  # subscription has no head-of-line blocking, so an unparseable message cannot
  # stall the pipeline; it redelivers on its own backoff until retention expires
  # while everything else flows past. A dead-letter topic would make that message
  # inspectable, at the cost of two further IAM grants (the Pub/Sub service agent
  # needs publisher on the dead-letter topic and subscriber here) that render the
  # policy silently inert when missed. Revisit if the detector's parse-failure
  # counter ever moves.
  retry_policy {
    minimum_backoff = var.retry_minimum_backoff
    maximum_backoff = var.retry_maximum_backoff
  }
}

resource "google_logging_project_sink" "drift_audit" {
  project     = var.project_id
  name        = var.sink_name
  destination = "pubsub.googleapis.com/${google_pubsub_topic.drift_audit.id}"
  filter      = local.sink_filter

  # Without this the sink publishes as cloud-logs@system.gserviceaccount.com,
  # an identity shared across every Google Cloud customer. With it the sink
  # publishes as this project's own logging service agent,
  # service-<project-number>@gcp-sa-logging.iam.gserviceaccount.com, so the
  # grant below admits only sinks belonging to this project.
  #
  # "Unique" means unique per project, not per sink: every sink here with this
  # flag set shares the identity, so the grant is not narrower than the project.
  unique_writer_identity = true
}

# IMPORTANT: without this grant the sink is silently inert. Log Router surfaces
# no error, the topic receives nothing, and the only trace is an export-error
# metric nobody is watching. It is the most likely reason a freshly applied
# drift pipeline delivers zero messages, and it looks identical to "no drift
# happened" from the consumer's side.
#
# writer_identity already carries the "serviceAccount:" prefix.
#
# `.id`, never `.name`. A replaced topic or subscription loses its whole IAM
# policy in GCP, and a binding keyed on the plan-time-known `.name` is left out
# of the plan that replaced it — green apply, empty policy. chat-pubsub's
# main.tf carries the full account of that failure.
resource "google_pubsub_topic_iam_member" "sink_writer" {
  project = var.project_id
  topic   = google_pubsub_topic.drift_audit.id
  role    = "roles/pubsub.publisher"
  member  = google_logging_project_sink.drift_audit.writer_identity
}

resource "google_pubsub_subscription_iam_member" "detector_subscriber" {
  project      = var.project_id
  subscription = google_pubsub_subscription.drift_audit.id
  role         = "roles/pubsub.subscriber"
  member       = "serviceAccount:${var.detector_service_account_email}"
}

# roles/pubsub.subscriber covers consuming messages but not reading the
# subscription's own metadata. It grants subscriptions.consume, snapshots.seek,
# and topics.attachSubscription -- notably not subscriptions.get. A client that
# confirms the subscription exists before pulling (the Go client's
# Subscription.Exists, and the chat adapter's _check_subscription_exists) needs
# viewer as well, and without it fails with a PermissionDenied that reads
# nothing like a missing grant.
resource "google_pubsub_subscription_iam_member" "detector_viewer" {
  project      = var.project_id
  subscription = google_pubsub_subscription.drift_audit.id
  role         = "roles/pubsub.viewer"
  member       = "serviceAccount:${var.detector_service_account_email}"
}
