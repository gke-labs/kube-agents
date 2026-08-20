output "topic_name" {
  description = "Short name of the drift-audit Pub/Sub topic"
  value       = google_pubsub_topic.drift_audit.name
}

output "subscription_name" {
  description = "Short name of the drift-audit Pub/Sub subscription"
  value       = google_pubsub_subscription.drift_audit.name
}

output "subscription_id" {
  description = "Fully-qualified subscription path (projects/<project>/subscriptions/<name>) — the value the drift detector's --subscription flag takes"
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
