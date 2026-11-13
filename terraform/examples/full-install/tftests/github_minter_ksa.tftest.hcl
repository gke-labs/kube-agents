# The token minter's KSA (#5282): github_minter_ksa_name names both the KSA
# half of the minter GSA's Workload Identity binding and the KSA the chart runs
# the minter as, so the two cannot differ; the default leaves an existing
# install's name where it was, and the variable refuses a name that isn't a
# DNS-1123 label. Providers mocked and the cluster module's outputs fixed, as
# in storage_size.tftest.hcl; nothing here reaches a cloud.

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

  github_repo          = "acme/infra"
  enable_github_minter = true
  github_app_id        = "123"
}

run "the_default_keeps_the_name_existing_installs_run_as" {
  command = apply
  assert {
    condition     = module.github_minter[0].workload_identity_member == "serviceAccount:tftest-project.svc.id.goog[kubeagents-system/kubeagents-github-minter]"
    error_message = "binding member: ${module.github_minter[0].workload_identity_member}"
  }
}

run "one_name_reaches_the_binding_and_the_pod" {
  command = apply
  variables {
    github_minter_ksa_name = "ka-today-1-github-minter"
  }
  assert {
    condition     = module.github_minter[0].workload_identity_member == "serviceAccount:tftest-project.svc.id.goog[kubeagents-system/ka-today-1-github-minter]"
    error_message = "binding member: ${module.github_minter[0].workload_identity_member}"
  }
  assert {
    condition     = yamldecode(nonsensitive(helm_release.kube_agents.values[0])).githubMinter.ksaName == "ka-today-1-github-minter"
    error_message = "chart githubMinter.ksaName differs from the binding's KSA"
  }
}

run "a_name_that_is_not_a_label_is_refused" {
  command = plan
  variables {
    github_minter_ksa_name = "Minter_KSA"
  }
  expect_failures = [var.github_minter_ksa_name]
}
