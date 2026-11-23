# The runtime sizing profile (platform_agent_sizing_profile): default "auto"
# passes platformAgent.deployment.sizingProfile = "auto" to the chart,
# explicit tiers pass through, empty "" passes nothing, and unknown profiles are
# refused. Providers mocked and the cluster module's outputs fixed, as in
# storage_size.tftest.hcl.

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

run "default_passes_auto_sizing_profile" {
  command = apply
  assert {
    condition     = yamldecode(nonsensitive(helm_release.kube_agents[0].values[0])).platformAgent.deployment.sizingProfile == "auto"
    error_message = "deployment: ${jsonencode(yamldecode(nonsensitive(helm_release.kube_agents[0].values[0])).platformAgent.deployment)}"
  }
}

run "explicit_fleet_profile_is_passed" {
  command = apply
  variables {
    platform_agent_sizing_profile = "fleet"
  }
  assert {
    condition     = yamldecode(nonsensitive(helm_release.kube_agents[0].values[0])).platformAgent.deployment.sizingProfile == "fleet"
    error_message = "deployment: ${jsonencode(yamldecode(nonsensitive(helm_release.kube_agents[0].values[0])).platformAgent.deployment)}"
  }
}

run "empty_profile_omits_sizing_profile" {
  command = apply
  variables {
    platform_agent_sizing_profile = ""
  }
  assert {
    condition     = !contains(keys(yamldecode(nonsensitive(helm_release.kube_agents[0].values[0])).platformAgent.deployment), "sizingProfile")
    error_message = "deployment keys: ${jsonencode(keys(yamldecode(nonsensitive(helm_release.kube_agents[0].values[0])).platformAgent.deployment))}"
  }
}

run "invalid_sizing_profile_is_refused" {
  command = plan
  variables {
    platform_agent_sizing_profile = "large"
  }
  expect_failures = [var.platform_agent_sizing_profile]
}
