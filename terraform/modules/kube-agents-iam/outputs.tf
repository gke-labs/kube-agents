output "service_account_email" {
  description = "Email of the created IAM service account"
  value       = google_service_account.agent.email
}

output "agent_project_roles" {
  description = <<-EOT
    Project-level roles actually granted to the agent's own service account,
    after the scoped_clusters coupling is applied. Surfaced because the residual
    ceiling is a security property worth being able to assert on rather than
    infer from which variables were set.
  EOT
  value       = local.agent_project_roles
}

output "scoped_service_accounts" {
  description = <<-EOT
    Map from GKE resource name to the email of the service account scoped to it.
    The key is the same string the IAM Condition is written against and the same
    string the credential broker looks up, so this output is directly comparable
    with both.
  EOT
  value       = { for key in keys(local.scoped_pool) : key => google_service_account.scoped[key].email }
}

output "scoped_sa_pool_json" {
  description = <<-EOT
    The mapping document the credential broker consumes, ready to be rendered
    into the PlatformAgent CR or written to the ConfigMap the broker mounts at
    CREDENTIAL_PROXY_SCOPED_SA_POOL_FILE.

    Emitted as the finished document rather than as parts a caller reassembles,
    because the point of the file is that Terraform and the broker agree on it
    exactly. Sorted by scope key so re-running the plan does not reorder it.
  EOT
  value = jsonencode({
    version = 1
    serviceAccounts = [
      for key in sort(keys(local.scoped_pool)) : {
        projectId           = local.scoped_pool[key].project_id
        location            = local.scoped_pool[key].location
        clusterName         = local.scoped_pool[key].cluster_name
        serviceAccountEmail = google_service_account.scoped[key].email
      }
    ]
  })
}
