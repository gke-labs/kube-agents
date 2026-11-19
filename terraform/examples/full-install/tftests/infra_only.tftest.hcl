# install_helm_releases = false: the Google Cloud side only. Neither Helm
# release is planned, the outputs a separate pipeline installs the charts from
# carry exactly what the releases are given when Terraform installs them, the
# checks the chart's values must pass still refuse a plan, and a default install
# still installs both releases. Providers mocked and the cluster module's
# outputs fixed, as in gitops_forge.tftest.hcl; nothing here reaches a cloud.

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

run "a_default_install_installs_both_releases_from_the_outputs" {
  command = apply
  assert {
    condition     = length(helm_release.kube_agents) == 1 && length(helm_release.cert_manager) == 1
    error_message = "a default install must install both releases"
  }
  assert {
    condition     = nonsensitive(jsonencode(helm_release.kube_agents[0].values)) == nonsensitive(jsonencode(output.kube_agents_helm_values))
    error_message = "the release must be given exactly the documents kube_agents_helm_values carries"
  }
  assert {
    condition     = jsonencode(helm_release.cert_manager[0].values) == jsonencode(output.cert_manager_helm_release.values)
    error_message = "cert-manager must be given exactly the documents cert_manager_helm_release carries"
  }
  assert {
    condition = jsonencode(output.kube_agents_helm_release) == jsonencode({
      name      = helm_release.kube_agents[0].name
      namespace = helm_release.kube_agents[0].namespace
      chart     = "charts/kube-agents"
      version   = helm_release.kube_agents[0].version
    })
    error_message = "kube_agents_helm_release: ${jsonencode(output.kube_agents_helm_release)}"
  }
}

run "infra_only_plans_no_release_and_hands_on_the_same_values" {
  command = apply
  variables {
    install_helm_releases = false
  }
  assert {
    condition     = length(helm_release.kube_agents) == 0 && length(helm_release.cert_manager) == 0
    error_message = "install_helm_releases = false must plan neither release"
  }
  assert {
    condition     = nonsensitive(jsonencode(output.kube_agents_helm_values)) == nonsensitive(jsonencode(run.a_default_install_installs_both_releases_from_the_outputs.kube_agents_helm_values))
    error_message = "the values handed on must be the ones a default install gives the release"
  }
  assert {
    condition     = jsonencode(output.cert_manager_helm_release) == jsonencode(run.a_default_install_installs_both_releases_from_the_outputs.cert_manager_helm_release)
    error_message = "the cert-manager release handed on must be the one a default install installs"
  }
  assert {
    condition     = output.agent_service_account_email != ""
    error_message = "the agent's service account must still be created and output"
  }
}

run "infra_only_without_cert_manager_hands_on_no_cert_manager_release" {
  command = plan
  variables {
    install_helm_releases = false
    enable_cert_manager   = false
  }
  assert {
    condition     = output.cert_manager_helm_release == null
    error_message = "cert_manager_helm_release must be null when enable_cert_manager is false"
  }
}

# The checks the chart's values must pass sit on the values output, so an
# infra-only plan refuses what the chart would refuse, as a default one does.
run "infra_only_still_refuses_values_the_chart_would_refuse" {
  command = plan
  variables {
    install_helm_releases = false
    model_provider        = "openai_compatible"
  }
  expect_failures = [output.kube_agents_helm_values]
}
