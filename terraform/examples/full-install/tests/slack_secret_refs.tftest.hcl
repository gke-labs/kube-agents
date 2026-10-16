# Slack Secret references wiring: when custom secret names are specified,
# botTokenSecretRef and appTokenSecretRef are passed to the chart and the
# corresponding token keys are omitted from the shared credentials Secret.

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

run "slack_with_external_secret_refs" {
  command = plan
  variables {
    enable_slack                = true
    slack_bot_token_secret_name = "custom-bot-secret"
    slack_bot_token_secret_key  = "custom-bot-key"
    slack_app_token_secret_name = "custom-app-secret"
    slack_app_token_secret_key  = "custom-app-key"
  }
  assert {
    condition     = local.platform_agent_integration.slack.botTokenSecretRef.name == "custom-bot-secret"
    error_message = "botTokenSecretRef.name: ${jsonencode(local.platform_agent_integration.slack.botTokenSecretRef)}"
  }
  assert {
    condition     = local.platform_agent_integration.slack.botTokenSecretRef.key == "custom-bot-key"
    error_message = "botTokenSecretRef.key: ${jsonencode(local.platform_agent_integration.slack.botTokenSecretRef)}"
  }
  assert {
    condition     = local.platform_agent_integration.slack.appTokenSecretRef.name == "custom-app-secret"
    error_message = "appTokenSecretRef.name: ${jsonencode(local.platform_agent_integration.slack.appTokenSecretRef)}"
  }
  assert {
    condition     = local.platform_agent_integration.slack.appTokenSecretRef.key == "custom-app-key"
    error_message = "appTokenSecretRef.key: ${jsonencode(local.platform_agent_integration.slack.appTokenSecretRef)}"
  }
  assert {
    condition     = !contains(keys(local.slack_credentials), "SLACK_BOT_TOKEN")
    error_message = "slack_credentials unexpectedly holds SLACK_BOT_TOKEN"
  }
  assert {
    condition     = !contains(keys(local.slack_credentials), "SLACK_APP_TOKEN")
    error_message = "slack_credentials unexpectedly holds SLACK_APP_TOKEN"
  }
}

run "slack_with_external_secret_refs_and_no_tokens_passes_check" {
  command = plan
  variables {
    enable_slack                = true
    slack_bot_token_secret_name = "custom-bot-secret"
    slack_app_token_secret_name = "custom-app-secret"
  }
  assert {
    condition     = local.platform_agent_integration.slack.botTokenSecretRef.name == "custom-bot-secret"
    error_message = "botTokenSecretRef.name: ${jsonencode(local.platform_agent_integration.slack.botTokenSecretRef)}"
  }
  assert {
    condition     = local.platform_agent_integration.slack.appTokenSecretRef.name == "custom-app-secret"
    error_message = "appTokenSecretRef.name: ${jsonencode(local.platform_agent_integration.slack.appTokenSecretRef)}"
  }
}

run "slack_key_without_name_is_rejected" {
  command = plan
  variables {
    enable_slack               = true
    slack_bot_token            = "xoxb-test"
    slack_app_token            = "xapp-test"
    slack_bot_token_secret_key = "custom-key"
  }
  expect_failures = [
    helm_release.kube_agents,
  ]
}

run "slack_with_inline_tokens_only" {
  command = plan
  variables {
    enable_slack    = true
    slack_bot_token = "xoxb-test"
    slack_app_token = "xapp-test"
  }
  assert {
    condition     = !contains(keys(local.platform_agent_integration.slack), "botTokenSecretRef")
    error_message = "slack integration unexpectedly holds botTokenSecretRef: ${jsonencode(local.platform_agent_integration.slack)}"
  }
  assert {
    condition     = !contains(keys(local.platform_agent_integration.slack), "appTokenSecretRef")
    error_message = "slack integration unexpectedly holds appTokenSecretRef: ${jsonencode(local.platform_agent_integration.slack)}"
  }
  assert {
    condition     = contains(keys(local.slack_credentials), "SLACK_BOT_TOKEN")
    error_message = "slack_credentials missing SLACK_BOT_TOKEN"
  }
  assert {
    condition     = contains(keys(local.slack_credentials), "SLACK_APP_TOKEN")
    error_message = "slack_credentials missing SLACK_APP_TOKEN"
  }
}
