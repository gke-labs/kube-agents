locals {
  create_chat_service_account = var.chat_service_account_id != ""
  chat_service_account_email = local.create_chat_service_account ? (
    var.manage
    ? "${google_service_account.chat[0].account_id}@${google_service_account.chat[0].project}.iam.gserviceaccount.com"
    : "${var.chat_service_account_id}@${var.project_id}.iam.gserviceaccount.com"
  ) : ""
  subscriber_service_account_email = local.create_chat_service_account ? local.chat_service_account_email : var.agent_service_account_email
}

data "google_project" "this" {
  count      = var.manage ? 1 : 0
  project_id = var.project_id
}

resource "google_service_account" "chat" {
  count        = var.manage && local.create_chat_service_account ? 1 : 0
  account_id   = var.chat_service_account_id
  display_name = "Platform Agent Google Chat Service Account"
  description  = "Impersonated by the Platform Agent credential broker for Google Chat API calls and Pub/Sub subscription pulls across GCP projects or organizations."
  project      = var.project_id
}

# Bound on the Chat service account as a resource, never at project scope: the
# agent GSA can mint short-lived tokens for this one identity in project_id so
# the credential broker's GoogleChatRelay can call the Chat API and pull the
# subscription across GCP organization boundaries where Domain Restricted
# Sharing blocks direct cross-org grants on the Pub/Sub subscription.
resource "google_service_account_iam_member" "agent_token_creator" {
  count              = var.manage && local.create_chat_service_account ? 1 : 0
  service_account_id = google_service_account.chat[0].name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:${var.agent_service_account_email}"
}

resource "google_pubsub_topic" "chat_events" {
  count = var.manage ? 1 : 0
  #checkov:skip=CKV_GCP_83:Chat event topic uses default Google-managed encryption keys
  project = var.project_id
  name    = var.topic_name
}

resource "google_pubsub_subscription" "chat_events" {
  count                = var.manage ? 1 : 0
  project              = var.project_id
  name                 = var.subscription_name
  topic                = google_pubsub_topic.chat_events[0].id
  ack_deadline_seconds = 60
}

# IMPORTANT: the Chat API needs its own service identity registration in
# addition to the Workspace Add-ons one. Without it, Google Chat silently
# delivers ZERO events to the app (no Pub/Sub publishes, no errors — the Chat
# client just shows "not responding"), and the "Service account email" field
# on the Chat API configuration page never populates. Both registrations
# resolve to the same P4SA
# (service-<PROJECT_NUMBER>@gcp-sa-gsuiteaddons.iam.gserviceaccount.com), so
# the account's existence alone cannot tell you whether the Chat registration
# has happened.
resource "google_project_service_identity" "gsuiteaddons" {
  count    = var.manage ? 1 : 0
  provider = google-beta

  project = var.project_id
  service = "gsuiteaddons.googleapis.com"
}

resource "google_project_service_identity" "chat" {
  count    = var.manage ? 1 : 0
  provider = google-beta

  project = var.project_id
  service = "chat.googleapis.com"
}

# `.id`, never `.name`, on every binding below.
#
# GCP purges a topic's or subscription's IAM policy when the resource is deleted
# and recreated, so a binding that Terraform does not replace alongside it is
# gone from the live policy while state still records it as present. `.name` is
# resolved from an input variable and so is known at plan time: it reads the same
# before and after a replacement, the binding is excluded from the plan
# altogether, and the apply finishes green over an empty policy (`etag: ACAB`).
# The credential proxy then fails its chat pulls with PermissionDenied and serves
# HTTP 503, and nothing in the plan output ever mentioned the grant.
#
# `.id` is computed, so a replacement renders it unknown, which is what pulls the
# bindings into the plan with it. The provider's diff suppression treats
# `projects/P/topics/T` and `T` as the same value, so switching an existing
# install from one to the other plans no change of its own.
resource "google_pubsub_topic_iam_member" "chat_api_push_publisher" {
  count   = var.manage ? 1 : 0
  project = var.project_id
  topic   = google_pubsub_topic.chat_events[0].id
  role    = "roles/pubsub.publisher"
  member  = "serviceAccount:chat-api-push@system.gserviceaccount.com"
}

resource "google_pubsub_topic_iam_member" "gsuiteaddons_publisher" {
  count   = var.manage ? 1 : 0
  project = var.project_id
  topic   = google_pubsub_topic.chat_events[0].id
  role    = "roles/pubsub.publisher"
  member  = "serviceAccount:service-${data.google_project.this[0].number}@gcp-sa-gsuiteaddons.iam.gserviceaccount.com"

  depends_on = [google_project_service_identity.gsuiteaddons]
}

resource "google_pubsub_subscription_iam_member" "agent_subscriber" {
  count        = var.manage ? 1 : 0
  project      = var.project_id
  subscription = google_pubsub_subscription.chat_events[0].id
  role         = "roles/pubsub.subscriber"
  member       = "serviceAccount:${local.subscriber_service_account_email}"
}

resource "google_pubsub_subscription_iam_member" "agent_viewer" {
  count        = var.manage ? 1 : 0
  project      = var.project_id
  subscription = google_pubsub_subscription.chat_events[0].id
  role         = "roles/pubsub.viewer"
  member       = "serviceAccount:${local.subscriber_service_account_email}"
}

moved {
  from = google_pubsub_topic.chat_events
  to   = google_pubsub_topic.chat_events[0]
}

moved {
  from = google_pubsub_subscription.chat_events
  to   = google_pubsub_subscription.chat_events[0]
}

moved {
  from = google_project_service_identity.gsuiteaddons
  to   = google_project_service_identity.gsuiteaddons[0]
}

moved {
  from = google_project_service_identity.chat
  to   = google_project_service_identity.chat[0]
}

moved {
  from = google_pubsub_topic_iam_member.chat_api_push_publisher
  to   = google_pubsub_topic_iam_member.chat_api_push_publisher[0]
}

moved {
  from = google_pubsub_topic_iam_member.gsuiteaddons_publisher
  to   = google_pubsub_topic_iam_member.gsuiteaddons_publisher[0]
}

moved {
  from = google_pubsub_subscription_iam_member.agent_subscriber
  to   = google_pubsub_subscription_iam_member.agent_subscriber[0]
}

moved {
  from = google_pubsub_subscription_iam_member.agent_viewer
  to   = google_pubsub_subscription_iam_member.agent_viewer[0]
}
