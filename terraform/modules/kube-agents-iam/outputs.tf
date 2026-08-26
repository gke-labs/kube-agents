output "service_account_email" {
  description = "Email of the created IAM service account"
  value       = google_service_account.agent.email
}

output "agent_project_roles" {
  description = <<-EOT
    Project-level roles actually granted to the agent's own service account.
    Surfaced because the residual ceiling is a security property worth being
    able to assert on rather than infer from which variables were set. It does
    not yet vary with scoped_clusters -- see the suspended coupling in main.tf.
  EOT
  value       = local.agent_project_roles
}

output "scoped_service_accounts" {
  description = <<-EOT
    Map from GKE resource name to the email of the service account for it. The
    key is the same string the credential broker looks up, so this output is
    directly comparable with the broker's mapping. The accounts hold no IAM
    grant; see scoped_pool.tf.
  EOT
  value       = { for key in keys(local.scoped_pool) : key => google_service_account.scoped[key].email }
}

