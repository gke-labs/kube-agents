# The scoped service account pool: one account per project the plan can list
# in the scope (scope.tf, local.scope_listed_projects), keyed on the bare
# project id, and only while scoped_pool_enabled arms it. These cases pin the
# key, the account id a project produces, the declared bound on the pool
# (scoped_pool_max_accounts, which the plan holds the derived set to; it is
# not a reading of the host project's quota), that a selector's members are
# pool members like an explicit project, and that a container's listed members
# are pool members and nothing else. The provider is mocked, so a plan here
# creates nothing.

mock_provider "google" {}

variables {
  project_id = "mgmt-project-1"
}

# Disarmed, the default: a declared scope provisions no account, so declaring
# `projects` on its own arms nothing (design §6).
run "a_disarmed_pool_provisions_nothing_beside_a_scope" {
  command = plan

  variables {
    scope = { projects = ["team-alpha", "team-beta"] }
  }

  assert {
    condition     = length(google_service_account.scoped) == 0 && length(google_service_account_iam_member.scoped_token_creator) == 0
    error_message = "accounts planned while disarmed: ${jsonencode(keys(google_service_account.scoped))}"
  }
  assert {
    condition     = length(output.scoped_service_accounts) == 0
    error_message = "the output names members while disarmed: ${jsonencode(keys(output.scoped_service_accounts))}"
  }
}

# Armed: the host project and each explicit project less an exact exclude,
# keyed on the project id. The account id is the project's readable prefix
# plus eight hex characters of
# sha256("<service_account_id>/projects/<project_id>"); the literal below was
# computed outside Terraform with the module default service_account_id,
# kubeagents-platform-gsa, so a drift in either operand of the formula fails
# here rather than filing an account under a new name.
run "an_armed_pool_holds_one_account_per_listed_project" {
  command = plan

  variables {
    scoped_pool_enabled = true
    scope = {
      projects = ["team-alpha", "team-beta", "team-gamma"]
      exclude  = { projects = ["team-gamma"] }
    }
  }

  assert {
    condition     = toset(keys(google_service_account.scoped)) == toset(["mgmt-project-1", "team-alpha", "team-beta"])
    error_message = "pool keys: ${jsonencode(keys(google_service_account.scoped))}"
  }
  assert {
    condition     = toset(keys(google_service_account_iam_member.scoped_token_creator)) == toset(keys(google_service_account.scoped))
    error_message = "tokenCreator is bound once per member and on nothing else: ${jsonencode(keys(google_service_account_iam_member.scoped_token_creator))}"
  }
  assert {
    condition     = toset(keys(output.scoped_service_accounts)) == toset(["mgmt-project-1", "team-alpha", "team-beta"])
    error_message = "output keys: ${jsonencode(keys(output.scoped_service_accounts))}"
  }
  assert {
    condition     = google_service_account.scoped["team-alpha"].account_id == "ka-team-alpha-0ed42166"
    error_message = "team-alpha's account id is ${google_service_account.scoped["team-alpha"].account_id}, not ka-team-alpha-0ed42166"
  }
  assert {
    condition     = alltrue([for account in values(google_service_account.scoped) : account.project == "mgmt-project-1"])
    error_message = "every member is created in the host project: ${jsonencode([for account in values(google_service_account.scoped) : account.project])}"
  }
  assert {
    condition     = google_service_account.scoped["team-alpha"].display_name == "Kube-Agents scoped reader: team-alpha"
    error_message = "display name: ${google_service_account.scoped["team-alpha"].display_name}"
  }
  assert {
    condition     = google_service_account.scoped["team-alpha"].description == "Pool member of kubeagents-platform-gsa for projects/team-alpha. Holds no IAM grant; authority arrives with per-cluster RBAC."
    error_message = "description: ${google_service_account.scoped["team-alpha"].description}"
  }
}

# The readable prefix is the first seventeen characters of the project id
# with a trailing hyphen stripped, so the id stays within the thirty the API
# allows and never ends in a hyphen.
run "a_long_project_id_is_truncated_without_a_trailing_hyphen" {
  command = plan

  variables {
    scoped_pool_enabled = true
    scope               = { projects = ["projectname-abcd-efgh-1"] }
  }

  assert {
    condition     = google_service_account.scoped["projectname-abcd-efgh-1"].account_id == "ka-projectname-abcd-d979c84e"
    error_message = "account id: ${google_service_account.scoped["projectname-abcd-efgh-1"].account_id}"
  }
}

# A pool past the declared bound is refused at plan, once, on the agent
# account: four listed projects against a bound of three.
run "a_pool_past_the_account_cap_is_refused" {
  command = plan

  variables {
    scoped_pool_enabled      = true
    scoped_pool_max_accounts = 3
    scope                    = { projects = ["team-alpha", "team-beta", "team-gamma"] }
  }

  expect_failures = [google_service_account.agent]
}

# The same four under the default cap plan.
run "a_pool_within_the_account_cap_plans" {
  command = plan

  variables {
    scoped_pool_enabled = true
    scope               = { projects = ["team-alpha", "team-beta", "team-gamma"] }
  }

  assert {
    condition     = length(google_service_account.scoped) == 4
    error_message = "${length(google_service_account.scoped)} members planned for four listed projects"
  }
}

# A selector's members are listed at plan time and get an account each, the
# scoping project among them.
run "a_selector_member_gets_an_account" {
  command = plan

  variables {
    scoped_pool_enabled    = true
    scope                  = { metrics_scopes = ["scoping-proj1"] }
    scope_selector_members = { "metricsScopes/scoping-proj1" = ["scoping-proj1", "monitored-proj1"] }
  }

  assert {
    condition     = toset(keys(google_service_account.scoped)) == toset(["mgmt-project-1", "scoping-proj1", "monitored-proj1"])
    error_message = "pool keys: ${jsonencode(keys(google_service_account.scoped))}"
  }
}

# A folder's or an organisation's members are listed at plan time while the
# pool is armed (the composition hands the resolver's container_members in),
# and each gets a pool account on that apply -- and nothing else: no
# per-project binding, because the container-level grant is inherited, and no
# place in the resolved-set count, because containers come last in the
# reconcile's order and are not counted at plan. A project created under the
# container since the last apply joins the pool on the next one.
run "a_folders_listed_members_are_pool_members_and_nothing_else" {
  command = plan

  variables {
    scoped_pool_enabled = true
    scope = {
      projects      = ["team-alpha"]
      folders       = ["123456789012"]
      organizations = ["987654321098"]
    }
    scope_container_members = {
      "folders/123456789012"       = ["folder-proj1", "folder-proj2", "mgmt-project-1"]
      "organizations/987654321098" = ["org-proj1", "folder-proj1"]
    }
  }

  assert {
    condition     = toset(keys(google_service_account.scoped)) == toset(["mgmt-project-1", "team-alpha", "folder-proj1", "folder-proj2", "org-proj1"])
    error_message = "pool keys: ${jsonencode(keys(google_service_account.scoped))}"
  }
  assert {
    condition     = toset(keys(output.scoped_service_accounts)) == toset(keys(google_service_account.scoped))
    error_message = "output keys: ${jsonencode(keys(output.scoped_service_accounts))}"
  }
  assert {
    condition     = toset([for binding in values(google_project_iam_member.scope_roles) : binding.project]) == toset(["team-alpha"])
    error_message = "a container's member is bound per project; the container grant is inherited: ${jsonencode(distinct([for binding in values(google_project_iam_member.scope_roles) : binding.project]))}"
  }
  assert {
    condition     = toset(output.scope_bound_projects) == toset(["team-alpha"])
    error_message = "scope_bound_projects carries a container's member: ${jsonencode(output.scope_bound_projects)}"
  }
  assert {
    condition     = length(google_folder_iam_member.scope_roles) == length(output.scope_container_roles) && length(google_organization_iam_member.scope_roles) == length(output.scope_container_roles)
    error_message = "the container bindings changed: ${length(google_folder_iam_member.scope_roles)} folder, ${length(google_organization_iam_member.scope_roles)} organisation"
  }
}

# The members' place in the pool is bounded by the pool cap alone, not the
# resolved-set cap: the same declaration under scope.max_projects = 2 (the
# management project and team-alpha) plans, because the container's three
# members are not counted toward the resolved set.
run "a_containers_members_are_not_counted_toward_the_resolved_set_cap" {
  command = plan

  variables {
    scoped_pool_enabled = true
    scope = {
      projects     = ["team-alpha"]
      folders      = ["123456789012"]
      max_projects = 2
    }
    scope_container_members = {
      "folders/123456789012" = ["folder-proj1", "folder-proj2", "folder-proj3"]
    }
  }

  assert {
    condition     = length(google_service_account.scoped) == 5
    error_message = "${length(google_service_account.scoped)} members planned for two listed projects and three container members"
  }
}

# The pool cap counts a container's member like any other: three members
# under a folder beside the management project, against a bound of three.
run "a_containers_members_count_toward_the_pool_cap" {
  command = plan

  variables {
    scoped_pool_enabled      = true
    scoped_pool_max_accounts = 3
    scope                    = { folders = ["123456789012"] }
    scope_container_members = {
      "folders/123456789012" = ["folder-proj1", "folder-proj2", "folder-proj3"]
    }
  }

  expect_failures = [google_service_account.agent]
}

# An exact exclude.projects entry drops a container's member from the pool,
# the operator's one lever over a member the plan listed; a glob is the
# reconcile's alone and drops nothing here.
run "an_exact_exclude_drops_a_containers_member_from_the_pool" {
  command = plan

  variables {
    scoped_pool_enabled = true
    scope = {
      folders = ["123456789012"]
      exclude = { projects = ["folder-proj2", "folder-*"] }
    }
    scope_container_members = {
      "folders/123456789012" = ["folder-proj1", "folder-proj2", "folder-proj3"]
    }
  }

  assert {
    condition     = toset(keys(google_service_account.scoped)) == toset(["mgmt-project-1", "folder-proj1", "folder-proj3"])
    error_message = "pool keys: ${jsonencode(keys(google_service_account.scoped))}"
  }
}

# Armed beside a container whose key the input lacks: refused on the agent
# account, naming the container, so a module caller that skipped the resolver
# is told rather than getting the container's clusters refused by the broker.
run "an_armed_pool_beside_an_unlisted_container_is_refused" {
  command = plan

  variables {
    scoped_pool_enabled = true
    scope = {
      folders       = ["123456789012"]
      organizations = ["987654321098"]
    }
    scope_container_members = {
      "folders/123456789012" = ["folder-proj1"]
    }
  }

  expect_failures = [google_service_account.agent]
}

# Disarmed, no container is read and the input stays empty: a folder in scope
# plans with nothing in scope_container_members, and no member is created.
run "a_disarmed_pool_beside_a_container_needs_no_listing" {
  command = plan

  variables {
    scope = {
      projects = ["team-alpha"]
      folders  = ["123456789012"]
    }
  }

  assert {
    condition     = length(google_service_account.scoped) == 0
    error_message = "accounts planned while disarmed: ${jsonencode(keys(google_service_account.scoped))}"
  }
  assert {
    condition     = length(google_folder_iam_member.scope_roles) == length(output.scope_container_roles)
    error_message = "the folder binding changed: ${length(google_folder_iam_member.scope_roles)}"
  }
}

# A container with no clusters lists to an empty entry: the key is present,
# so the plan is not refused, and it adds no member.
run "a_container_with_no_members_adds_nothing_and_is_not_refused" {
  command = plan

  variables {
    scoped_pool_enabled     = true
    scope                   = { folders = ["123456789012"] }
    scope_container_members = { "folders/123456789012" = [] }
  }

  assert {
    condition     = toset(keys(google_service_account.scoped)) == toset(["mgmt-project-1"])
    error_message = "pool keys: ${jsonencode(keys(google_service_account.scoped))}"
  }
}

# The input has the shape the resolver's output has, and nothing else: a key
# that is not folders/<id> or organizations/<id>, or a member that is not a
# project ID the CRD accepts (a number, an uppercase name), is refused at the
# variable, before the pool would file an account under a key the broker
# never looks up.
run "a_malformed_container_listing_is_refused_at_the_variable" {
  command = plan

  variables {
    scoped_pool_enabled = true
    scope = {
      projects      = []
      folders       = ["123456789012"]
      organizations = []
    }
    scope_container_members = {
      "folders/123456789012" = ["200000000002"]
    }
  }

  expect_failures = [var.scope_container_members]
}

run "a_container_key_of_the_wrong_shape_is_refused_at_the_variable" {
  command = plan

  variables {
    scoped_pool_enabled = true
    scope = {
      projects      = []
      folders       = ["123456789012"]
      organizations = []
    }
    scope_container_members = {
      "folder/123456789012" = ["team-alpha"]
    }
  }

  expect_failures = [var.scope_container_members]
}
