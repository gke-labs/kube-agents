# The daily alert ceilings: a default install passes the chart no
# alertDailyLimits key, a set key reaches the release values and a null key
# does not, platformAgent.deployment.env in extra_helm_values still goes to the
# release beside the ceilings, and the variable refuses a value that is not a
# whole number from 0 to 2147483647 and a key that is not a bucket. Providers
# mocked and the cluster module's outputs fixed, as in gitops_forge.tftest.hcl;
# nothing here reaches a cloud.

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

run "default_passes_no_alert_daily_limits" {
  command = apply
  assert {
    condition     = !contains(keys(yamldecode(nonsensitive(helm_release.kube_agents.values[0])).platformAgent.deployment), "alertDailyLimits")
    error_message = "deployment: ${jsonencode(yamldecode(nonsensitive(helm_release.kube_agents.values[0])).platformAgent.deployment)}"
  }
}

run "all_null_passes_no_alert_daily_limits" {
  command = apply
  variables {
    alert_daily_limits = { critical = null, drift = null }
  }
  assert {
    condition     = !contains(keys(yamldecode(nonsensitive(helm_release.kube_agents.values[0])).platformAgent.deployment), "alertDailyLimits")
    error_message = "deployment: ${jsonencode(yamldecode(nonsensitive(helm_release.kube_agents.values[0])).platformAgent.deployment)}"
  }
}

run "only_the_set_keys_are_passed" {
  command = apply
  variables {
    alert_daily_limits = { critical = 50, drift = 20 }
  }
  assert {
    condition     = jsonencode(yamldecode(nonsensitive(helm_release.kube_agents.values[0])).platformAgent.deployment.alertDailyLimits) == jsonencode({ critical = 50, drift = 20 })
    error_message = "alertDailyLimits: ${jsonencode(yamldecode(nonsensitive(helm_release.kube_agents.values[0])).platformAgent.deployment.alertDailyLimits)}"
  }
  assert {
    condition     = yamldecode(nonsensitive(helm_release.kube_agents.values[0])).platformAgent.deployment.image.tag == var.image_tag
    error_message = "the ceilings replaced the rest of the deployment block"
  }
}

run "extra_helm_values_env_reaches_the_release_beside_the_ceilings" {
  command = apply
  variables {
    alert_daily_limits = { warning = 30 }
    extra_helm_values = {
      platformAgent = { deployment = { env = [{ name = "EOD_EXCLUDE_NAMESPACES", value = "kube-system" }] } }
    }
  }
  assert {
    condition     = jsonencode(yamldecode(nonsensitive(helm_release.kube_agents.values[0])).platformAgent.deployment.alertDailyLimits) == jsonencode({ warning = 30 })
    error_message = "alertDailyLimits: ${jsonencode(yamldecode(nonsensitive(helm_release.kube_agents.values[0])).platformAgent.deployment.alertDailyLimits)}"
  }
  assert {
    condition     = jsonencode(yamldecode(helm_release.kube_agents.values[1]).platformAgent.deployment.env) == jsonencode([{ name = "EOD_EXCLUDE_NAMESPACES", value = "kube-system" }])
    error_message = "extra_helm_values: ${helm_release.kube_agents.values[1]}"
  }
}

run "a_negative_limit_is_refused" {
  command = plan
  variables {
    alert_daily_limits = { warning = -1 }
  }
  expect_failures = [var.alert_daily_limits]
}

run "a_limit_above_the_maximum_is_refused" {
  command = plan
  variables {
    alert_daily_limits = { critical = 2147483648 }
  }
  expect_failures = [var.alert_daily_limits]
}

run "a_fractional_limit_is_refused" {
  command = plan
  variables {
    alert_daily_limits = { drift = 2.5 }
  }
  expect_failures = [var.alert_daily_limits]
}

run "an_unknown_key_is_refused" {
  command = plan
  variables {
    alert_daily_limits = { warnings = 15 }
  }
  expect_failures = [var.alert_daily_limits]
}
