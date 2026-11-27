output "topic_name" {
  description = "Name of the chat-events Pub/Sub topic (for the PlatformAgent CR's googleChat integration)"
  value       = var.manage ? google_pubsub_topic.chat_events[0].name : var.topic_name
}

output "subscription_name" {
  description = "Name of the chat-events Pub/Sub subscription (for the PlatformAgent CR's googleChat integration)"
  value       = var.manage ? google_pubsub_subscription.chat_events[0].name : var.subscription_name
}

output "chat_service_account_email" {
  description = "Email of the dedicated Google Chat service account when chat_service_account_id is set, or empty string in same-project mode"
  value       = local.chat_service_account_email
}
