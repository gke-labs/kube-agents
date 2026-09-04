data "google_project" "this" {
  project_id = var.project_id
}

resource "google_pubsub_topic" "chat_events" {
  #checkov:skip=CKV_GCP_83:Chat event topic uses default Google-managed encryption keys
  project = var.project_id
  name    = var.topic_name
}

resource "google_pubsub_subscription" "chat_events" {
  project              = var.project_id
  name                 = var.subscription_name
  topic                = google_pubsub_topic.chat_events.id
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
  provider = google-beta

  project = var.project_id
  service = "gsuiteaddons.googleapis.com"
}

resource "google_project_service_identity" "chat" {
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
  project = var.project_id
  topic   = google_pubsub_topic.chat_events.id
  role    = "roles/pubsub.publisher"
  member  = "serviceAccount:chat-api-push@system.gserviceaccount.com"
}

resource "google_pubsub_topic_iam_member" "gsuiteaddons_publisher" {
  project = var.project_id
  topic   = google_pubsub_topic.chat_events.id
  role    = "roles/pubsub.publisher"
  member  = "serviceAccount:service-${data.google_project.this.number}@gcp-sa-gsuiteaddons.iam.gserviceaccount.com"

  depends_on = [google_project_service_identity.gsuiteaddons]
}

resource "google_pubsub_subscription_iam_member" "agent_subscriber" {
  project      = var.project_id
  subscription = google_pubsub_subscription.chat_events.id
  role         = "roles/pubsub.subscriber"
  member       = "serviceAccount:${var.agent_service_account_email}"
}

resource "google_pubsub_subscription_iam_member" "agent_viewer" {
  project      = var.project_id
  subscription = google_pubsub_subscription.chat_events.id
  role         = "roles/pubsub.viewer"
  member       = "serviceAccount:${var.agent_service_account_email}"
}
