# litellm_budget: the chart's litellm.budget, rendered only while usd is above
# 0; the period checked only then; a negative amount and a period that is not
# a LiteLLM duration refused at the plan. Providers mocked and the cluster
# module's outputs fixed, as in litellm_env_secret.tftest.hcl; nothing here
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

run "a_budget_renders_litellm_budget_with_its_period" {
  command = apply
  variables {
    litellm_budget = { usd = 25, period = "7d" }
  }
  assert {
    condition = jsonencode(yamldecode(nonsensitive(helm_release.kube_agents.values[0])).litellm.budget) == jsonencode({
      usd    = 25
      period = "7d"
    })
    error_message = "litellm: ${jsonencode(yamldecode(nonsensitive(helm_release.kube_agents.values[0])).litellm)}"
  }
}

run "the_period_defaults_to_one_day" {
  command = apply
  variables {
    litellm_budget = { usd = 2.5 }
  }
  assert {
    condition     = yamldecode(nonsensitive(helm_release.kube_agents.values[0])).litellm.budget.period == "1d"
    error_message = "litellm: ${jsonencode(yamldecode(nonsensitive(helm_release.kube_agents.values[0])).litellm)}"
  }
}

# Unset, and set to 0, render no budget key at all, so an install without a
# ceiling renders the values it did (the *.values.golden files in gitops_forge
# hold that byte for byte).
run "unset_renders_no_budget_key" {
  command = apply
  assert {
    condition     = !contains(keys(yamldecode(nonsensitive(helm_release.kube_agents.values[0])).litellm), "budget")
    error_message = "litellm: ${jsonencode(yamldecode(nonsensitive(helm_release.kube_agents.values[0])).litellm)}"
  }
}

run "zero_renders_no_budget_key_whatever_the_period" {
  command = apply
  variables {
    litellm_budget = { usd = 0, period = "not-a-duration" }
  }
  assert {
    condition     = !contains(keys(yamldecode(nonsensitive(helm_release.kube_agents.values[0])).litellm), "budget")
    error_message = "litellm: ${jsonencode(yamldecode(nonsensitive(helm_release.kube_agents.values[0])).litellm)}"
  }
}

run "a_negative_budget_fails_the_plan" {
  command = plan
  variables {
    litellm_budget = { usd = -1 }
  }
  expect_failures = [var.litellm_budget]
}

run "a_period_that_is_not_a_duration_fails_the_plan_beside_a_budget" {
  command = plan
  variables {
    litellm_budget = { usd = 5, period = "1x" }
  }
  expect_failures = [var.litellm_budget]
}
