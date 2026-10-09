# The GitOps forge: a GitHub install renders the deprecated github alias and
# nothing else, a GitLab install renders the forges/repositories lists with a
# credentialsRef and never the alias, and the plan refuses a GitLab install that
# also asks for the GitHub token minter. The providers are mocked and the
# cluster module's outputs fixed, so nothing here reaches a cloud.

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

run "github_with_no_repo_renders_no_integration" {
  command = plan
  assert {
    condition     = length(local.platform_agent_integration) == 0
    error_message = "integration: ${jsonencode(local.platform_agent_integration)}"
  }
}

run "github_renders_the_alias" {
  command = plan
  variables {
    github_repo = "acme/infra"
  }
  assert {
    condition     = jsonencode(local.platform_agent_integration) == jsonencode({ github = { gitRepo = "acme/infra", org = "acme" } })
    error_message = "integration: ${jsonencode(local.platform_agent_integration)}"
  }
}

run "gitlab_renders_the_lists" {
  command = plan
  variables {
    gitops_forge             = "gitlab"
    gitops_host              = "gitlab.example.com"
    gitlab_repo              = "platform/infra/gitops"
    gitlab_token_secret_name = "gl-token"
  }
  assert {
    condition = local.gitlab_forges == [{
      name = "gitlab", provider = "gitlab", host = "gitlab.example.com", credentialsRef = { name = "gl-token" }
    }]
    error_message = "gitlab forge: ${jsonencode(local.gitlab_forges)}"
  }
  assert {
    condition = local.gitlab_repositories == [{
      forge = "gitlab", repository = "platform/infra/gitops", role = "gitops"
    }]
    error_message = "gitlab repository: ${jsonencode(local.gitlab_repositories)}"
  }
  assert {
    condition     = keys(local.platform_agent_integration) == ["forges", "repositories"]
    error_message = "integration keys: ${jsonencode(keys(local.platform_agent_integration))}"
  }
}

run "gitlab_on_gitlab_com_names_no_host" {
  command = plan
  variables {
    gitops_forge = "gitlab"
    gitlab_repo  = "g/p"
  }
  assert {
    condition     = !contains(keys(local.gitlab_forges[0]), "host")
    error_message = "gitlab.com forge carries a host: ${jsonencode(local.gitlab_forges)}"
  }
}

run "gitlab_with_a_private_ca_names_its_secret" {
  command = plan
  variables {
    gitops_forge          = "gitlab"
    gitops_host           = "gitlab.example.com"
    gitlab_repo           = "platform/infra/gitops"
    gitlab_ca_secret_name = "gitlab-forge-ca"
  }
  assert {
    condition = local.gitlab_forges == [{
      name           = "gitlab", provider = "gitlab", host = "gitlab.example.com",
      credentialsRef = { name = "gitlab-forge-token" }, caBundleRef = { name = "gitlab-forge-ca" }
    }]
    error_message = "gitlab forge: ${jsonencode(local.gitlab_forges)}"
  }
}

run "gitlab_without_a_ca_names_none" {
  command = plan
  variables {
    gitops_forge = "gitlab"
    gitlab_repo  = "g/p"
  }
  assert {
    condition     = !contains(keys(local.gitlab_forges[0]), "caBundleRef")
    error_message = "a forge with no CA carries caBundleRef: ${jsonencode(local.gitlab_forges)}"
  }
}

run "a_gitlab_ca_needs_the_gitlab_forge" {
  command = plan
  variables {
    gitlab_ca_secret_name = "gitlab-forge-ca"
  }
  expect_failures = [helm_release.kube_agents]
}

run "a_gitlab_ca_needs_a_self_managed_host" {
  command = plan
  variables {
    gitops_forge          = "gitlab"
    gitlab_repo           = "g/p"
    gitlab_ca_secret_name = "gitlab-forge-ca"
  }
  expect_failures = [helm_release.kube_agents]
}

run "a_gitlab_ca_is_refused_for_gitlab_com_by_name" {
  command = plan
  variables {
    gitops_forge          = "gitlab"
    gitops_host           = "gitlab.com"
    gitlab_repo           = "g/p"
    gitlab_ca_secret_name = "gitlab-forge-ca"
  }
  expect_failures = [helm_release.kube_agents]
}

run "the_ca_secret_name_is_a_kubernetes_name" {
  command = plan
  variables {
    gitops_forge          = "gitlab"
    gitops_host           = "gitlab.example.com"
    gitlab_repo           = "g/p"
    gitlab_ca_secret_name = "Not_A_Name"
  }
  expect_failures = [var.gitlab_ca_secret_name]
}

run "gitlab_refuses_a_minter" {
  command = plan
  variables {
    gitops_forge         = "gitlab"
    gitlab_repo          = "g/p"
    enable_github_minter = true
    github_repo          = "acme/infra"
    github_app_id        = "1"
  }
  expect_failures = [helm_release.kube_agents]
}

run "gitlab_needs_a_repo" {
  command = plan
  variables {
    gitops_forge = "gitlab"
  }
  expect_failures = [helm_release.kube_agents]
}

run "gitlab_inputs_need_the_gitlab_forge" {
  command = plan
  variables {
    gitlab_repo = "g/p"
  }
  expect_failures = [helm_release.kube_agents]
}

run "a_gitlab_host_needs_the_gitlab_forge" {
  command = plan
  variables {
    gitops_host = "gitlab.example.com"
  }
  expect_failures = [helm_release.kube_agents]
}

run "the_forge_is_github_or_gitlab" {
  command = plan
  variables {
    gitops_forge = "bitbucket"
  }
  expect_failures = [var.gitops_forge]
}

run "the_host_is_bare" {
  command = plan
  variables {
    gitops_forge = "gitlab"
    gitlab_repo  = "g/p"
    gitops_host  = "https://gitlab.example.com"
  }
  expect_failures = [var.gitops_host]
}

# The release values a GitHub install renders, byte for byte as the composition
# rendered them before GitLab support (testdata/*.values.golden, rendered by
# these same runs at that commit).
run "github_values_are_unchanged" {
  command = apply
  variables {
    github_repo = "acme/infra"
  }
  assert {
    condition     = nonsensitive(helm_release.kube_agents.values[0]) == file("tests/testdata/github.values.golden")
    error_message = "a GitHub install's release values changed"
  }
}

run "github_minter_values_are_unchanged" {
  command = apply
  variables {
    github_repo          = "acme/infra"
    enable_github_minter = true
    github_app_id        = "123"
  }
  assert {
    condition     = nonsensitive(helm_release.kube_agents.values[0]) == file("tests/testdata/github_minter.values.golden")
    error_message = "a GitHub minter install's release values changed"
  }
}

run "gitlab_release_values" {
  command = apply
  variables {
    gitops_forge             = "gitlab"
    gitops_host              = "gitlab.example.com"
    gitlab_repo              = "platform/infra/gitops"
    gitlab_token_secret_name = "gl-token"
  }
  assert {
    condition = jsonencode(yamldecode(nonsensitive(helm_release.kube_agents.values[0])).platformAgent.integration) == jsonencode({
      forges       = [{ credentialsRef = { name = "gl-token" }, host = "gitlab.example.com", name = "gitlab", provider = "gitlab" }]
      repositories = [{ forge = "gitlab", repository = "platform/infra/gitops", role = "gitops" }]
    })
    error_message = "integration: ${jsonencode(yamldecode(nonsensitive(helm_release.kube_agents.values[0])).platformAgent.integration)}"
  }
  assert {
    condition     = yamldecode(nonsensitive(helm_release.kube_agents.values[0])).githubMinter.enabled == false
    error_message = "a GitLab install enabled the GitHub minter"
  }
}
