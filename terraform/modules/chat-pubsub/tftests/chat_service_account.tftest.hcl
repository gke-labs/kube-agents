# Same-project, cross-project (service-account impersonation), and unmanaged
# modes of the chat-pubsub module. Providers are mocked so nothing reaches GCP.

mock_provider "google" {
  mock_data "google_project" {
    defaults = {
      number = "123456789012"
    }
  }
}

mock_provider "google-beta" {}

variables {
  project_id                  = "workspace-chat-proj"
  topic_name                  = "platform-agent-chat-events"
  subscription_name           = "platform-agent-chat-events-sub"
  agent_service_account_email = "kubeagents-platform-gsa@gke-host-proj.iam.gserviceaccount.com"
}

run "same_project_creates_no_chat_service_account_and_binds_agent_directly" {
  command = plan

  assert {
    condition     = length(google_service_account.chat) == 0
    error_message = "same-project mode must not create a dedicated Chat service account"
  }

  assert {
    condition     = length(google_service_account_iam_member.agent_token_creator) == 0
    error_message = "same-project mode must not create a token-creator binding"
  }

  assert {
    condition     = google_pubsub_subscription_iam_member.agent_subscriber[0].member == "serviceAccount:kubeagents-platform-gsa@gke-host-proj.iam.gserviceaccount.com"
    error_message = "same-project mode must grant roles/pubsub.subscriber directly to agent_service_account_email"
  }

  assert {
    condition     = google_pubsub_subscription_iam_member.agent_viewer[0].member == "serviceAccount:kubeagents-platform-gsa@gke-host-proj.iam.gserviceaccount.com"
    error_message = "same-project mode must grant roles/pubsub.viewer directly to agent_service_account_email"
  }

  assert {
    condition     = output.chat_service_account_email == ""
    error_message = "same-project mode must output an empty chat_service_account_email"
  }
}

run "cross_project_creates_chat_service_account_and_binds_subscription_to_it" {
  command = plan

  variables {
    chat_service_account_id = "kubeagents-chat-gsa"
  }

  assert {
    condition     = length(google_service_account.chat) == 1 && google_service_account.chat[0].account_id == "kubeagents-chat-gsa" && google_service_account.chat[0].project == "workspace-chat-proj"
    error_message = "cross-project mode must create google_service_account.chat in project_id"
  }

  assert {
    condition     = length(google_service_account_iam_member.agent_token_creator) == 1 && google_service_account_iam_member.agent_token_creator[0].role == "roles/iam.serviceAccountTokenCreator" && google_service_account_iam_member.agent_token_creator[0].member == "serviceAccount:kubeagents-platform-gsa@gke-host-proj.iam.gserviceaccount.com"
    error_message = "cross-project mode must grant agent_service_account_email roles/iam.serviceAccountTokenCreator on the Chat service account"
  }

  assert {
    condition     = google_pubsub_subscription_iam_member.agent_subscriber[0].member == "serviceAccount:kubeagents-chat-gsa@workspace-chat-proj.iam.gserviceaccount.com"
    error_message = "cross-project mode must bind roles/pubsub.subscriber to the Chat service account"
  }

  assert {
    condition     = google_pubsub_subscription_iam_member.agent_viewer[0].member == "serviceAccount:kubeagents-chat-gsa@workspace-chat-proj.iam.gserviceaccount.com"
    error_message = "cross-project mode must bind roles/pubsub.viewer to the Chat service account"
  }

  assert {
    condition     = output.chat_service_account_email == "kubeagents-chat-gsa@workspace-chat-proj.iam.gserviceaccount.com"
    error_message = "cross-project mode must output the Chat service account email"
  }
}

run "unmanaged_mode_creates_no_resources_and_outputs_names" {
  command = plan

  variables {
    chat_service_account_id = "kubeagents-chat-gsa"
    manage                  = false
  }

  assert {
    condition     = length(data.google_project.this) == 0 && length(google_service_account.chat) == 0 && length(google_service_account_iam_member.agent_token_creator) == 0 && length(google_pubsub_topic.chat_events) == 0 && length(google_pubsub_subscription.chat_events) == 0 && length(google_project_service_identity.gsuiteaddons) == 0 && length(google_project_service_identity.chat) == 0 && length(google_pubsub_topic_iam_member.chat_api_push_publisher) == 0 && length(google_pubsub_topic_iam_member.gsuiteaddons_publisher) == 0 && length(google_pubsub_subscription_iam_member.agent_subscriber) == 0 && length(google_pubsub_subscription_iam_member.agent_viewer) == 0
    error_message = "manage = false must create no data sources or resources in the Chat project"
  }

  assert {
    condition     = output.topic_name == "platform-agent-chat-events" && output.subscription_name == "platform-agent-chat-events-sub" && output.chat_service_account_email == "kubeagents-chat-gsa@workspace-chat-proj.iam.gserviceaccount.com"
    error_message = "manage = false must still output topic_name, subscription_name, and chat_service_account_email"
  }
}
