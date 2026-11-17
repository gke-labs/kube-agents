# Which projects the scope binds, and with what: the allowlist intersected
# with project_roles in each project a declaration reaches, the scoping
# project and a lookup-only host included, and nothing in the management
# project, which carries project_roles already. The role set itself is read
# from local.scope_roles rather than spelled out, so a role added to the
# allowlist and the default list together changes nothing here; the
# repository's tests/test_scope_iam.py pins the allowlist's contents. The
# provider is mocked, so a plan here binds nothing anywhere.

mock_provider "google" {}

variables {
  project_id = "mgmt-project-1"
}

# What the module binds is its own scope_roles; that the set is the
# intersection is the next run's, with a custom list where the two differ.
run "an_explicit_project_carries_scope_roles_and_the_management_project_none" {
  command = plan

  variables {
    scope = { projects = ["team-alpha", "mgmt-project-1"] }
  }

  assert {
    condition     = length(local.scope_roles) > 0 && toset(keys(google_project_iam_member.scope_roles)) == toset([for role in local.scope_roles : "team-alpha/${role}"])
    error_message = "every allowlist role in team-alpha and nothing in the management project: ${jsonencode(keys(google_project_iam_member.scope_roles))}"
  }
}

# A custom set carries only the allowlist entries it holds into the scope;
# what it holds beyond the allowlist stays at home.
run "a_custom_role_set_is_intersected_with_the_allowlist" {
  command = plan

  variables {
    project_roles = ["roles/container.clusterViewer", "roles/container.admin", "roles/logging.viewer"]
    scope         = { projects = ["team-alpha"] }
  }

  assert {
    condition = toset(keys(google_project_iam_member.scope_roles)) == toset([
      "team-alpha/roles/container.clusterViewer",
      "team-alpha/roles/logging.viewer",
    ])
    error_message = "bindings: ${jsonencode(keys(google_project_iam_member.scope_roles))}"
  }
  assert {
    condition     = contains(keys(google_project_iam_member.agent_roles), "roles/container.admin")
    error_message = "roles/container.admin is still granted in the management project: ${jsonencode(keys(google_project_iam_member.agent_roles))}"
  }
}

# A Metrics Scope's members, the scoping project among them, are bound with
# the allowlist; an exclude entry that names either by ID withholds nothing,
# because the reconcile names a monitored project, and reads the scoping
# project, with that grant.
run "a_metrics_scope_binds_its_members_and_its_scoping_project_however_excluded" {
  command = plan

  variables {
    scope = {
      metrics_scopes = ["scoping-proj1"]
      exclude        = { projects = ["scoping-proj1", "monitored-proj1"] }
    }
    scope_selector_members = {
      "metricsScopes/scoping-proj1" = ["scoping-proj1", "monitored-proj1"]
    }
  }

  assert {
    condition     = toset([for binding in values(google_project_iam_member.scope_roles) : binding.project]) == toset(["scoping-proj1", "monitored-proj1"])
    error_message = "bound projects: ${jsonencode(distinct([for binding in values(google_project_iam_member.scope_roles) : binding.project]))}"
  }
  assert {
    condition     = length(google_project_iam_member.scope_roles) == 2 * length(local.scope_roles)
    error_message = "the allowlist in each of two projects, ${length(google_project_iam_member.scope_roles)} bindings planned"
  }
}

# The scoping project is bound even when the resolver reported it as the only
# member, and even when no member is reported for it at all.
run "a_scoping_project_is_bound_on_its_own" {
  command = plan

  variables {
    scope                  = { metrics_scopes = ["scoping-proj1"] }
    scope_selector_members = { "metricsScopes/scoping-proj1" = [] }
  }

  assert {
    condition     = toset([for binding in values(google_project_iam_member.scope_roles) : binding.project]) == toset(["scoping-proj1"])
    error_message = "bound projects: ${jsonencode(distinct([for binding in values(google_project_iam_member.scope_roles) : binding.project]))}"
  }
}

# A Shared VPC host's service projects are bound with the allowlist, less one
# an exclude entry names by ID; the host itself, not otherwise in scope, gets
# roles/compute.viewer alone, for the lookup, whether or not it is excluded.
run "a_shared_vpc_host_binds_its_service_projects_and_itself_for_the_lookup" {
  command = plan

  variables {
    scope = {
      shared_vpc_hosts = ["host-proj-1"]
      exclude          = { projects = ["svc-proj-1", "host-proj-1"] }
    }
    scope_selector_members = {
      "sharedVpcHosts/host-proj-1" = ["svc-proj-1", "svc-proj-2"]
    }
  }

  assert {
    condition     = toset(keys(google_project_iam_member.scope_roles)) == setunion(toset([for role in local.scope_roles : "svc-proj-2/${role}"]), toset(["host-proj-1/roles/compute.viewer"]))
    error_message = "bindings: ${jsonencode(keys(google_project_iam_member.scope_roles))}"
  }
}

# A host that is also an explicit project carries the allowlist once, with no
# lookup-only binding beside it.
run "a_host_in_scope_projects_carries_the_allowlist_once" {
  command = plan

  variables {
    scope = {
      projects         = ["host-proj-1"]
      shared_vpc_hosts = ["host-proj-1"]
    }
    scope_selector_members = { "sharedVpcHosts/host-proj-1" = [] }
  }

  assert {
    condition     = length(google_project_iam_member.scope_roles) == length(local.scope_roles) && alltrue([for binding in values(google_project_iam_member.scope_roles) : binding.project == "host-proj-1"])
    error_message = "bindings: ${jsonencode(keys(google_project_iam_member.scope_roles))}"
  }
  assert {
    condition     = length(local.scope_lookup_only_hosts) == 0
    error_message = "a host bound with the allowlist is not a lookup-only host as well: ${jsonencode(local.scope_lookup_only_hosts)}"
  }
}

# The management project as a host or a scoping project binds nothing there:
# it carries project_roles already. Its members are bound as usual.
run "the_management_project_as_a_selector_binds_only_the_members" {
  command = plan

  variables {
    scope = {
      shared_vpc_hosts = ["mgmt-project-1"]
      metrics_scopes   = ["mgmt-project-1"]
    }
    scope_selector_members = {
      "sharedVpcHosts/mgmt-project-1" = ["svc-proj-1"]
      "metricsScopes/mgmt-project-1"  = ["mgmt-project-1", "monitored-proj1"]
    }
  }

  assert {
    condition     = toset([for binding in values(google_project_iam_member.scope_roles) : binding.project]) == toset(["svc-proj-1", "monitored-proj1"])
    error_message = "bound projects: ${jsonencode(distinct([for binding in values(google_project_iam_member.scope_roles) : binding.project]))}"
  }
}

# Only the declared selectors' entries are read: a key for a selector the
# scope no longer declares binds nothing.
run "a_stale_members_key_binds_nothing" {
  command = plan

  variables {
    scope                  = {}
    scope_selector_members = { "sharedVpcHosts/old-host-1" = ["svc-proj-9"] }
  }

  assert {
    condition     = length(google_project_iam_member.scope_roles) == 0
    error_message = "bindings: ${jsonencode(keys(google_project_iam_member.scope_roles))}"
  }
}

# A project both explicit and a selector member is one binding per role.
run "a_project_named_twice_is_bound_once" {
  command = plan

  variables {
    scope = {
      projects       = ["monitored-proj1"]
      metrics_scopes = ["scoping-proj1"]
    }
    scope_selector_members = { "metricsScopes/scoping-proj1" = ["scoping-proj1", "monitored-proj1"] }
  }

  assert {
    condition     = length(google_project_iam_member.scope_roles) == 2 * length(local.scope_roles)
    error_message = "${length(google_project_iam_member.scope_roles)} bindings planned for two projects"
  }
}

# A container carries the allowlist plus roles/cloudasset.viewer, on the
# container itself.
run "a_folder_carries_the_allowlist_plus_cloudasset_viewer" {
  command = plan

  variables {
    scope = { folders = ["123456789012"], organizations = ["987654321098"] }
  }

  assert {
    condition     = length(google_folder_iam_member.scope_roles) == length(local.scope_roles) + 1 && contains(keys(google_folder_iam_member.scope_roles), "123456789012/roles/cloudasset.viewer")
    error_message = "folder bindings: ${jsonencode(keys(google_folder_iam_member.scope_roles))}"
  }
  assert {
    condition     = length(google_organization_iam_member.scope_roles) == length(local.scope_roles) + 1 && contains(keys(google_organization_iam_member.scope_roles), "987654321098/roles/cloudasset.viewer")
    error_message = "organization bindings: ${jsonencode(keys(google_organization_iam_member.scope_roles))}"
  }
  assert {
    condition     = length(google_project_iam_member.scope_roles) == 0
    error_message = "a container binds no project one by one"
  }
}
