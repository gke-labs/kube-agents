# The calendar-mode advice switch: off by default, and when on exactly one
# custom role carrying compute.advice.calendarMode alone, bound to the agent
# in the management project, with project_roles untouched.
# tests/test_agent_iam_ceiling.py pins that the read-only bundle itself does
# not widen; these cases pin that the switch adds the one permission and
# nothing else.

mock_provider "google" {}

variables {
  project_id = "mgmt-project-1"
}

run "off_by_default_plans_no_role_and_no_binding" {
  command = plan

  assert {
    condition     = length(google_project_iam_custom_role.calendar_mode_advice) == 0 && length(google_project_iam_member.calendar_mode_advice) == 0
    error_message = "calendar_mode_advice_enabled defaults to false and must plan no role and no binding"
  }
}

run "on_plans_the_one_permission_role_bound_in_the_management_project" {
  command = plan

  variables {
    calendar_mode_advice_enabled = true
  }

  assert {
    condition     = google_project_iam_custom_role.calendar_mode_advice[0].permissions == toset(["compute.advice.calendarMode"])
    error_message = "the role must carry compute.advice.calendarMode and nothing else, got ${jsonencode(google_project_iam_custom_role.calendar_mode_advice[0].permissions)}"
  }

  assert {
    condition     = google_project_iam_custom_role.calendar_mode_advice[0].role_id == "kubeAgentsCalendarModeAdvice" && google_project_iam_custom_role.calendar_mode_advice[0].project == "mgmt-project-1"
    error_message = "the role is kubeAgentsCalendarModeAdvice in the management project"
  }

  assert {
    condition     = length(google_project_iam_member.calendar_mode_advice) == 1 && google_project_iam_member.calendar_mode_advice[0].project == "mgmt-project-1"
    error_message = "exactly one binding, in the management project"
  }

  assert {
    condition     = length(local.agent_project_roles) == length(var.project_roles) && !contains(local.agent_project_roles, "projects/mgmt-project-1/roles/kubeAgentsCalendarModeAdvice")
    error_message = "the switch must not change project_roles"
  }
}
