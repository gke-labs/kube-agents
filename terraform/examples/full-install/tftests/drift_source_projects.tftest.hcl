# The drift ingress follows the declaration: the source sinks come from
# kube-agents-iam's scope_export_projects less drift_pubsub_source_exclude_projects,
# the exclusion parses the comma- or space-separated form a TF_VAR_ line carries,
# and an exclusion that names no listed project warns rather than silently
# excluding nothing. Providers mocked and the cluster module's outputs fixed, as
# in gitops_forge.tftest.hcl; nothing here reaches a cloud.

mock_provider "google" {
  mock_resource "google_service_account" {
    defaults = {
      name   = "projects/tftest-project/serviceAccounts/mock-service-account@tftest-project.iam.gserviceaccount.com"
      email  = "mock-service-account@tftest-project.iam.gserviceaccount.com"
      member = "serviceAccount:mock-service-account@tftest-project.iam.gserviceaccount.com"
    }
  }
}
mock_provider "google-beta" {}
mock_provider "helm" {}
mock_provider "http" {}
mock_provider "time" {}
mock_provider "random" {
  mock_resource "random_password" {
    defaults = { result = "mock-password" }
  }
}
mock_provider "tls" {
  mock_resource "tls_private_key" {
    defaults = { private_key_openssh = "mock-private-key", public_key_openssh = "mock-public-key" }
  }
}

override_module {
  target = module.gke_cluster
  outputs = {
    cluster_endpoint        = "10.0.0.1"
    cluster_endpoint_is_dns = false
    cluster_ca_certificate  = "Y2E="
    cluster_location        = "us-central1"
    cluster_name            = "c"
    network_policy_enforced = true
    workload_identity_pool  = "tftest-project.svc.id.goog"
  }
}

variables {
  project_id     = "tftest-project"
  cluster_name   = "c"
  location       = "us-central1"
  api_server_key = "k"
  scope = {
    projects = ["payments-prod", "ops-mgmt"]
  }
}

run "the_declared_projects_less_the_host_get_a_sink" {
  command = plan
  assert {
    condition     = jsonencode(local.drift_pubsub_source_projects) == jsonencode(["ops-mgmt", "payments-prod"])
    error_message = "the export list must be scope.projects less the host, sorted: ${jsonencode(local.drift_pubsub_source_projects)}"
  }
  assert {
    condition     = jsonencode(module.kube_agents_iam.scope_export_projects) == jsonencode(["ops-mgmt", "payments-prod"])
    error_message = "scope_export_projects: ${jsonencode(module.kube_agents_iam.scope_export_projects)}"
  }
}

run "an_excluded_project_stays_in_the_scope_and_out_of_the_export" {
  command = plan
  variables {
    # The form a TF_VAR_ line carries: commas, spaces, or both.
    drift_pubsub_source_exclude_projects = " payments-prod ,  "
  }
  assert {
    condition     = jsonencode(local.drift_pubsub_source_projects) == jsonencode(["ops-mgmt"])
    error_message = "the exclusion must drop payments-prod from the export alone: ${jsonencode(local.drift_pubsub_source_projects)}"
  }
  assert {
    condition     = contains(module.kube_agents_iam.scope_export_projects, "payments-prod")
    error_message = "the excluded project must stay in the scope's own listing; the exclusion is for the export only"
  }
}

run "a_glob_in_the_scopes_own_exclusions_drops_the_project_from_the_export" {
  command = plan
  variables {
    scope = {
      projects = ["payments-prod", "ops-mgmt", "ops-mgmt-2"]
      exclude  = { projects = ["*-prod", "ops-mgmt-?"] }
    }
  }
  assert {
    condition     = jsonencode(local.drift_pubsub_source_projects) == jsonencode(["ops-mgmt"])
    error_message = "a project a scope.exclude.projects glob matches is dropped by the reconcile, so it gets no sink either: ${jsonencode(local.drift_pubsub_source_projects)}"
  }
  assert {
    condition     = contains(module.kube_agents_iam.scope_export_projects, "payments-prod")
    error_message = "the glob is matched for the export alone; the IAM module's listing leaves a glob to the reconcile"
  }
}

run "bracket_classes_an_exact_entry_and_the_operator_exclusion_combine" {
  command = plan
  variables {
    scope = {
      projects = ["pay-prod", "ops-mgmt", "ops-mgmt-2", "lab-one", "lab-two", "lab-six"]
      exclude  = { projects = ["ops-mgmt-2", "lab-[ot]*", "[!lo]*"] }
    }
    drift_pubsub_source_exclude_projects = "ops-mgmt"
  }
  assert {
    condition     = jsonencode(local.drift_pubsub_source_projects) == jsonencode(["lab-six"])
    error_message = "an exact entry, a [seq] class, a [!seq] class and the operator's exclusion must each take their project out of the export: ${jsonencode(local.drift_pubsub_source_projects)}"
  }
  assert {
    condition     = !contains(module.kube_agents_iam.scope_export_projects, "ops-mgmt-2") && contains(module.kube_agents_iam.scope_export_projects, "lab-one")
    error_message = "the IAM module's listing leaves out the exact entry and leaves a glob to the reconcile: ${jsonencode(module.kube_agents_iam.scope_export_projects)}"
  }
}

run "an_entry_the_plan_cannot_match_warns_and_keeps_the_sink" {
  command = plan
  variables {
    scope = {
      projects = ["ops-alpha", "ops-mgmt"]
      exclude  = { projects = ["ops-[!z-a]*"] }
    }
  }
  expect_failures = [check.drift_scope_exclude_globs_compile]
  assert {
    condition     = jsonencode(local.drift_pubsub_source_projects) == jsonencode(["ops-alpha", "ops-mgmt"])
    error_message = "a reversed range is a pattern RE2 rejects; the plan keeps both sinks and the check says so: ${jsonencode(local.drift_pubsub_source_projects)}"
  }
}

run "a_space_separated_exclusion_parses_too" {
  command = plan
  variables {
    drift_pubsub_source_exclude_projects = "payments-prod ops-mgmt"
  }
  assert {
    condition     = length(local.drift_pubsub_source_projects) == 0
    error_message = "both entries must be read: ${jsonencode(local.drift_pubsub_source_projects)}"
  }
}

run "an_exclusion_that_names_no_listed_project_warns" {
  command = plan
  variables {
    drift_pubsub_source_exclude_projects = "paymnts-prod"
  }
  expect_failures = [check.drift_source_exclusions_name_listed_projects]
  assert {
    condition     = jsonencode(local.drift_pubsub_source_projects) == jsonencode(["ops-mgmt", "payments-prod"])
    error_message = "a misspelled exclusion excludes nothing, which is what the check has to say out loud"
  }
}

run "an_exclusion_that_is_not_a_project_id_is_refused" {
  command = plan
  variables {
    drift_pubsub_source_exclude_projects = "projects/payments-prod"
  }
  expect_failures = [var.drift_pubsub_source_exclude_projects]
}
