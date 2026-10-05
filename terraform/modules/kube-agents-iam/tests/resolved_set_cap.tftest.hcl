# The whole-set cap: the management project, scope.projects and every
# selector's members, once each and less an exact exclude entry, may not
# exceed what the reconcile lists (the declared scope.max_projects, the
# reconcile's RESOLVED_SET_CAP as its default), and the precondition holds
# while a selector is declared or the cap is below its default.
# tests/test_scope_iam.py pins the default to the reconcile's; these cases pin
# how it is counted and which caps bind.

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

# The cap is the declaration's: scope.max_projects (spec.scope.maxProjects)
# replaces the default, so three explicit projects beside a selector fit a
# cap of 4 and are refused under 3; the count itself does not move.
run "a_declared_cap_is_the_one_the_plan_counts_against" {
  command = plan

  variables {
    scope = {
      projects       = ["team-a", "team-b"]
      metrics_scopes = ["scoping-proj1"]
      max_projects   = 4
    }
    scope_selector_members = { "metricsScopes/scoping-proj1" = ["scoping-proj1"] }
  }

  assert {
    condition     = length(local.scope_listed_projects) == 4 && local.scope_resolved_set_cap == 4
    error_message = "the count is ${length(local.scope_listed_projects)} against a cap of ${local.scope_resolved_set_cap}"
  }
}

run "a_declared_cap_below_the_count_is_refused" {
  command = plan

  variables {
    scope = {
      projects       = ["team-a", "team-b"]
      metrics_scopes = ["scoping-proj1"]
      max_projects   = 3
    }
    scope_selector_members = { "metricsScopes/scoping-proj1" = ["scoping-proj1"] }
  }

  expect_failures = [google_service_account.agent]
}

# The CRD's bounds, held at the variable: zero, a fraction and a value past
# 5000 are refused before any read, one run each.
run "a_cap_of_zero_is_refused_at_the_variable" {
  command = plan

  variables {
    scope = { projects = ["team-a"], max_projects = 0 }
  }

  expect_failures = [var.scope]
}

run "a_fractional_cap_is_refused_at_the_variable" {
  command = plan

  variables {
    scope = { projects = ["team-a"], max_projects = 1.5 }
  }

  expect_failures = [var.scope]
}

run "a_cap_past_the_crds_maximum_is_refused_at_the_variable" {
  command = plan

  variables {
    scope = { projects = ["team-a"], max_projects = 5001 }
  }

  expect_failures = [var.scope]
}

# A cap below the default holds the precondition even without a selector: the
# admission the plan kept for the CRD's hundred explicit projects is for the
# default cap alone, and four projects under a declared cap of two would
# otherwise bind roles the reconcile never uses.
run "a_declared_cap_below_the_default_holds_without_a_selector" {
  command = plan

  variables {
    scope = { projects = ["team-a", "team-b", "team-c", "team-d"], max_projects = 2 }
  }

  expect_failures = [google_service_account.agent]
}

run "the_default_cap_without_a_selector_still_admits_the_crds_hundred" {
  command = plan

  variables {
    scope = { projects = [for i in range(100) : format("team-%03d", i)], max_projects = 100 }
  }

  assert {
    condition     = length(local.scope_listed_projects) == 101
    error_message = "the count is ${length(local.scope_listed_projects)}, not 101"
  }
}

# The direction the cap exists for: raised above the default, a count the default
# would refuse fits. 1 + 100 + 50 = 151 under a cap of 200.
run "a_cap_above_the_default_admits_what_fits_it" {
  command = plan

  variables {
    scope = {
      projects       = [for i in range(100) : format("team-%03d", i)]
      metrics_scopes = ["scoping-proj1"]
      max_projects   = 200
    }
    scope_selector_members = {
      "metricsScopes/scoping-proj1" = concat(["scoping-proj1"], [for i in range(49) : format("monitored-proj-%04d", i + 1)])
    }
  }

  assert {
    condition     = length(local.scope_listed_projects) == 151 && local.scope_resolved_set_cap == 200
    error_message = "the count is ${length(local.scope_listed_projects)} against a cap of ${local.scope_resolved_set_cap}"
  }
}
