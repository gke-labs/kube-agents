output "cluster_name" {
  description = "Name of the provisioned GKE Autopilot cluster"
  value       = module.gke_cluster.cluster_name
}

output "cluster_location" {
  description = "Region the cluster runs in"
  value       = module.gke_cluster.cluster_location
}

output "agent_service_account_email" {
  description = "Email of the Platform Agent's Google Service Account"
  value       = module.kube_agents_iam.service_account_email
}

output "chat_topic_name" {
  description = "Pub/Sub topic for Google Chat events (null when Chat is disabled); already wired into the PlatformAgent CR's googleChat section"
  value       = try(module.chat_pubsub[0].topic_name, null)
}

output "chat_subscription_name" {
  description = "Pub/Sub subscription for Google Chat events (null when Chat is disabled); already wired into the PlatformAgent CR's googleChat section"
  value       = try(module.chat_pubsub[0].subscription_name, null)
}

output "github_minter_service_account_email" {
  description = "Email of the GitHub token minter's service account (null when the minter is disabled)"
  value       = try(module.github_minter[0].service_account_email, null)
}

output "github_minter_kms_keyring" {
  description = "KMS key ring holding the GitHub App signing key (null when the minter is disabled)"
  value       = try(module.github_minter[0].kms_keyring, null)
}

output "github_minter_kms_key" {
  description = "KMS signing key to import the GitHub App PEM into (null when the minter is disabled)"
  value       = try(module.github_minter[0].kms_key, null)
}
