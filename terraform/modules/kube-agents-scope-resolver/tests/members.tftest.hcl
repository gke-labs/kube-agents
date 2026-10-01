# What the two selectors resolve to, from the documents the APIs answer.
# Every read is overridden, and both providers are mocked so a read this
# file forgets to override fails on a mocked answer rather than reaching the
# network. Conditions read the module's locals and outputs, never a
# data.http instance: its request_headers carry the provider token, so the
# value is marked sensitive and a failing assertion that references it
# crashes Terraform's diagnostic renderer instead of printing the message.
# The google_client_config count carries no such mark and is read directly.

mock_provider "google" {}
mock_provider "http" {}

variables {
  quota_project    = "mgmt-project-1"
  shared_vpc_hosts = ["host-proj-1"]
  metrics_scopes   = ["scoping-proj1"]
}

run "no_selector_makes_no_read" {
  command = plan

  variables {
    shared_vpc_hosts = []
    metrics_scopes   = []
  }

  assert {
    condition     = output.members == {} && output.uncarriable_members == {}
    error_message = "with no selector declared, nothing resolves: ${jsonencode(output.members)}"
  }
  assert {
    condition     = !local.scope_resolves_selectors && length(data.google_client_config.scope_resolver) == 0 && length(local.scope_monitored_numbers) == 0
    error_message = "with no selector declared, no token is fetched and there is no number to name"
  }
}

# A host's service projects by ID (a non-project resource left out) and a
# scope's monitored projects by number, each number named through Resource
# Manager; keyed under the snapshot names and sorted.
run "each_selector_resolves_to_its_members" {
  command = plan

  override_data {
    target = data.http.scope_shared_vpc_host["host-proj-1"]
    values = {
      status_code   = 200
      response_body = jsonencode({ resources = [{ id = "svc-proj-2", type = "PROJECT" }, { id = "svc-proj-1", type = "PROJECT" }, { id = "shared-subnet", type = "SUBNETWORK" }] })
    }
  }
  override_data {
    target = data.http.scope_metrics_scope["scoping-proj1"]
    values = {
      status_code   = 200
      response_body = jsonencode({ monitoredProjects = [{ name = "locations/global/metricsScopes/scoping-proj1/projects/100000000001" }, { name = "locations/global/metricsScopes/scoping-proj1/projects/100000000002" }] })
    }
  }
  override_data {
    target = data.http.scope_monitored_project["100000000001"]
    values = { status_code = 200, response_body = jsonencode({ projectId = "scoping-proj1" }) }
  }
  override_data {
    target = data.http.scope_monitored_project["100000000002"]
    values = { status_code = 200, response_body = jsonencode({ projectId = "monitored-proj1" }) }
  }

  assert {
    condition = jsonencode(output.members) == jsonencode({
      "sharedVpcHosts/host-proj-1"  = ["svc-proj-1", "svc-proj-2"]
      "metricsScopes/scoping-proj1" = ["monitored-proj1", "scoping-proj1"]
    })
    error_message = "members: ${jsonencode(output.members)}"
  }
  assert {
    condition     = output.uncarriable_members == {}
    error_message = "every member here is an ID the scope can carry: ${jsonencode(output.uncarriable_members)}"
  }
  assert {
    condition     = local.scope_monitored_numbers == toset(["100000000001", "100000000002"])
    error_message = "one naming read per monitored number: ${jsonencode(local.scope_monitored_numbers)}"
  }
}

# A project that is not a Shared VPC host, and a host with nothing attached:
# no members, no refusal, as at runtime.
run "a_project_that_is_not_a_host_resolves_to_no_members" {
  command = plan

  variables {
    metrics_scopes = []
  }

  override_data {
    target = data.http.scope_shared_vpc_host["host-proj-1"]
    values = {
      status_code   = 400
      response_body = jsonencode({ error = { code = 400, message = "Invalid resource usage: 'The resource 'projects/host-proj-1' is not a shared VPC host project.'." } })
    }
  }

  assert {
    condition     = jsonencode(output.members) == jsonencode({ "sharedVpcHosts/host-proj-1" = [] })
    error_message = "members: ${jsonencode(output.members)}"
  }
}

run "a_host_with_nothing_attached_resolves_to_no_members" {
  command = plan

  variables {
    metrics_scopes = []
  }

  override_data {
    target = data.http.scope_shared_vpc_host["host-proj-1"]
    values = {
      status_code   = 200
      response_body = jsonencode({ kind = "compute#projectsGetXpnResources" })
    }
  }

  assert {
    condition     = jsonencode(output.members) == jsonencode({ "sharedVpcHosts/host-proj-1" = [] })
    error_message = "members: ${jsonencode(output.members)}"
  }
}
