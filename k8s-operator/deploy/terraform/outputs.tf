output "gke_cluster_name" {
  value       = google_container_cluster.primary.name
  description = "The name of the provisioned GKE cluster."
}

output "gke_cluster_endpoint" {
  value       = google_container_cluster.primary.endpoint
  description = "The API endpoint for GKE."
}

output "gchat_pubsub_topic" {
  value       = google_pubsub_topic.chat_events.id
  description = "The Google Chat Pub/Sub topic ID."
}

output "gchat_pubsub_subscription" {
  value       = google_pubsub_subscription.chat_events_sub.id
  description = "The Google Chat Pub/Sub subscription ID."
}

output "controller_gsa_email" {
  value       = google_service_account.controller.email
  description = "The email of the Controller GSA."
}

output "platform_agent_gsa_email" {
  value       = google_service_account.platform_agent.email
  description = "The email of the Platform Agent GSA."
}

output "operator_agent_gsa_email" {
  value       = google_service_account.operator_agent.email
  description = "The email of the Operator Agent GSA."
}

output "devteam_agent_gsa_email" {
  value       = google_service_account.devteam_agent.email
  description = "The email of the DevTeam Agent GSA."
}

output "gke_cluster_ca_certificate" {
  value       = google_container_cluster.primary.master_auth[0].cluster_ca_certificate
  description = "The CA certificate of the GKE cluster."
}
