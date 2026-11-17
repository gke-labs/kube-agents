# Answers the module refuses rather than resolving a selector to a set that
# may be short: a read the identity cannot make, a document that is not the
# one the module reads, a second page. Each refusal is a postcondition on the
# read, so the plan fails before anything is applied.

mock_provider "google" {}
mock_provider "http" {}

variables {
  quota_project    = "mgmt-project-1"
  shared_vpc_hosts = ["host-proj-1"]
  metrics_scopes   = []
}

run "a_host_the_identity_cannot_read_is_refused" {
  command = plan

  override_data {
    target = data.http.scope_shared_vpc_host["host-proj-1"]
    values = {
      status_code   = 403
      response_body = jsonencode({ error = { code = 403, message = "Required 'compute.projects.get' permission for 'projects/host-proj-1'", status = "PERMISSION_DENIED" } })
    }
  }

  expect_failures = [data.http.scope_shared_vpc_host]
}

run "a_disabled_api_in_the_consumer_project_is_refused" {
  command = plan

  override_data {
    target = data.http.scope_shared_vpc_host["host-proj-1"]
    values = {
      status_code   = 403
      response_body = jsonencode({ error = { code = 403, message = "Compute Engine API has not been used in project mgmt-project-1 before or it is disabled.", status = "PERMISSION_DENIED", details = [{ "@type" = "type.googleapis.com/google.rpc.ErrorInfo", reason = "SERVICE_DISABLED", domain = "googleapis.com" }] } })
    }
  }

  expect_failures = [data.http.scope_shared_vpc_host]
}

# A 400 that is not the not-a-host answer is a failed read, not an empty host.
run "another_400_is_refused" {
  command = plan

  override_data {
    target = data.http.scope_shared_vpc_host["host-proj-1"]
    values = {
      status_code   = 400
      response_body = jsonencode({ error = { code = 400, message = "Invalid value for field 'maxResults'" } })
    }
  }

  expect_failures = [data.http.scope_shared_vpc_host]
}

# A 200 whose body is a list, and one whose resources carry no id: neither is
# the getXpnResources document, and each would otherwise read as a host with
# no service projects.
run "a_200_that_decodes_to_a_list_is_refused" {
  command = plan

  override_data {
    target = data.http.scope_shared_vpc_host["host-proj-1"]
    values = { status_code = 200, response_body = jsonencode([]) }
  }

  expect_failures = [data.http.scope_shared_vpc_host]
}

run "a_resource_without_an_id_is_refused" {
  command = plan

  override_data {
    target = data.http.scope_shared_vpc_host["host-proj-1"]
    values = { status_code = 200, response_body = jsonencode({ resources = [{ type = "PROJECT" }] }) }
  }

  expect_failures = [data.http.scope_shared_vpc_host]
}

run "a_second_page_is_refused" {
  command = plan

  override_data {
    target = data.http.scope_shared_vpc_host["host-proj-1"]
    values = { status_code = 200, response_body = jsonencode({ resources = [{ id = "svc-proj-1", type = "PROJECT" }], nextPageToken = "CgVwYWdlMg" }) }
  }

  expect_failures = [data.http.scope_shared_vpc_host]
}

# The page bound holds whatever cap is declared: the plan cannot follow a
# second page, so a cap past one page does not admit a host that needs one.
run "a_second_page_is_refused_whatever_the_cap" {
  command = plan

  variables {
    quota_project    = "mgmt-project-1"
    shared_vpc_hosts = ["host-proj-1"]
    member_cap       = 1000
  }

  override_data {
    target = data.http.scope_shared_vpc_host["host-proj-1"]
    values = { status_code = 200, response_body = jsonencode({ resources = [{ id = "svc-proj-1", type = "PROJECT" }], nextPageToken = "CgVwYWdlMg" }) }
  }

  expect_failures = [data.http.scope_shared_vpc_host]
}

run "a_metrics_scope_the_identity_cannot_read_is_refused" {
  command = plan

  variables {
    shared_vpc_hosts = []
    metrics_scopes   = ["scoping-proj1"]
  }

  override_data {
    target = data.http.scope_metrics_scope["scoping-proj1"]
    values = {
      status_code   = 403
      response_body = jsonencode({ error = { code = 403, message = "Permission monitoring.metricsScopes.get denied", status = "PERMISSION_DENIED" } })
    }
  }

  expect_failures = [data.http.scope_metrics_scope]
}

# A scope always monitors its own scoping project, so a 200 with no monitored
# project, or with none at all, is not the document either.
run "a_metrics_scope_with_no_monitored_project_is_refused" {
  command = plan

  variables {
    shared_vpc_hosts = []
    metrics_scopes   = ["scoping-proj1"]
  }

  override_data {
    target = data.http.scope_metrics_scope["scoping-proj1"]
    values = { status_code = 200, response_body = jsonencode({ monitoredProjects = [] }) }
  }

  expect_failures = [data.http.scope_metrics_scope]
}

run "a_metrics_scope_answer_with_no_monitored_projects_key_is_refused" {
  command = plan

  variables {
    shared_vpc_hosts = []
    metrics_scopes   = ["scoping-proj1"]
  }

  override_data {
    target = data.http.scope_metrics_scope["scoping-proj1"]
    values = { status_code = 200, response_body = jsonencode({ name = "locations/global/metricsScopes/scoping-proj1" }) }
  }

  expect_failures = [data.http.scope_metrics_scope]
}

run "a_monitored_project_the_identity_cannot_name_is_refused" {
  command = plan

  variables {
    shared_vpc_hosts = []
    metrics_scopes   = ["scoping-proj1"]
  }

  override_data {
    target = data.http.scope_metrics_scope["scoping-proj1"]
    values = { status_code = 200, response_body = jsonencode({ monitoredProjects = [{ name = "locations/global/metricsScopes/scoping-proj1/projects/100000000001" }] }) }
  }
  override_data {
    target = data.http.scope_monitored_project["100000000001"]
    values = {
      status_code   = 403
      response_body = jsonencode({ error = { code = 403, message = "The caller does not have permission", status = "PERMISSION_DENIED" } })
    }
  }

  expect_failures = [data.http.scope_monitored_project]
}

# Resource Manager names the number with an ID the scope cannot carry: refused
# by number, since the number is what an exclude entry can name.
run "a_monitored_project_named_by_a_legacy_id_is_refused" {
  command = plan

  variables {
    shared_vpc_hosts = []
    metrics_scopes   = ["scoping-proj1"]
  }

  override_data {
    target = data.http.scope_metrics_scope["scoping-proj1"]
    values = { status_code = 200, response_body = jsonencode({ monitoredProjects = [{ name = "locations/global/metricsScopes/scoping-proj1/projects/100000000001" }] }) }
  }
  override_data {
    target = data.http.scope_monitored_project["100000000001"]
    values = { status_code = 200, response_body = jsonencode({ projectId = "example.com:legacy" }) }
  }

  expect_failures = [data.http.scope_monitored_project]
}
