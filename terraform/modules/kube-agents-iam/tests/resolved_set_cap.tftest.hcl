# The whole-set cap: the management project, scope.projects and every
# selector's members, once each and less an exact exclude entry, may not
# exceed what the reconcile lists (RESOLVED_SET_CAP), and the precondition
# holds only while a selector is declared. tests/test_scope_iam.py pins the
# number to the reconcile's; these cases pin how it is counted.

mock_provider "google" {}

variables {
  project_id = "mgmt-project-1"
}

# The CRD's hundred explicit projects and no selector: the count is 101, and
# the plan is not refused for it, as it was not before the selectors existed.
run "a_hundred_explicit_projects_without_a_selector_plan" {
  command = plan

  variables {
    scope = { projects = [for i in range(100) : format("team-%03d", i)] }
  }

  assert {
    condition     = length(local.scope_listed_projects) == 101
    error_message = "the count is ${length(local.scope_listed_projects)}, not 101"
  }
}

# The same hundred beside a selector that resolves to its scoping project
# alone: 102, refused.
run "a_hundred_explicit_projects_beside_a_selector_are_refused" {
  command = plan

  variables {
    scope                  = { projects = [for i in range(100) : format("team-%03d", i)], metrics_scopes = ["scoping-proj1"] }
    scope_selector_members = { "metricsScopes/scoping-proj1" = ["scoping-proj1"] }
  }

  expect_failures = [google_service_account.agent]
}

run "ninety_eight_explicit_projects_beside_that_selector_fit" {
  command = plan

  variables {
    scope                  = { projects = [for i in range(98) : format("team-%03d", i)], metrics_scopes = ["scoping-proj1"] }
    scope_selector_members = { "metricsScopes/scoping-proj1" = ["scoping-proj1"] }
  }

  assert {
    condition     = length(local.scope_listed_projects) == 100
    error_message = "the count is ${length(local.scope_listed_projects)}, not 100"
  }
}

# Two lists each under the per-list cap whose sum is over it: 1 + 50 + 60.
run "two_lists_under_the_cap_whose_sum_is_over_are_refused" {
  command = plan

  variables {
    scope = {
      projects       = [for i in range(50) : format("explicit-proj-%04d", i + 1)]
      metrics_scopes = ["scoping-proj1"]
    }
    scope_selector_members = {
      "metricsScopes/scoping-proj1" = concat(["scoping-proj1"], [for i in range(59) : format("monitored-proj-%04d", i + 1)])
    }
  }

  expect_failures = [google_service_account.agent]
}

# An exact ID entry lowers the count on either leg: ten monitored projects
# excluded leave 101, refused; one explicit project more brings it to 100.
run "ten_exact_excludes_on_the_selector_leg_leave_it_one_over" {
  command = plan

  variables {
    scope = {
      projects       = [for i in range(50) : format("explicit-proj-%04d", i + 1)]
      metrics_scopes = ["scoping-proj1"]
      exclude        = { projects = [for i in range(10) : format("monitored-proj-%04d", i + 1)] }
    }
    scope_selector_members = {
      "metricsScopes/scoping-proj1" = concat(["scoping-proj1"], [for i in range(59) : format("monitored-proj-%04d", i + 1)])
    }
  }

  expect_failures = [google_service_account.agent]
}

run "an_exact_exclude_on_each_leg_brings_it_to_the_cap" {
  command = plan

  variables {
    scope = {
      projects       = [for i in range(50) : format("explicit-proj-%04d", i + 1)]
      metrics_scopes = ["scoping-proj1"]
      exclude        = { projects = concat(["explicit-proj-0001"], [for i in range(10) : format("monitored-proj-%04d", i + 1)]) }
    }
    scope_selector_members = {
      "metricsScopes/scoping-proj1" = concat(["scoping-proj1"], [for i in range(59) : format("monitored-proj-%04d", i + 1)])
    }
  }

  assert {
    condition     = length(local.scope_listed_projects) == 100
    error_message = "the count is ${length(local.scope_listed_projects)}, not 100"
  }
}

# A glob is the reconcile's alone: the same set under `monitored-*` is
# refused.
run "a_glob_does_not_lower_the_count" {
  command = plan

  variables {
    scope = {
      projects       = [for i in range(50) : format("explicit-proj-%04d", i + 1)]
      metrics_scopes = ["scoping-proj1"]
      exclude        = { projects = ["monitored-*"] }
    }
    scope_selector_members = {
      "metricsScopes/scoping-proj1" = concat(["scoping-proj1"], [for i in range(59) : format("monitored-proj-%04d", i + 1)])
    }
  }

  expect_failures = [google_service_account.agent]
}

# A project named on both legs is counted once.
run "a_project_on_both_legs_is_counted_once" {
  command = plan

  variables {
    scope = {
      projects       = [for i in range(50) : format("shared-proj-%04d", i + 1)]
      metrics_scopes = ["scoping-proj1"]
    }
    scope_selector_members = {
      "metricsScopes/scoping-proj1" = concat(["scoping-proj1"], [for i in range(50) : format("shared-proj-%04d", i + 1)])
    }
  }

  assert {
    condition     = length(local.scope_listed_projects) == 52
    error_message = "the count is ${length(local.scope_listed_projects)}, not 52"
  }
}

# A folder beside a set at the cap is not counted: its members are unknown
# here and its binding is one on the container.
run "a_container_is_not_counted" {
  command = plan

  variables {
    scope = {
      projects       = [for i in range(98) : format("team-%03d", i)]
      folders        = ["123456789012"]
      metrics_scopes = ["scoping-proj1"]
    }
    scope_selector_members = { "metricsScopes/scoping-proj1" = ["scoping-proj1"] }
  }

  assert {
    condition     = length(local.scope_listed_projects) == 100
    error_message = "the count is ${length(local.scope_listed_projects)}, not 100"
  }
}
