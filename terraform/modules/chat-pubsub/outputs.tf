output "topic_name" {
  description = "Name of the chat-events Pub/Sub topic (for the PlatformAgent CR's googleChat integration)"
  value       = google_pubsub_topic.chat_events.name
}

output "subscription_name" {
  description = "Name of the chat-events Pub/Sub subscription (for the PlatformAgent CR's googleChat integration)"
  value       = google_pubsub_subscription.chat_events.name
}
