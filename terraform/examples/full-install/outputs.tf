output "cluster_name" {
  description = "Name of the provisioned GKE Autopilot cluster"
  value       = module.gke_cluster.cluster_name
}

# The image tag the last apply recorded, which is NOT the tag the cluster is
# serving: the redeploy workflows move the running tag with `helm upgrade` and
# never touch Terraform. A drift plan needs this one. Planning at the RUNNING
# tag instead makes every out-of-band redeploy show up as a pending change to
# helm_release.kube_agents, so the daily report opens on image lag and the
# infra-drift issue never reaches the clean plan that would close it.
#
# An output rather than reading the helm_release's values out of state, because
# outputs are what Terraform persists for exactly this purpose and `terraform
# output -raw` is a stable interface where digging through `show -json` is not.
output "image_tag" {
  description = "Image tag recorded by the last apply of this composition"
  value       = var.image_tag
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

output "stockout_pubsub_topic" {
  description = "Pub/Sub topic for GKE stockout alerts (null when enable_stockout_investigator is false)"
  value       = try(google_pubsub_topic.stockout_alerts[0].name, null)
}

output "stockout_pubsub_subscription" {
  description = "Pub/Sub subscription for GKE stockout alerts (null when enable_stockout_investigator is false)"
  value       = try(google_pubsub_subscription.stockout_alerts[0].name, null)
}

output "stockout_pubsub_sink" {
  description = "Cloud Logging sink for GKE stockout alerts (null when enable_stockout_investigator is false)"
  value       = try(google_logging_project_sink.stockout_alerts[0].name, null)
}

output "drift_pubsub_topic" {
  description = "Pub/Sub topic the drift audit-log sink publishes to (null when enable_drift_pubsub is false)"
  value       = try(module.drift_pubsub[0].topic_name, null)
}

output "drift_pubsub_subscription" {
  description = "Pub/Sub pull subscription the drift detector reads from (null when enable_drift_pubsub is false)"
  value       = try(module.drift_pubsub[0].subscription_name, null)
}

output "drift_pubsub_subscription_id" {
  description = "Fully-qualified drift subscription path, projects/<project>/subscriptions/<name>, the value the drift detector's --subscription flag takes (null when enable_drift_pubsub is false)"
  value       = try(module.drift_pubsub[0].subscription_id, null)
}

output "drift_pubsub_source_projects" {
  description = "The projects beyond project_id whose audit logs the drift topic also receives, one sink each named drift_pubsub_sink with project_id appended: the projects the declaration lists at plan time (the IAM module's scope_export_projects: scope.projects and the selectors' members, never a container's Asset-Inventory members) less drift_pubsub_source_exclude_projects. Empty with no declared scope; null when enable_drift_pubsub is false."
  value       = try(module.drift_pubsub[0].source_projects, null)
}

output "scoped_service_accounts" {
  description = "Map from project id to the pool member's service account for it, one per project the plan listed in scope while scoped_pool_enabled is true, and empty otherwise. The key is what the credential broker matches on, so the two are directly comparable. The accounts hold no IAM grant as of 2026-08-12; see scoped_pool.tf."
  value       = module.kube_agents_iam.scoped_service_accounts
}

output "scope_projects" {
  description = "The projects beyond project_id that scope.projects named and the IAM module bound scope_roles in. The host project is omitted even when named; the CR's projects list carries it as written."
  value       = module.kube_agents_iam.scope_projects
}

output "scope_roles" {
  description = "The roles every scope project carries: the IAM module's read allowlist intersected with the roles the host project got."
  value       = module.kube_agents_iam.scope_roles
}

output "scope_folders" {
  description = "The folders scope.folders named and the IAM module bound scope_container_roles on."
  value       = module.kube_agents_iam.scope_folders
}

output "scope_organizations" {
  description = "The organisations scope.organizations named and the IAM module bound scope_container_roles on."
  value       = module.kube_agents_iam.scope_organizations
}

output "scope_container_roles" {
  description = "The roles every folder and organisation in scope carries: scope_roles plus roles/cloudasset.viewer for the reconcile's container search."
  value       = module.kube_agents_iam.scope_container_roles
}

output "scope_shared_vpc_hosts" {
  description = "The Shared VPC host projects scope.shared_vpc_hosts named; each was resolved at plan time to its service projects (scope_selector_members) and, unless otherwise in scope or project_id itself, is bound with roles/compute.viewer alone for the reconcile's lookup (scope_lookup_only_hosts)."
  value       = module.kube_agents_iam.scope_shared_vpc_hosts
}

output "scope_metrics_scopes" {
  description = "The Metrics Scope scoping projects scope.metrics_scopes named; each was resolved at plan time to the projects it monitors (scope_selector_members) and, unless it is project_id, is bound with scope_roles itself for the reconcile's lookup."
  value       = module.kube_agents_iam.scope_metrics_scopes
}

output "scope_selector_members" {
  description = "What each Shared VPC host and Metrics Scope resolved to at plan time, by project ID, under the selector's snapshot name (sharedVpcHosts/<host>, metricsScopes/<scope>)."
  value       = module.scope_resolver.members
}

output "scope_bound_projects" {
  description = "Every project beyond project_id the IAM module bound scope_roles in: the explicit projects, the selectors' members less a Shared VPC service project an exclude entry names by ID (a monitored project excluded by number is never resolved, and one excluded by ID keeps its grant), and each Metrics Scope scoping project."
  value       = module.kube_agents_iam.scope_bound_projects
}

output "scope_lookup_only_hosts" {
  description = "The Shared VPC hosts bound with roles/compute.viewer alone, for the reconcile's lookup of their service projects: every declared host that is neither project_id nor otherwise in scope."
  value       = module.kube_agents_iam.scope_lookup_only_hosts
}
