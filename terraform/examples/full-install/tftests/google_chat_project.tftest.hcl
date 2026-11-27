# Same-project, cross-project managed, and cross-project unmanaged Google Chat
# configurations in the full-install composition. Providers are mocked and the
# cluster module is overridden so nothing reaches GCP.

mock_provider "google" {
  mock_data "google_project" {
    defaults = {
      number = "123456789012"
    }
  }
  mock_resource "google_service_account" {
    defaults = {
      name   = "projects/tftest-project/serviceAccounts/kubeagents-platform-gsa@tftest-project.iam.gserviceaccount.com"
      email  = "kubeagents-platform-gsa@tftest-project.iam.gserviceaccount.com"
      member = "serviceAccount:kubeagents-platform-gsa@tftest-project.iam.gserviceaccount.com"
    }
  }
}

mock_provider "google-beta" {}
mock_provider "helm" {}
mock_provider "http" {}
mock_provider "random" {}
mock_provider "time" {}
mock_provider "tls" {}

override_module {
  target = module.gke_cluster
  outputs = {
    cluster_id              = "projects/tftest-project/locations/us-central1/clusters/tftest-cluster"
    cluster_name            = "tftest-cluster"
    endpoint                = "203.0.113.10"
    ca_certificate          = "LS0tLS1CRUdJTiBDRVJUSUZJQ0FURS0tLS0tCg=="
    network_policy_enforced = true
  }
}

variables {
  project_id            = "tftest-project"
  cluster_name          = "tftest-cluster"
  location              = "us-central1"
  api_server_key        = "tftest-api-server-key"
  install_helm_releases = false
  enable_google_chat    = true
}

run "same_project_enables_chat_apis_in_host_project_and_omits_chat_sa" {
  command = plan

  assert {
    condition     = contains(keys(google_project_service.required), "chat.googleapis.com") && contains(keys(google_project_service.required), "gsuiteaddons.googleapis.com") && contains(keys(google_project_service.required), "pubsub.googleapis.com")
    error_message = "same-project Google Chat must enable chat, gsuiteaddons, and pubsub APIs in project_id"
  }

  assert {
    condition     = length(google_project_service.chat_project) == 0
    error_message = "same-project Google Chat must not create cross-project google_project_service.chat_project resources"
  }

  assert {
    condition     = !contains(keys(local.platform_agent_integration.googleChat), "projectId") && !contains(keys(local.platform_agent_integration.googleChat), "serviceAccountEmail")
    error_message = "same-project Google Chat must leave projectId and serviceAccountEmail unset in chart values"
  }
}

run "cross_project_managed_moves_chat_apis_and_wires_chat_service_account" {
  command = plan

  variables {
    chat_project_id = "workspace-chat-proj"
  }

  assert {
    condition     = !contains(keys(google_project_service.required), "chat.googleapis.com") && !contains(keys(google_project_service.required), "gsuiteaddons.googleapis.com")
    error_message = "cross-project Google Chat must remove chat.googleapis.com and gsuiteaddons.googleapis.com from the GKE host project's required APIs"
  }

  assert {
    condition     = contains(keys(google_project_service.chat_project), "chat.googleapis.com") && contains(keys(google_project_service.chat_project), "gsuiteaddons.googleapis.com") && contains(keys(google_project_service.chat_project), "pubsub.googleapis.com")
    error_message = "cross-project managed Google Chat must enable chat, gsuiteaddons, and pubsub APIs in chat_project_id"
  }

  assert {
    condition     = local.platform_agent_integration.googleChat.projectId == "workspace-chat-proj"
    error_message = "cross-project Google Chat must set googleChat.projectId to chat_project_id in chart values"
  }

  assert {
    condition     = local.platform_agent_integration.googleChat.serviceAccountEmail == "kubeagents-chat-gsa@workspace-chat-proj.iam.gserviceaccount.com"
    error_message = "cross-project Google Chat must set googleChat.serviceAccountEmail in chart values"
  }
}

run "cross_project_unmanaged_creates_nothing_in_chat_project_and_keeps_chart_values" {
  command = plan

  variables {
    chat_project_id     = "workspace-chat-proj"
    chat_manage_project = false
  }

  assert {
    condition     = !contains(keys(google_project_service.required), "chat.googleapis.com") && !contains(keys(google_project_service.required), "gsuiteaddons.googleapis.com") && length(google_project_service.chat_project) == 0
    error_message = "chat_manage_project = false must not enable Chat APIs in either project"
  }

  assert {
    condition     = local.platform_agent_integration.googleChat.projectId == "workspace-chat-proj" && local.platform_agent_integration.googleChat.serviceAccountEmail == "kubeagents-chat-gsa@workspace-chat-proj.iam.gserviceaccount.com" && local.platform_agent_integration.googleChat.topicName == "platform-agent-chat-events" && local.platform_agent_integration.googleChat.subscriptionName == "platform-agent-chat-events-sub"
    error_message = "chat_manage_project = false must still populate googleChat.projectId, serviceAccountEmail, topicName, and subscriptionName in chart values"
  }
}

run "cross_project_custom_chat_service_account_id_reaches_chart_values" {
  command = plan

  variables {
    chat_project_id         = "workspace-chat-proj"
    chat_manage_project     = false
    chat_service_account_id = "custom-chat-two-gsa"
  }

  assert {
    condition     = local.platform_agent_integration.googleChat.serviceAccountEmail == "custom-chat-two-gsa@workspace-chat-proj.iam.gserviceaccount.com"
    error_message = "custom chat_service_account_id must reach googleChat.serviceAccountEmail in chart values"
  }
}
