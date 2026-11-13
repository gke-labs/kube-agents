# What a folder's and an organisation's members resolve to for the scoped
# service account pool, from the Cloud Asset Inventory document the search
# answers, and the answers the container read refuses. Read only while
# list_container_members is set (the pool is armed); the members are the
# pool's alone, so `members` and the selector reads are untouched. As in the
# other files, conditions read locals and outputs, never a data.http
# instance, whose request_headers carry the provider token.

mock_provider "google" {}
mock_provider "http" {}

variables {
  quota_project          = "mgmt-project-1"
  folders                = ["123456789012"]
  list_container_members = true
}

# Five clusters in three projects, two of them regional and one zonal, listed
# under the folder's key, distinct and sorted. The token is fetched for the
# container read with no selector declared.
run "a_folder_resolves_to_the_projects_its_clusters_are_in" {
  command = plan

  override_data {
    target = data.http.scope_container["folders/123456789012"]
    values = {
      status_code = 200
      response_body = jsonencode({ results = [
        { name = "//container.googleapis.com/projects/fleet-proj-b/locations/us-central1/clusters/prod", assetType = "container.googleapis.com/Cluster", project = "projects/200000000002", location = "us-central1" },
        { name = "//container.googleapis.com/projects/fleet-proj-a/locations/us-central1/clusters/prod", assetType = "container.googleapis.com/Cluster", project = "projects/200000000001", location = "us-central1" },
        { name = "//container.googleapis.com/projects/fleet-proj-a/zones/us-central1-a/clusters/dev", assetType = "container.googleapis.com/Cluster", project = "projects/200000000001", location = "us-central1-a" },
        { name = "//container.googleapis.com/projects/fleet-proj-c/locations/europe-west1/clusters/prod", assetType = "container.googleapis.com/Cluster", project = "projects/200000000003", location = "europe-west1" },
        { name = "//container.googleapis.com/projects/fleet-proj-b/locations/us-east1/clusters/staging", assetType = "container.googleapis.com/Cluster", project = "projects/200000000002", location = "us-east1" },
      ] })
    }
  }

  assert {
    condition     = jsonencode(output.container_members) == jsonencode({ "folders/123456789012" = ["fleet-proj-a", "fleet-proj-b", "fleet-proj-c"] })
    error_message = "container_members: ${jsonencode(output.container_members)}"
  }
  assert {
    condition     = output.members == {} && output.uncarriable_members == {}
    error_message = "a container's members are the pool's alone, never a selector's: ${jsonencode(output.members)}"
  }
  assert {
    condition     = length(data.google_client_config.scope_resolver) == 1 && local.scope_container_url["folders/123456789012"] == "https://cloudasset.googleapis.com/v1/folders/123456789012:searchAllResources?assetTypes=container.googleapis.com/Cluster&pageSize=500"
    error_message = "the container read is the reconcile's search, made with the provider's token: ${jsonencode(local.scope_container_url)}"
  }
}

run "an_organisation_resolves_under_its_own_key" {
  command = plan

  variables {
    folders       = []
    organizations = ["987654321098"]
  }

  override_data {
    target = data.http.scope_container["organizations/987654321098"]
    values = {
      status_code   = 200
      response_body = jsonencode({ results = [{ name = "//container.googleapis.com/projects/org-proj-1/locations/us-central1/clusters/prod" }] })
    }
  }

  assert {
    condition     = jsonencode(output.container_members) == jsonencode({ "organizations/987654321098" = ["org-proj-1"] })
    error_message = "container_members: ${jsonencode(output.container_members)}"
  }
  assert {
    condition     = local.scope_container_url["organizations/987654321098"] == "https://cloudasset.googleapis.com/v1/organizations/987654321098:searchAllResources?assetTypes=container.googleapis.com/Cluster&pageSize=500"
    error_message = "url: ${jsonencode(local.scope_container_url)}"
  }
}

# An exact exclude_projects entry drops a member from the pool; a glob is the
# reconcile's alone and drops nothing here.
run "an_exact_exclude_drops_a_member" {
  command = plan

  variables {
    exclude_projects = ["fleet-proj-b", "fleet-*"]
  }

  override_data {
    target = data.http.scope_container["folders/123456789012"]
    values = {
      status_code = 200
      response_body = jsonencode({ results = [
        { name = "//container.googleapis.com/projects/fleet-proj-a/locations/us-central1/clusters/prod" },
        { name = "//container.googleapis.com/projects/fleet-proj-b/locations/us-central1/clusters/prod" },
      ] })
    }
  }

  assert {
    condition     = jsonencode(output.container_members) == jsonencode({ "folders/123456789012" = ["fleet-proj-a"] })
    error_message = "container_members: ${jsonencode(output.container_members)}"
  }
}

# A legacy domain-scoped ID cannot hold a pool account: left out, reported
# under the container's key and by the check block, the other members listed.
run "a_legacy_id_member_is_left_out_and_warned" {
  command = plan

  override_data {
    target = data.http.scope_container["folders/123456789012"]
    values = {
      status_code = 200
      response_body = jsonencode({ results = [
        { name = "//container.googleapis.com/projects/fleet-proj-a/locations/us-central1/clusters/prod" },
        { name = "//container.googleapis.com/projects/example.com:legacy-fleet/locations/us-central1/clusters/prod" },
      ] })
    }
  }

  assert {
    condition     = jsonencode(output.container_members) == jsonencode({ "folders/123456789012" = ["fleet-proj-a"] })
    error_message = "container_members: ${jsonencode(output.container_members)}"
  }
  assert {
    condition     = jsonencode(output.uncarriable_members) == jsonencode({ "folders/123456789012" = ["example.com:legacy-fleet"] })
    error_message = "uncarriable: ${jsonencode(output.uncarriable_members)}"
  }

  expect_failures = [check.selector_members_the_scope_can_carry]
}

# The management project is the pool's unconditionally (kube-agents-iam seeds
# it and drops it from a container's members itself), so a folder whose
# clusters include its own lists it in neither container_members nor
# uncarriable_members, whichever ID form it has, and the check stays quiet.
run "the_management_project_is_not_a_container_member" {
  command = plan

  override_data {
    target = data.http.scope_container["folders/123456789012"]
    values = {
      status_code = 200
      response_body = jsonencode({ results = [
        { name = "//container.googleapis.com/projects/mgmt-project-1/locations/us-central1/clusters/mgmt" },
        { name = "//container.googleapis.com/projects/fleet-proj-a/locations/us-central1/clusters/prod" },
      ] })
    }
  }

  assert {
    condition     = jsonencode(output.container_members) == jsonencode({ "folders/123456789012" = ["fleet-proj-a"] })
    error_message = "container_members: ${jsonencode(output.container_members)}"
  }
  assert {
    condition     = output.uncarriable_members == {}
    error_message = "uncarriable: ${jsonencode(output.uncarriable_members)}"
  }
}

run "a_legacy_management_project_is_neither_a_member_nor_uncarriable" {
  command = plan

  variables {
    quota_project = "example.com:mgmt-project"
  }

  override_data {
    target = data.http.scope_container["folders/123456789012"]
    values = {
      status_code = 200
      response_body = jsonencode({ results = [
        { name = "//container.googleapis.com/projects/example.com:mgmt-project/locations/us-central1/clusters/mgmt" },
        { name = "//container.googleapis.com/projects/fleet-proj-a/locations/us-central1/clusters/prod" },
      ] })
    }
  }

  assert {
    condition     = jsonencode(output.container_members) == jsonencode({ "folders/123456789012" = ["fleet-proj-a"] })
    error_message = "container_members: ${jsonencode(output.container_members)}"
  }
  assert {
    condition     = output.uncarriable_members == {}
    error_message = "the management project gets its pool account regardless, so it is not uncarriable: ${jsonencode(output.uncarriable_members)}"
  }
}

# A container that answers no member at all, with no `results` key or an
# empty one, lists to an empty list, not a refusal, and the key is present
# for kube-agents-iam's precondition; but the search is eventually
# consistent and the plan has no state to grace an index gap with, so an
# empty answer for a listed container is warned about, since applying it
# retires every member's pool account.
run "a_container_with_no_cluster_resolves_to_an_empty_list_and_is_warned" {
  command = plan

  override_data {
    target = data.http.scope_container["folders/123456789012"]
    values = { status_code = 200, response_body = jsonencode({}) }
  }

  assert {
    condition     = jsonencode(output.container_members) == jsonencode({ "folders/123456789012" = [] })
    error_message = "container_members: ${jsonencode(output.container_members)}"
  }

  expect_failures = [check.listed_containers_name_a_member]
}

run "an_empty_results_array_is_warned_the_same_way" {
  command = plan

  override_data {
    target = data.http.scope_container["folders/123456789012"]
    values = { status_code = 200, response_body = jsonencode({ results = [] }) }
  }

  assert {
    condition     = jsonencode(output.container_members) == jsonencode({ "folders/123456789012" = [] })
    error_message = "container_members: ${jsonencode(output.container_members)}"
  }

  expect_failures = [check.listed_containers_name_a_member]
}

# The pool off: no container is read, whatever is declared, and the output is
# empty rather than a map of empty lists.
run "nothing_is_read_when_listing_is_off" {
  command = plan

  variables {
    list_container_members = false
    organizations          = ["987654321098"]
  }

  assert {
    condition     = output.container_members == {} && length(local.scope_listed_containers) == 0
    error_message = "with the pool off, no container is listed: ${jsonencode(output.container_members)}"
  }
  assert {
    condition     = length(data.google_client_config.scope_resolver) == 0
    error_message = "with no selector and no listing, no token is fetched"
  }
}

run "a_container_the_identity_cannot_search_is_refused" {
  command = plan

  override_data {
    target = data.http.scope_container["folders/123456789012"]
    values = {
      status_code   = 403
      response_body = jsonencode({ error = { code = 403, message = "Permission 'cloudasset.assets.searchAllResources' denied on resource '//cloudresourcemanager.googleapis.com/folders/123456789012' (or it may not exist).", status = "PERMISSION_DENIED" } })
    }
  }

  expect_failures = [data.http.scope_container]
}

run "a_disabled_asset_api_in_the_consumer_project_is_refused" {
  command = plan

  override_data {
    target = data.http.scope_container["folders/123456789012"]
    values = {
      status_code   = 403
      response_body = jsonencode({ error = { code = 403, message = "Cloud Asset API has not been used in project mgmt-project-1 before or it is disabled.", status = "PERMISSION_DENIED", details = [{ "@type" = "type.googleapis.com/google.rpc.ErrorInfo", reason = "SERVICE_DISABLED", domain = "googleapis.com" }] } })
    }
  }

  expect_failures = [data.http.scope_container]
}

# A 200 that is not the searchAllResources document: a list, and a result
# whose name is not a cluster's. Each would otherwise read as a container
# with no clusters.
run "a_200_that_decodes_to_a_list_is_refused" {
  command = plan

  override_data {
    target = data.http.scope_container["folders/123456789012"]
    values = { status_code = 200, response_body = jsonencode([]) }
  }

  expect_failures = [data.http.scope_container]
}

run "a_result_that_is_not_a_cluster_name_is_refused" {
  command = plan

  override_data {
    target = data.http.scope_container["folders/123456789012"]
    values = { status_code = 200, response_body = jsonencode({ results = [{ name = "//compute.googleapis.com/projects/fleet-proj-a/zones/us-central1-a/instances/vm" }] }) }
  }

  expect_failures = [data.http.scope_container]
}

run "a_second_page_is_refused" {
  command = plan

  override_data {
    target = data.http.scope_container["folders/123456789012"]
    values = { status_code = 200, response_body = jsonencode({ results = [{ name = "//container.googleapis.com/projects/fleet-proj-a/locations/us-central1/clusters/prod" }], nextPageToken = "CgVwYWdlMg" }) }
  }

  expect_failures = [data.http.scope_container]
}

# The per-container member cap, counted less an exact exclude entry: 101
# projects refused, 101 with one excluded listed.
run "a_container_past_the_cap_is_refused" {
  command = plan

  override_data {
    target = data.http.scope_container["folders/123456789012"]
    values = {
      status_code   = 200
      response_body = jsonencode({ results = [for i in range(101) : { name = format("//container.googleapis.com/projects/fleet-proj-%04d/locations/us-central1/clusters/prod", i + 1) }] })
    }
  }

  expect_failures = [data.http.scope_container]
}

run "one_past_the_cap_less_an_exact_exclude_fits" {
  command = plan

  variables {
    exclude_projects = ["fleet-proj-0001"]
  }

  override_data {
    target = data.http.scope_container["folders/123456789012"]
    values = {
      status_code   = 200
      response_body = jsonencode({ results = [for i in range(101) : { name = format("//container.googleapis.com/projects/fleet-proj-%04d/locations/us-central1/clusters/prod", i + 1) }] })
    }
  }

  assert {
    condition     = length(output.container_members["folders/123456789012"]) == 100 && !contains(output.container_members["folders/123456789012"], "fleet-proj-0001")
    error_message = "the excluded member is dropped and the other hundred listed: ${length(output.container_members["folders/123456789012"])}"
  }
}
