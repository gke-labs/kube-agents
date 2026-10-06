# The scoped service account pool: one account per project the plan can list
# in the scope (scope.tf, local.scope_listed_projects), keyed on the bare
# project id, and only while scoped_pool_enabled arms it. These cases pin the
# key, the account id a project produces, the declared bound on the pool
# (scoped_pool_max_accounts, which the plan holds the derived set to; it is
# not a reading of the host project's quota), and that a selector's members
# are pool members like an explicit project. The provider is mocked, so a plan here creates nothing.

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

# A folder's or an organisation's members are not listed at plan time, so an
# armed pool with a folder in scope creates members for the host project and
# the explicit projects only: no member for the folder or anything under it.
# This is the omission the design records as a follow-up and the broker
# refuses on at runtime, when a project under the folder has no pool entry.
run "a_folder_in_scope_adds_no_pool_member" {
  command = plan

  variables {
    scoped_pool_enabled = true
    scope = {
      projects      = ["team-alpha"]
      folders       = ["123456789012"]
      organizations = []
    }
  }

  assert {
    condition     = toset(keys(google_service_account.scoped)) == toset(["mgmt-project-1", "team-alpha"])
    error_message = "pool keys: ${jsonencode(keys(google_service_account.scoped))}"
  }
  assert {
    condition     = toset(keys(output.scoped_service_accounts)) == toset(["mgmt-project-1", "team-alpha"])
    error_message = "output keys: ${jsonencode(keys(output.scoped_service_accounts))}"
  }
}
