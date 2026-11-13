# The declarations the plan refuses, each a precondition on the agent's
# service account (main.tf): a role set that cannot manage clusters beside a
# scope, a Shared VPC host under a role set without roles/compute.viewer, and
# a selector the resolver did not resolve.

mock_provider "google" {}

variables {
  project_id = "mgmt-project-1"
}

run "a_role_set_that_cannot_manage_clusters_is_refused_beside_a_scope" {
  command = plan

  variables {
    project_roles = ["roles/compute.viewer", "roles/iam.securityReviewer"]
    scope         = { projects = ["team-alpha"] }
  }

  expect_failures = [google_service_account.agent]
}

run "the_same_role_set_plans_while_the_scope_is_empty" {
  command = plan

  variables {
    project_roles = ["roles/compute.viewer", "roles/iam.securityReviewer"]
  }

  assert {
    condition     = length(google_project_iam_member.scope_roles) == 0
    error_message = "an empty scope binds nothing and is not refused"
  }
}

run "a_container_under_a_role_set_that_cannot_manage_clusters_is_refused" {
  command = plan

  variables {
    project_roles = []
    scope         = { folders = ["123456789012"] }
  }

  expect_failures = [google_service_account.agent]
}

run "a_host_under_a_role_set_without_compute_viewer_is_refused" {
  command = plan

  variables {
    project_roles          = ["roles/container.viewer"]
    scope                  = { shared_vpc_hosts = ["host-proj-1"] }
    scope_selector_members = { "sharedVpcHosts/host-proj-1" = [] }
  }

  expect_failures = [google_service_account.agent]
}

# No carve-out for the management project as host: a custom set that carries
# compute.projects.get through a custom role and one that carries it through
# nothing look the same from the plan.
run "the_management_project_as_host_is_refused_the_same_way" {
  command = plan

  variables {
    project_roles          = ["roles/container.viewer"]
    scope                  = { shared_vpc_hosts = ["mgmt-project-1"] }
    scope_selector_members = { "sharedVpcHosts/mgmt-project-1" = [] }
  }

  expect_failures = [google_service_account.agent]
}

run "a_metrics_scope_needs_no_compute_viewer" {
  command = plan

  variables {
    project_roles          = ["roles/container.viewer"]
    scope                  = { metrics_scopes = ["scoping-proj1"] }
    scope_selector_members = { "metricsScopes/scoping-proj1" = ["scoping-proj1"] }
  }

  assert {
    condition     = keys(google_project_iam_member.scope_roles) == ["scoping-proj1/roles/container.viewer"]
    error_message = "bindings: ${jsonencode(keys(google_project_iam_member.scope_roles))}"
  }
}

run "a_declared_selector_with_no_members_entry_is_refused" {
  command = plan

  variables {
    scope                  = { shared_vpc_hosts = ["host-proj-1"], metrics_scopes = ["scoping-proj1"] }
    scope_selector_members = { "sharedVpcHosts/host-proj-1" = [] }
  }

  expect_failures = [google_service_account.agent]
}
