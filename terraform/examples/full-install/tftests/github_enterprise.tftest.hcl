# A GitHub Enterprise Server as the GitOps forge: the github forge with
# gitops_host renders the forges/repositories lists at that host, with the
# owner as the namespace, caBundleRef when a CA Secret is named, and github =
# null in place of the alias. The plan refuses a host that GitHub operates, a
# CA without a host, and a repository that is not owner/name. The github.com
# renderings stay byte for byte in gitops_forge.tftest.hcl. The providers are
# mocked and the cluster module's outputs fixed, so nothing here reaches a
# cloud.

# Fixed where the mock would otherwise generate a value per run, so the
# rendered release values can be compared byte for byte.
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

run "ghes_renders_the_lists" {
  command = plan
  variables {
    gitops_host           = "github.acme.corp"
    github_repo           = "platform/infra"
    github_ca_secret_name = "github-forge-ca"
  }
  assert {
    condition = local.ghes_forges == [{
      name        = "github", provider = "github", host = "github.acme.corp", namespace = "platform",
      caBundleRef = { name = "github-forge-ca" }
    }]
    error_message = "github forge: ${jsonencode(local.ghes_forges)}"
  }
  assert {
    condition     = local.ghes_repositories == [{ forge = "github", repository = "infra", role = "gitops" }]
    error_message = "github repository: ${jsonencode(local.ghes_repositories)}"
  }
  assert {
    condition     = keys(local.platform_agent_integration) == ["forges", "github", "repositories"]
    error_message = "integration keys: ${jsonencode(keys(local.platform_agent_integration))}"
  }
  assert {
    condition     = local.platform_agent_integration.github == null
    error_message = "the alias is not removed: ${jsonencode(local.platform_agent_integration)}"
  }
}

run "ghes_without_a_ca_names_none" {
  command = plan
  variables {
    gitops_host = "github.acme.corp"
    github_repo = "platform/infra"
  }
  assert {
    condition     = !contains(keys(local.ghes_forges[0]), "caBundleRef")
    error_message = "a forge with no CA carries caBundleRef: ${jsonencode(local.ghes_forges)}"
  }
}

run "github_com_renders_no_ghes_forge" {
  command = plan
  variables {
    github_repo = "acme/infra"
  }
  assert {
    condition     = !local.gitops_is_ghes && !contains(keys(local.platform_agent_integration), "forges")
    error_message = "integration: ${jsonencode(local.platform_agent_integration)}"
  }
}

run "a_github_ca_needs_a_ghes_host" {
  command = plan
  variables {
    github_repo           = "acme/infra"
    github_ca_secret_name = "github-forge-ca"
  }
  expect_failures = [helm_release.kube_agents]
}

run "a_github_ca_is_not_for_gitlab" {
  command = plan
  variables {
    gitops_forge          = "gitlab"
    gitops_host           = "gitlab.example.com"
    gitlab_repo           = "g/p"
    github_ca_secret_name = "github-forge-ca"
  }
  expect_failures = [helm_release.kube_agents]
}

run "the_github_ca_secret_name_is_a_kubernetes_name" {
  command = plan
  variables {
    gitops_host           = "github.acme.corp"
    github_repo           = "platform/infra"
    github_ca_secret_name = "Not_A_Name"
  }
  expect_failures = [var.github_ca_secret_name]
}

run "a_host_github_operates_is_refused" {
  command = plan
  variables {
    gitops_host = "acme.ghe.com"
    github_repo = "platform/infra"
  }
  expect_failures = [helm_release.kube_agents]
}

run "github_com_as_a_host_is_refused" {
  command = plan
  variables {
    gitops_host = "github.com"
    github_repo = "platform/infra"
  }
  expect_failures = [helm_release.kube_agents]
}

run "a_gitlab_host_is_not_a_ghes" {
  command = plan
  variables {
    gitops_host = "gitlab.com"
    github_repo = "platform/infra"
  }
  expect_failures = [helm_release.kube_agents]
}

run "ghes_needs_owner_and_name" {
  command = plan
  variables {
    gitops_host = "github.acme.corp"
    github_repo = "https://github.acme.corp/platform/infra"
  }
  expect_failures = [helm_release.kube_agents]
}

run "ghes_needs_a_repo" {
  command = plan
  variables {
    gitops_host = "github.acme.corp"
  }
  expect_failures = [helm_release.kube_agents]
}

run "ghes_release_values" {
  command = apply
  variables {
    gitops_host           = "github.acme.corp"
    github_repo           = "platform/infra"
    github_ca_secret_name = "github-forge-ca"
    enable_github_minter  = true
    github_app_id         = "12"
  }
  assert {
    condition = jsonencode(yamldecode(nonsensitive(helm_release.kube_agents.values[0])).platformAgent.integration) == jsonencode({
      forges = [{
        caBundleRef = { name = "github-forge-ca" }, host = "github.acme.corp", name = "github", namespace = "platform", provider = "github"
      }]
      github       = null
      repositories = [{ forge = "github", repository = "infra", role = "gitops" }]
    })
    error_message = "integration: ${jsonencode(yamldecode(nonsensitive(helm_release.kube_agents.values[0])).platformAgent.integration)}"
  }
  assert {
    condition     = yamldecode(nonsensitive(helm_release.kube_agents.values[0])).githubMinter.enabled == true
    error_message = "a GitHub Enterprise Server install lost its minter"
  }
  assert {
    condition     = yamldecode(nonsensitive(helm_release.kube_agents.values[0])).githubMinter.org == "platform" && yamldecode(nonsensitive(helm_release.kube_agents.values[0])).githubMinter.repo == "infra"
    error_message = "minter scope: ${jsonencode(yamldecode(nonsensitive(helm_release.kube_agents.values[0])).githubMinter)}"
  }
}
