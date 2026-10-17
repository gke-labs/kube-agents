# SAP AI Core (model_provider = "sap"): the aicore_* variables reach the
# credentials Secret under LiteLLM's AICORE_* names and the chart is told the
# provider, an install on another provider carries none of the keys, and a sap
# install with neither credential shape set trips the warning check rather than
# the plan. Providers mocked and the cluster module's outputs fixed, as in
# alert_daily_limits.tftest.hcl; nothing here reaches a cloud.

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

run "client_credentials_reach_the_secret" {
  command = apply
  variables {
    model_provider       = "sap"
    aicore_client_id     = "sb-client"
    aicore_client_secret = "client-secret-value"
    aicore_auth_url      = "https://tenant.authentication.eu10.hana.ondemand.com"
    aicore_base_url      = "https://api.ai.prod.eu-central-1.aws.ml.hana.ondemand.com"
  }
  assert {
    condition     = yamldecode(nonsensitive(helm_release.kube_agents.values[0])).litellm.modelProvider == "sap"
    error_message = "litellm.modelProvider did not reach the release as sap"
  }
  assert {
    condition = (
      yamldecode(nonsensitive(helm_release.kube_agents.values[0])).platformAgent.credentials.data.AICORE_CLIENT_ID == "sb-client" &&
      yamldecode(nonsensitive(helm_release.kube_agents.values[0])).platformAgent.credentials.data.AICORE_CLIENT_SECRET == "client-secret-value" &&
      yamldecode(nonsensitive(helm_release.kube_agents.values[0])).platformAgent.credentials.data.AICORE_AUTH_URL == "https://tenant.authentication.eu10.hana.ondemand.com" &&
      yamldecode(nonsensitive(helm_release.kube_agents.values[0])).platformAgent.credentials.data.AICORE_BASE_URL == "https://api.ai.prod.eu-central-1.aws.ml.hana.ondemand.com"
    )
    error_message = "the four discrete SAP AI Core values did not all reach the credentials Secret"
  }
  # Empty variables stay out of the Secret, as an empty API key does: an empty
  # AICORE_SERVICE_KEY would be a source LiteLLM reads before the discrete keys.
  assert {
    condition     = !contains(keys(yamldecode(nonsensitive(helm_release.kube_agents.values[0])).platformAgent.credentials.data), "AICORE_SERVICE_KEY")
    error_message = "an unset aicore_service_key reached the credentials Secret"
  }
}

run "a_service_key_alone_is_enough" {
  command = apply
  variables {
    model_provider        = "sap"
    aicore_service_key    = "{\"clientid\":\"sb-client\",\"clientsecret\":\"s\",\"url\":\"https://tenant.authentication.eu10.hana.ondemand.com\",\"serviceurls\":{\"AI_API_URL\":\"https://api.ai.prod.eu-central-1.aws.ml.hana.ondemand.com\"}}"
    aicore_resource_group = "team-a"
  }
  assert {
    condition     = yamldecode(nonsensitive(helm_release.kube_agents.values[0])).platformAgent.credentials.data.AICORE_RESOURCE_GROUP == "team-a"
    error_message = "aicore_resource_group did not reach the credentials Secret"
  }
  assert {
    condition     = jsondecode(yamldecode(nonsensitive(helm_release.kube_agents.values[0])).platformAgent.credentials.data.AICORE_SERVICE_KEY).clientid == "sb-client"
    error_message = "aicore_service_key did not reach the credentials Secret intact"
  }
}

run "another_provider_carries_no_sap_keys" {
  command = apply
  assert {
    condition = length([
      for key in keys(yamldecode(nonsensitive(helm_release.kube_agents.values[0])).platformAgent.credentials.data) : key
      if startswith(key, "AICORE_")
    ]) == 0
    error_message = "a default install carries AICORE_* keys in its credentials Secret"
  }
}

run "sap_without_credentials_warns" {
  command = plan
  variables {
    model_provider  = "sap"
    aicore_base_url = "https://api.ai.prod.eu-central-1.aws.ml.hana.ondemand.com"
  }
  expect_failures = [check.sap_ai_core_credentials_present]
}

run "both_credential_shapes_warn" {
  command = plan
  variables {
    model_provider     = "sap"
    aicore_service_key = "{\"clientid\":\"sb-client\"}"
    aicore_client_id   = "sb-other"
  }
  expect_failures = [check.sap_ai_core_one_credential_shape]
}

run "an_unknown_provider_is_refused" {
  command = plan
  variables {
    model_provider = "sap_ai_core"
  }
  expect_failures = [var.model_provider]
}
