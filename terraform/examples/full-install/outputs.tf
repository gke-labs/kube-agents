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

output "agent_project_roles" {
  description = "Project-level IAM roles actually granted to the agent's service account"
  value       = local.agent_project_roles

  # permission_set = "custom" with no project_roles would otherwise fall
  # through to the read-only bundle — quietly granting something other than
  # what was asked for.
  precondition {
    condition     = !(var.permission_set == "custom" && var.project_roles == null)
    error_message = "permission_set = \"custom\" requires project_roles to be set explicitly (use [] to grant nothing)."
  }
}

output "backup_plan_name" {
  description = "Name of the scheduled BackupPlan (null when enable_gke_backup_plan is false)"
  value       = try(module.gke_backup_plan[0].backup_plan_name, null)

  # Both operands are input variables, so this is decided at plan time — before
  # the cluster exists. Without it the mismatch surfaces as a raw
  # FAILED_PRECONDITION from the Backup for GKE API partway through an apply
  # that has already built everything ahead of the plan.
  precondition {
    condition     = !var.enable_gke_backup_plan || var.enable_backup_agent
    error_message = "enable_gke_backup_plan = true requires enable_backup_agent = true: a BackupPlan cannot target a cluster whose Backup for GKE agent is off."
  }
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
