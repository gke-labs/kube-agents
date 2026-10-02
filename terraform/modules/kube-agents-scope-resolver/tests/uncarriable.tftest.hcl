# A member named by a legacy domain-scoped ID, which the scope cannot carry:
# left out of `members`, reported in `uncarriable_members` and by the check
# block, while the selector's other members resolve as usual. And a legacy
# management project, which quota_project has to admit for the plans and
# destroys of such an install.

mock_provider "google" {}
mock_provider "http" {}

variables {
  quota_project    = "mgmt-project-1"
  shared_vpc_hosts = ["host-proj-1"]
  metrics_scopes   = ["scoping-proj1"]
}

run "a_legacy_id_member_is_left_out_and_warned" {
  command = plan

  override_data {
    target = data.http.scope_shared_vpc_host["host-proj-1"]
    values = {
      status_code   = 200
      response_body = jsonencode({ resources = [{ id = "svc-proj-1", type = "PROJECT" }, { id = "example.com:legacy-svc", type = "PROJECT" }] })
    }
  }
  override_data {
    target = data.http.scope_metrics_scope["scoping-proj1"]
    values = {
      status_code   = 200
      response_body = jsonencode({ monitoredProjects = [{ name = "locations/global/metricsScopes/scoping-proj1/projects/100000000001" }, { name = "locations/global/metricsScopes/scoping-proj1/projects/example.com:legacy-mon" }] })
    }
  }
  override_data {
    target = data.http.scope_monitored_project["100000000001"]
    values = { status_code = 200, response_body = jsonencode({ projectId = "scoping-proj1" }) }
  }

  assert {
    condition = jsonencode(output.members) == jsonencode({
      "sharedVpcHosts/host-proj-1"  = ["svc-proj-1"]
      "metricsScopes/scoping-proj1" = ["scoping-proj1"]
    })
    error_message = "members: ${jsonencode(output.members)}"
  }
  assert {
    condition = jsonencode(output.uncarriable_members) == jsonencode({
      "sharedVpcHosts/host-proj-1"  = ["example.com:legacy-svc"]
      "metricsScopes/scoping-proj1" = ["example.com:legacy-mon"]
    })
    error_message = "uncarriable: ${jsonencode(output.uncarriable_members)}"
  }
  assert {
    condition     = length(local.scope_monitored_numbers) == 1
    error_message = "a member the API named by an ID has no number to name: ${jsonencode(local.scope_monitored_numbers)}"
  }

  expect_failures = [check.selector_members_the_scope_can_carry]
}

run "a_legacy_management_project_plans_with_no_selector" {
  command = plan

  variables {
    quota_project    = "example.com:legacy-mgmt"
    shared_vpc_hosts = []
    metrics_scopes   = []
  }

  assert {
    condition     = output.members == {}
    error_message = "no selector, no members"
  }
}
