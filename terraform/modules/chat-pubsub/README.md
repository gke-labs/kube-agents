# Google Chat Pub/Sub Routing Module

Reusable Terraform module for provisioning the Google Chat → Pub/Sub inbound routing the Platform Agent's chat integration relies on: the chat-events topic and pull subscription, the Workspace Add-ons **and** Chat API service identities, and the IAM bindings that let Google Chat publish and the agent subscribe.

The Chat API service identity registration is load-bearing: without it, Google Chat silently delivers zero events (no publishes, no errors) and the "Service account email" field on the Chat API configuration page never populates, even though both registrations resolve to the same P4SA.

## Relationship to the install

This is the module the full-install composition (and therefore `install.sh` with
`--enable-google-chat`) uses for the Chat backend. The canonical identifiers (topic
`platform-agent-chat-events`, subscription `platform-agent-chat-events-sub`) are the
composition's `chat_topic_name`/`chat_subscription_name` defaults,
and the module's defaults mirror them.

The module outputs are the values the PlatformAgent CR's `googleChat` integration needs.

## Usage

```hcl
module "chat_pubsub" {
  source                      = "git::https://github.com/gke-labs/kube-agents.git//terraform/modules/chat-pubsub?ref=1.2.0"
  project_id                  = "my-gcp-project"
  agent_service_account_email = "kubeagents-platform-gsa@my-gcp-project.iam.gserviceaccount.com"
}
```

### Cross-project and cross-organization Google Chat

When the Google Chat app and its Pub/Sub topic live in a different GCP project or organization from the GKE cluster, pass `chat_service_account_id`:

```hcl
module "chat_pubsub" {
  source                      = "git::https://github.com/gke-labs/kube-agents.git//terraform/modules/chat-pubsub?ref=1.2.0"
  project_id                  = "my-workspace-chat-project"
  agent_service_account_email = "kubeagents-platform-gsa@my-gke-project.iam.gserviceaccount.com"
  chat_service_account_id     = "kubeagents-chat-gsa"
}
```

With `chat_service_account_id` set, the module creates `<chat_service_account_id>@<project_id>.iam.gserviceaccount.com`, grants `agent_service_account_email` `roles/iam.serviceAccountTokenCreator` on that service account as a resource, and binds `roles/pubsub.subscriber` and `roles/pubsub.viewer` on the subscription to the Chat service account instead of the agent's GSA. Set `manage = false` when the installing identity cannot administer `project_id` and a project admin provisions those resources out of band; the module then creates nothing and still outputs `topic_name`, `subscription_name`, and `chat_service_account_email` for the `PlatformAgent` CR.

See the [Release versioning guide](../../../docs/site/src/content/docs/deploy/release-versioning.md) for SemVer pinning instructions.
