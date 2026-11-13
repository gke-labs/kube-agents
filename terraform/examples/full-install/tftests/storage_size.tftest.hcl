# The data volume size: the default passes the chart no platformAgent.storage
# key, a raised size reaches the release values as platformAgent.storage.size
# beside the mode, and the variable refuses a value that is not a whole number
# of Mi, Gi or Ti above zero and at most 64Ti. Providers mocked and the cluster
# module's outputs fixed, as in alert_daily_limits.tftest.hcl; nothing here
# reaches a cloud.

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
}

run "default_passes_no_storage" {
  command = apply
  assert {
    condition     = !contains(keys(yamldecode(nonsensitive(helm_release.kube_agents.values[0])).platformAgent), "storage")
    error_message = "platformAgent keys: ${jsonencode(keys(yamldecode(nonsensitive(helm_release.kube_agents.values[0])).platformAgent))}"
  }
}

run "the_default_written_out_passes_no_storage" {
  command = apply
  variables {
    platform_agent_storage_size = "50Gi"
  }
  assert {
    condition     = !contains(keys(yamldecode(nonsensitive(helm_release.kube_agents.values[0])).platformAgent), "storage")
    error_message = "platformAgent keys: ${jsonencode(keys(yamldecode(nonsensitive(helm_release.kube_agents.values[0])).platformAgent))}"
  }
}

run "a_raised_size_is_passed_beside_the_mode" {
  command = apply
  variables {
    platform_agent_storage_size = "80Gi"
    platform_agent_mode         = "next"
  }
  assert {
    condition     = jsonencode(yamldecode(nonsensitive(helm_release.kube_agents.values[0])).platformAgent.storage) == jsonencode({ size = "80Gi" })
    error_message = "storage: ${jsonencode(yamldecode(nonsensitive(helm_release.kube_agents.values[0])).platformAgent.storage)}"
  }
  assert {
    condition     = yamldecode(nonsensitive(helm_release.kube_agents.values[0])).platformAgent.mode == "next"
    error_message = "the storage operand replaced the mode"
  }
}

run "a_zero_size_is_refused" {
  command = plan
  variables {
    platform_agent_storage_size = "0Gi"
  }
  expect_failures = [var.platform_agent_storage_size]
}

run "a_size_without_a_binary_unit_is_refused" {
  command = plan
  variables {
    platform_agent_storage_size = "80G"
  }
  expect_failures = [var.platform_agent_storage_size]
}

run "a_fractional_size_is_refused" {
  command = plan
  variables {
    platform_agent_storage_size = "1.5Ti"
  }
  expect_failures = [var.platform_agent_storage_size]
}

run "the_largest_disk_is_allowed" {
  command = plan
  variables {
    platform_agent_storage_size = "64Ti"
  }
}

run "a_size_above_64Ti_is_refused" {
  command = plan
  variables {
    platform_agent_storage_size = "65537Gi"
  }
  expect_failures = [var.platform_agent_storage_size]
}
