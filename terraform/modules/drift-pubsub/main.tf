# Cloud Logging -> Pub/Sub delivery path for the drift detector.
#
# GKE audit logs cannot be read from the Kubernetes API. The control plane is
# managed, so the API server's audit backend is not ours to configure, and the
# stream surfaces only in Cloud Logging. This module builds the route out of
# Cloud Logging and into a subscription the detector pulls from.
#
# The design and the Phase 0 spike that produced the sink filter live in
# agents/platform/docs/drift-detection/.

locals {
  # Mutating calls against GKE clusters, from the Admin Activity audit log.
  # Cloud Logging ANDs newline-separated expressions.
  #
  # Principals are deliberately NOT filtered here. The detector classifies them
  # itself and needs the unfiltered volume visible to measure its own noise
  # profile (the spike measured ~78% system controllers, ~20% CI service
  # accounts, ~1% human). Filtering in the sink would discard the denominators
  # and make a mistuned automation allowlist impossible to debug.
  base_filter = <<-EOT
    logName="projects/${var.project_id}/logs/cloudaudit.googleapis.com%2Factivity"
    resource.type="k8s_cluster"
    protoPayload.methodName=~"create|patch|update|delete"
  EOT

  # An empty cluster_names means every cluster in the project: one sink for the
  # fleet, with the detector routing on resource.labels.cluster_name the way the
  # event watcher already routes on its own per-cluster identity.
  cluster_list   = join(" OR ", [for name in var.cluster_names : "\"${name}\""])
  cluster_filter = length(var.cluster_names) > 0 ? "resource.labels.cluster_name=(${local.cluster_list})" : ""

  sink_filter = join("\n", compact([trimspace(local.base_filter), local.cluster_filter]))
}

resource "google_pubsub_topic" "drift_audit" {
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

  # A dedicated writer identity rather than the shared project-wide one, so the
  # publisher grant below scopes to this sink alone.
  unique_writer_identity = true
}

# IMPORTANT: without this grant the sink is silently inert. Log Router surfaces
# no error, the topic receives nothing, and the only trace is an export-error
# metric nobody is watching. It is the most likely reason a freshly applied
# drift pipeline delivers zero messages, and it looks identical to "no drift
# happened" from the consumer's side.
#
# writer_identity already carries the "serviceAccount:" prefix.
resource "google_pubsub_topic_iam_member" "sink_writer" {
  project = var.project_id
  topic   = google_pubsub_topic.drift_audit.name
  role    = "roles/pubsub.publisher"
  member  = google_logging_project_sink.drift_audit.writer_identity
}

resource "google_pubsub_subscription_iam_member" "detector_subscriber" {
  project      = var.project_id
  subscription = google_pubsub_subscription.drift_audit.name
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
  subscription = google_pubsub_subscription.drift_audit.name
  role         = "roles/pubsub.viewer"
  member       = "serviceAccount:${var.detector_service_account_email}"
}
