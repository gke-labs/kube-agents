# Sovereign universe_domain, sovereign model-egress consent gate, and Gitea
# GitOps forge wiring in the full-install root composition.

mock_provider "google" {
  mock_resource "google_service_account" {
    defaults = {
      name   = "projects/part0:sample-project/serviceAccounts/mock-sa@sample-project.part0.iam.gserviceaccount.com"
      email  = "mock-sa@sample-project.part0.iam.gserviceaccount.com"
      member = "serviceAccount:mock-sa@sample-project.part0.iam.gserviceaccount.com"
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
    cluster_endpoint        = "gke-1234abcd.gke.universe.example.test"
    cluster_endpoint_is_dns = true
    cluster_ca_certificate  = "Y2E="
    cluster_location        = "u-region-north1"
    cluster_name            = "sovereign-host"
    network_policy_enforced = true
    workload_identity_pool  = "sample-project.part0.svc.id.goog"
  }
}

variables {
  project_id     = "part0:sample-project"
  cluster_name   = "sovereign-host"
  location       = "u-region-north1"
  api_server_key = "k"
}

run "custom_universe_with_openai_compatible_omits_commercial_only_apis" {
  command = plan

  variables {
    universe_domain               = "universe.example.test"
    vpc_network                   = "platform-net"
    vpc_subnetwork                = "platform-subnet"
    model_provider                = "openai_compatible"
    model_default_name            = "local-qwen"
    openai_compatible_api_base    = "http://vllm.models.svc.cluster.local/v1"
    openai_compatible_target_port = 8000
  }

  assert {
    condition     = local.custom_universe == true && local.effective_universe_domain == "universe.example.test"
    error_message = "custom_universe must be true for universe.example.test"
  }

  assert {
    condition     = !contains(keys(google_project_service.required), "cloudtrace.googleapis.com") && !contains(keys(google_project_service.required), "gkehub.googleapis.com") && !contains(keys(google_project_service.required), "gkebackup.googleapis.com") && !contains(keys(google_project_service.required), "developerknowledge.googleapis.com")
    error_message = "commercial-only APIs must be withheld in custom universes: ${jsonencode(keys(google_project_service.required))}"
  }

  assert {
    condition     = !contains(local.agent_project_roles, "roles/gkehub.viewer") && !contains(local.agent_project_roles, "roles/cloudtrace.user") && !contains(local.agent_project_roles, "roles/mcp.toolUser")
    error_message = "commercial-only IAM roles must be withheld in custom universes: ${jsonencode(local.agent_project_roles)}"
  }
}

run "custom_universe_refuses_external_saas_model_without_consent" {
  command = plan

  variables {
    universe_domain = "universe.example.test"
    model_provider  = "gemini"
  }

  expect_failures = [
    helm_release.kube_agents,
  ]
}

run "custom_universe_allows_external_saas_model_with_explicit_consent" {
  command = plan

  variables {
    universe_domain             = "universe.example.test"
    model_provider              = "gemini"
    allow_external_model_egress = true
  }

  assert {
    condition     = local.custom_universe == true
    error_message = "plan with allow_external_model_egress = true must succeed"
  }
}

run "gitea_renders_forges_and_repositories_with_ca_and_no_minter" {
  command = plan

  variables {
    gitops_forge          = "gitea"
    gitops_host           = "gitea.example.test"
    gitops_repo_path      = "platform/gitops"
    gitops_ca_secret_name = "gitea-forge-ca"
  }

  assert {
    condition = local.gitea_forges == [{
      name           = "gitea"
      provider       = "gitea"
      host           = "gitea.example.test"
      namespace      = "platform"
      credentialsRef = { name = "gitea-forge-token" }
      caBundleRef    = { name = "gitea-forge-ca" }
    }]
    error_message = "gitea_forges mismatch: ${jsonencode(local.gitea_forges)}"
  }

  assert {
    condition = local.gitea_repositories == [{
      forge      = "gitea"
      repository = "gitops"
      role       = "gitops"
    }]
    error_message = "gitea_repositories mismatch: ${jsonencode(local.gitea_repositories)}"
  }

  assert {
    condition     = keys(local.platform_agent_integration) == ["forges", "repositories"]
    error_message = "integration keys: ${jsonencode(keys(local.platform_agent_integration))}"
  }
}

run "gitea_refuses_missing_host_or_hosted_domain_or_identity" {
  command = plan

  variables {
    gitops_forge     = "gitea"
    gitops_host      = "github.com"
    gitops_repo_path = "platform/gitops"
  }

  expect_failures = [
    helm_release.kube_agents,
  ]
}

run "gitea_refuses_host_as_owner_and_dash_or_double_git_slug" {
  command = plan

  variables {
    gitops_forge     = "gitea"
    gitops_host      = "gitea.corp.internal"
    gitops_repo_path = "github.com/platform-gitops"
  }

  expect_failures = [
    helm_release.kube_agents,
  ]
}

run "manage_iam_false_uses_derived_partitioned_gsa_without_creating_iam_resources" {
  command = plan

  variables {
    universe_domain             = "universe.example.test"
    allow_external_model_egress = true
    manage_iam                  = false
    create_cluster              = false
    enable_drift_detector       = false
  }

  assert {
    condition     = module.kube_agents_iam.service_account_email == "kubeagents-platform-gsa@sample-project.part0.iam.gserviceaccount.com"
    error_message = "expected derived partitioned GSA email when manage_iam = false, got ${module.kube_agents_iam.service_account_email}"
  }
}

run "manage_iam_false_with_vertex_ai_derives_litellm_gsa_without_creating_iam_resources" {
  command = plan

  variables {
    universe_domain       = "universe.example.test"
    model_provider        = "vertex_ai"
    manage_iam            = false
    create_cluster        = false
    enable_drift_detector = false
  }

  assert {
    condition     = module.litellm_vertex_iam[0].service_account_email == "kubeagents-litellm-gsa@sample-project.part0.iam.gserviceaccount.com"
    error_message = "expected derived partitioned LiteLLM GSA email when manage_iam = false, got ${module.litellm_vertex_iam[0].service_account_email}"
  }

  assert {
    condition     = length(google_project_iam_member.litellm_vertex_user) == 0
    error_message = "expected zero litellm_vertex_user IAM bindings when manage_iam = false"
  }
}

run "manage_iam_false_refuses_default_drift_detector_and_other_iam_features_at_plan" {
  command = plan

  variables {
    universe_domain             = "universe.example.test"
    allow_external_model_egress = true
    manage_iam                  = false
  }

  expect_failures = [
    helm_release.kube_agents,
  ]
}

run "manage_iam_false_refuses_google_chat_minter_and_stockout_at_plan" {
  command = plan

  variables {
    universe_domain             = "universe.example.test"
    allow_external_model_egress = true
    manage_iam                  = false
    create_cluster              = false
    enable_drift_detector       = false
    enable_google_chat          = true
  }

  expect_failures = [
    helm_release.kube_agents,
  ]
}
