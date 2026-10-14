output "topic_name" {
  description = "Short name of the drift-audit Pub/Sub topic"
  value       = google_pubsub_topic.drift_audit.name
}

output "subscription_name" {
  description = "Short name of the drift-audit Pub/Sub subscription"
  value       = google_pubsub_subscription.drift_audit.name
}

output "subscription_id" {
  description = "Fully-qualified subscription path (projects/<project>/subscriptions/<name>). The drift detector's --subscription flag takes this or the bare subscription_name."
  value       = google_pubsub_subscription.drift_audit.id
}

output "sink_writer_identity" {
  description = "Service account the Log Router sink publishes as. Exported for debugging: an empty topic almost always means this identity lost its roles/pubsub.publisher grant."
  value       = google_logging_project_sink.drift_audit.writer_identity
}

output "sink_filter" {
  description = "The Cloud Logging filter the sink exports on. Exported so a caller can diff it against what the detector expects to receive."
  value       = google_logging_project_sink.drift_audit.filter
}

output "source_projects" {
  description = "The projects beyond project_id whose audit logs this topic also receives, one sink each: source_projects as given, less project_id."
  value       = sort(tolist(local.source_projects))
}

output "source_sink_name" {
  description = "The name every source sink carries in its project: sink_name with project_id appended, so two installs listing one project, or an install listing another install's management project, do not collide on it."
  value       = local.source_sink_name
}

output "source_sink_writer_identities" {
  description = "Service account each source project's sink publishes as, by project ID. Exported for debugging, as sink_writer_identity is: a source project whose records never arrive almost always means this identity lost its roles/pubsub.publisher grant on the topic."
  value       = { for project, sink in google_logging_project_sink.source_drift_audit : project => sink.writer_identity }
}
