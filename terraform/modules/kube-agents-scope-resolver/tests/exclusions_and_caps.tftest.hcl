# The per-selector cap, counted less an exact exclude entry as the reconcile
# counts: by number for a monitored project, which is then neither named nor
# listed, and by ID for a service project, which stays listed here and is
# withheld by kube-agents-iam.

mock_provider "google" {}
mock_provider "http" {}

variables {
  quota_project    = "mgmt-project-1"
  metrics_scopes   = ["scoping-proj1"]
  shared_vpc_hosts = ["host-proj-1"]
  exclude_projects = ["100000000001", "svc-proj-0001"]
}

# 101 monitored projects and 101 service projects, one of each named in
# exclude_projects: both selectors fit.
run "one_past_the_cap_less_an_exact_exclude_fits" {
  command = plan

  override_data {
    target = data.http.scope_metrics_scope["scoping-proj1"]
    values = {
      status_code   = 200
      response_body = jsonencode({ monitoredProjects = [for i in range(101) : { name = "locations/global/metricsScopes/scoping-proj1/projects/${100000000001 + i}" }] })
    }
  }
  override_data {
    target = data.http.scope_monitored_project
    values = { status_code = 200, response_body = jsonencode({ projectId = "named-project" }) }
  }
  override_data {
    target = data.http.scope_shared_vpc_host["host-proj-1"]
    values = {
      status_code   = 200
      response_body = jsonencode({ resources = [for i in range(101) : { id = format("svc-proj-%04d", i + 1), type = "PROJECT" }] })
    }
  }

  assert {
    condition     = !contains(local.scope_monitored_numbers, "100000000001") && length(local.scope_monitored_numbers) == 100
    error_message = "an excluded number is not named; the other hundred are: ${jsonencode(local.scope_monitored_numbers)}"
  }
  assert {
    condition     = length(output.members["sharedVpcHosts/host-proj-1"]) == 101 && contains(output.members["sharedVpcHosts/host-proj-1"], "svc-proj-0001")
    error_message = "a service project excluded by ID is still listed; kube-agents-iam withholds its grant"
  }
}

# The same two answers with no exclusion: both refused at the read.
run "one_past_the_cap_is_refused" {
  command = plan

  variables {
    exclude_projects = []
  }

  override_data {
    target = data.http.scope_metrics_scope["scoping-proj1"]
    values = {
      status_code   = 200
      response_body = jsonencode({ monitoredProjects = [for i in range(101) : { name = "locations/global/metricsScopes/scoping-proj1/projects/${100000000001 + i}" }] })
    }
  }
  override_data {
    target = data.http.scope_monitored_project
    values = { status_code = 200, response_body = jsonencode({ projectId = "named-project" }) }
  }
  override_data {
    target = data.http.scope_shared_vpc_host["host-proj-1"]
    values = {
      status_code   = 200
      response_body = jsonencode({ resources = [for i in range(101) : { id = format("svc-proj-%04d", i + 1), type = "PROJECT" }] })
    }
  }

  expect_failures = [
    data.http.scope_metrics_scope,
    data.http.scope_shared_vpc_host,
  ]
}

# An ID entry does not lower a Metrics Scope's count: the scope names its
# projects by number, and the ID is matched by the reconcile after naming.
run "an_id_entry_does_not_lower_a_metrics_scope_count" {
  command = plan

  variables {
    shared_vpc_hosts = []
    exclude_projects = ["named-project"]
  }

  override_data {
    target = data.http.scope_metrics_scope["scoping-proj1"]
    values = {
      status_code   = 200
      response_body = jsonencode({ monitoredProjects = [for i in range(101) : { name = "locations/global/metricsScopes/scoping-proj1/projects/${100000000001 + i}" }] })
    }
  }
  override_data {
    target = data.http.scope_monitored_project
    values = { status_code = 200, response_body = jsonencode({ projectId = "named-project" }) }
  }

  expect_failures = [data.http.scope_metrics_scope]
}
