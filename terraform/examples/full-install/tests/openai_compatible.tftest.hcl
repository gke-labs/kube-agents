# model_provider = "openai_compatible": the litellm.openaiCompatible values the
# chart reads, in the in-cluster Service and the CIDR forms; the optional key in
# the credentials Secret; the plan refusing a missing model or base URL; and no
# openaiCompatible key for any other provider. Providers mocked and the cluster
# module's outputs fixed, as in gitops_forge.tftest.hcl; nothing here reaches a
# cloud.

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

run "service_form_renders_the_target_port" {
  command = apply
  variables {
    model_provider                = "openai_compatible"
    model_default_name            = "some-org/some-model"
    openai_compatible_api_base    = "http://llm.models.svc.cluster.local/v1"
    openai_compatible_target_port = 8000
  }
  assert {
    condition = jsonencode(yamldecode(nonsensitive(helm_release.kube_agents.values[0])).litellm) == jsonencode({
      maxTokens        = 0
      modelDefaultName = "some-org/some-model"
      modelProvider    = "openai_compatible"
      openaiCompatible = {
        apiBase           = "http://llm.models.svc.cluster.local/v1"
        caBundleSecretRef = { key = "ca.crt", name = "" }
        egressCIDRs       = []
        targetPort        = 8000
      }
    })
    error_message = "litellm: ${jsonencode(yamldecode(nonsensitive(helm_release.kube_agents.values[0])).litellm)}"
  }
  # No key given: nothing lands in the Secret.
  assert {
    condition     = !contains(keys(nonsensitive(local.credentials)), "OPENAI_COMPATIBLE_API_KEY")
    error_message = "an empty openai_compatible_api_key reached the credentials Secret"
  }
}

run "cidr_form_renders_the_ranges_the_ca_and_the_key" {
  command = apply
  variables {
    model_provider                   = "openai_compatible"
    model_default_name               = "some-org/some-model"
    openai_compatible_api_base       = "https://llm.corp.example.com:8443/v1"
    openai_compatible_egress_cidrs   = ["10.8.0.0/24", "10.9.0.0/24"]
    openai_compatible_ca_secret_name = "llm-ca"
    openai_compatible_api_key        = "oc-key"
  }
  assert {
    condition = jsonencode(yamldecode(nonsensitive(helm_release.kube_agents.values[0])).litellm.openaiCompatible) == jsonencode({
      apiBase           = "https://llm.corp.example.com:8443/v1"
      caBundleSecretRef = { key = "ca.crt", name = "llm-ca" }
      egressCIDRs       = ["10.8.0.0/24", "10.9.0.0/24"]
      targetPort        = null
    })
    error_message = "openaiCompatible: ${jsonencode(yamldecode(nonsensitive(helm_release.kube_agents.values[0])).litellm.openaiCompatible)}"
  }
  assert {
    condition     = nonsensitive(local.credentials["OPENAI_COMPATIBLE_API_KEY"]) == "oc-key"
    error_message = "openai_compatible_api_key did not reach the credentials Secret"
  }
}

# Any other provider renders no openaiCompatible key, so its release values
# stay what they were (the *.values.golden files in gitops_forge hold that
# byte for byte), even with the variables set.
run "other_providers_render_no_openai_compatible_key" {
  command = apply
  variables {
    openai_compatible_api_base     = "https://llm.corp.example.com/v1"
    openai_compatible_egress_cidrs = ["10.8.0.0/24"]
  }
  assert {
    condition     = !contains(keys(yamldecode(nonsensitive(helm_release.kube_agents.values[0])).litellm), "openaiCompatible")
    error_message = "litellm: ${jsonencode(yamldecode(nonsensitive(helm_release.kube_agents.values[0])).litellm)}"
  }
}

run "missing_model_fails_the_plan" {
  command = plan
  variables {
    model_provider                = "openai_compatible"
    openai_compatible_api_base    = "http://llm.models.svc.cluster.local/v1"
    openai_compatible_target_port = 8000
  }
  expect_failures = [helm_release.kube_agents]
}

run "missing_api_base_fails_the_plan" {
  command = plan
  variables {
    model_provider                = "openai_compatible"
    model_default_name            = "some-org/some-model"
    openai_compatible_target_port = 8000
  }
  expect_failures = [helm_release.kube_agents]
}

run "target_port_out_of_range_is_refused" {
  command = plan
  variables {
    model_provider                = "openai_compatible"
    model_default_name            = "some-org/some-model"
    openai_compatible_api_base    = "http://llm.models.svc.cluster.local/v1"
    openai_compatible_target_port = 70000
  }
  expect_failures = [var.openai_compatible_target_port]
}

run "api_base_without_a_scheme_is_refused" {
  command = plan
  variables {
    model_provider             = "openai_compatible"
    model_default_name         = "some-org/some-model"
    openai_compatible_api_base = "llm.models.svc.cluster.local/v1"
  }
  expect_failures = [var.openai_compatible_api_base]
}

run "ca_secret_name_must_be_a_secret_name" {
  command = plan
  variables {
    model_provider                   = "openai_compatible"
    model_default_name               = "some-org/some-model"
    openai_compatible_api_base       = "https://llm.example.com/v1"
    openai_compatible_ca_secret_name = "Not_A_Name"
  }
  expect_failures = [var.openai_compatible_ca_secret_name]
}
