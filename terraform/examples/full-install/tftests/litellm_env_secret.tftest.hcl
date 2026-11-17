# litellm_env_secret_name: the chart's litellm.envFromSecret, rendered only when
# named; model_provider = "sap" and any other LiteLLM provider slug accepted; a
# slug outside the chart's table refused without model_default_name; a bad
# Secret name refused. Providers mocked and the cluster module's outputs fixed,
# as in openai_compatible.tftest.hcl; nothing here reaches a cloud.

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

run "sap_with_the_secret_renders_env_from_secret" {
  command = apply
  variables {
    model_provider          = "sap"
    litellm_env_secret_name = "sap-aicore"
  }
  assert {
    condition = jsonencode(yamldecode(nonsensitive(helm_release.kube_agents.values[0])).litellm) == jsonencode({
      envFromSecret    = "sap-aicore"
      maxTokens        = 0
      modelDefaultName = ""
      modelProvider    = "sap"
    })
    error_message = "litellm: ${jsonencode(yamldecode(nonsensitive(helm_release.kube_agents.values[0])).litellm)}"
  }
}

run "another_slug_with_a_model_renders" {
  command = apply
  variables {
    model_provider          = "azure"
    model_default_name      = "gpt-5.4"
    litellm_env_secret_name = "azure-openai"
  }
  assert {
    condition     = yamldecode(nonsensitive(helm_release.kube_agents.values[0])).litellm.modelProvider == "azure"
    error_message = "litellm: ${jsonencode(yamldecode(nonsensitive(helm_release.kube_agents.values[0])).litellm)}"
  }
}

run "openai_compatible_carries_the_secret_beside_its_own_values" {
  command = apply
  variables {
    model_provider             = "openai_compatible"
    model_default_name         = "some-org/some-model"
    openai_compatible_api_base = "https://ai-gateway.example.com/v1"
    litellm_env_secret_name    = "ai-gateway-oauth"
  }
  assert {
    condition     = yamldecode(nonsensitive(helm_release.kube_agents.values[0])).litellm.envFromSecret == "ai-gateway-oauth"
    error_message = "litellm: ${jsonencode(yamldecode(nonsensitive(helm_release.kube_agents.values[0])).litellm)}"
  }
}

# Unset, no envFromSecret key at all, so an install without one renders the
# values it did (the *.values.golden files in gitops_forge hold that byte for
# byte).
run "unset_renders_no_env_from_secret_key" {
  command = apply
  assert {
    condition     = !contains(keys(yamldecode(nonsensitive(helm_release.kube_agents.values[0])).litellm), "envFromSecret")
    error_message = "litellm: ${jsonencode(yamldecode(nonsensitive(helm_release.kube_agents.values[0])).litellm)}"
  }
}

run "a_slug_without_a_model_fails_the_plan" {
  command = plan
  variables {
    model_provider          = "azure"
    litellm_env_secret_name = "azure-openai"
  }
  expect_failures = [helm_release.kube_agents]
}

run "a_provider_that_is_not_a_slug_fails_the_plan" {
  command = plan
  variables {
    model_provider = "v-llm"
  }
  expect_failures = [var.model_provider]
}

run "chatgpt_fails_the_plan_as_the_chart_would" {
  command = plan
  variables {
    model_provider     = "chatgpt"
    model_default_name = "gpt-5.4"
  }
  expect_failures = [var.model_provider]
}

run "a_bad_secret_name_fails_the_plan" {
  command = plan
  variables {
    litellm_env_secret_name = "Not_A_Secret"
  }
  expect_failures = [var.litellm_env_secret_name]
}
