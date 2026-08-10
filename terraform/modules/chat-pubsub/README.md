# Google Chat Pub/Sub Routing Module

Reusable Terraform module for provisioning the Google Chat → Pub/Sub inbound routing the Platform Agent's chat integration relies on: the chat-events topic and pull subscription, the Workspace Add-ons **and** Chat API service identities, and the IAM bindings that let Google Chat publish and the agent subscribe.

The Chat API service identity registration is load-bearing: without it, Google Chat silently delivers zero events (no publishes, no errors) and the "Service account email" field on the Chat API configuration page never populates, even though both registrations resolve to the same P4SA.

## Relationship to the provisioning scripts

This module and `k8s-operator/scripts/provision_05_gcp_gchat.sh` create the **same** topic,
subscription, service identities, and IAM bindings — use one or the other for a given
project, never both. The canonical identifiers (topic `platform-agent-chat-events`,
subscription `platform-agent-chat-events-sub`) live in `k8s-operator/scripts/common.sh`,
and the module's defaults mirror them.

The module outputs are the values the PlatformAgent CR's `googleChat` integration needs.

## Usage

```hcl
module "chat_pubsub" {
  source                      = "git::https://github.com/gke-labs/kube-agents.git//terraform/modules/chat-pubsub?ref=vX.Y.Z"
  project_id                  = "my-gcp-project"
  agent_service_account_email = "kubeagents-platform-gsa@my-gcp-project.iam.gserviceaccount.com"
}
```

See the [Release versioning & promotion guide](../../../docs/site/src/content/docs/deploy/release-versioning.md) for SemVer pinning instructions.
