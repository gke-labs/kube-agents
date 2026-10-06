output "service_account_email" {
  description = "Email of the created IAM service account"
  value       = google_service_account.agent.email
}

output "agent_project_roles" {
  description = <<-EOT
    Project-level roles actually granted to the agent's own service account.
    Surfaced because the residual ceiling is a security property worth being
    able to assert on rather than infer from which variables were set. It does
    not yet vary with scoped_pool_enabled -- see the suspended coupling in
    main.tf.
  EOT
  value       = local.agent_project_roles
}

output "scoped_service_accounts" {
  description = <<-EOT
    Map from project id to the email of the pool member for it: one entry per
    project the plan listed in the scope while scoped_pool_enabled is true,
    the host project included, and empty otherwise. The key is the same
    string the credential broker looks up, so this output is directly
    comparable with the broker's mapping. The accounts hold no IAM grant; see
    scoped_pool.tf.
  EOT
  value       = { for key in keys(local.scoped_pool) : key => google_service_account.scoped[key].email }
}

output "scope_projects" {
  description = <<-EOT
    The projects beyond project_id that scope.projects named, each bound with
    scope_roles. The host project is omitted even when scope.projects names it,
    because it carries project_roles already.
  EOT
  value       = sort(tolist(local.scope_projects))
}

output "scope_roles" {
  description = <<-EOT
    The roles bound in every scope project: the module's read allowlist
    (local.scope_role_allowlist in scope.tf) intersected with project_roles.
    Surfaced so the ceiling a scoped project grants can be asserted on rather
    than inferred from the allowlist and the role list separately.
  EOT
  value       = local.scope_roles
}

output "scope_folders" {
  description = <<-EOT
    The folders scope.folders named, each bound on the folder itself with
    scope_container_roles, so every project beneath inherits the grant.
  EOT
  value       = sort(tolist(local.scope_folders))
}

output "scope_organizations" {
  description = <<-EOT
    The organisations scope.organizations named, each bound on the
    organisation itself with scope_container_roles.
  EOT
  value       = sort(tolist(local.scope_organizations))
}

output "scope_container_roles" {
  description = <<-EOT
    The roles bound on every folder and organisation in scope: scope_roles
    plus roles/cloudasset.viewer, which the reconcile's container search needs
    on the container it searches.
  EOT
  value       = local.scope_container_roles
}

output "scope_shared_vpc_hosts" {
  description = <<-EOT
    The Shared VPC host projects scope.shared_vpc_hosts named. One that is
    not otherwise in scope, and is not project_id, is bound with
    roles/compute.viewer alone, so the reconcile's lookup can read it
    (scope_lookup_only_hosts); one that is in scope carries scope_roles like
    any other member.
  EOT
  value       = sort(tolist(local.scope_shared_vpc_hosts))
}

output "scope_metrics_scopes" {
  description = <<-EOT
    The Metrics Scope scoping projects scope.metrics_scopes named. Each
    other than project_id is bound with scope_roles itself, so the
    reconcile's lookup can read the scope there, beside the monitored
    projects scope_selector_members resolved it to.
  EOT
  value       = sort(tolist(local.scope_metrics_scopes))
}

output "scope_bound_projects" {
  description = <<-EOT
    Every project beyond project_id bound with scope_roles: the explicit
    scope.projects, the selectors' members (less a Shared VPC service project
    an exclude entry names by ID; a monitored project excluded by number never
    reached the input, and one excluded by ID keeps its grant), and each
    Metrics Scope scoping project, once each. A Shared VPC host that is not
    among them is in scope_lookup_only_hosts instead.
  EOT
  value       = sort(tolist(local.scope_bound_projects))
}

output "scope_lookup_only_hosts" {
  description = <<-EOT
    The Shared VPC hosts bound with roles/compute.viewer alone, for the
    reconcile's lookup of their service projects: every host in
    scope.shared_vpc_hosts that is neither project_id nor in
    scope_bound_projects.
  EOT
  value       = sort(tolist(local.scope_lookup_only_hosts))
}
