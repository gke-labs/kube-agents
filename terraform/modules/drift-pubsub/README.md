# Drift Audit-Log Pub/Sub Routing Module

Reusable Terraform module for provisioning the GKE audit log → Pub/Sub delivery path the drift detector consumes: the Log Router sink, the drift-audit topic and pull subscription, and the IAM bindings that let the sink publish and the detector subscribe.

The detector cannot read audit logs from the Kubernetes API. On GKE the control plane is managed, so the API server's audit backend is not the operator's to configure and the stream surfaces only in Cloud Logging — hence a sink rather than an informer. The design and the Phase 0 spike that produced the sink filter are documented under `agents/platform/docs/drift-detection/`.

The sink's writer-identity grant is load-bearing: without `roles/pubsub.publisher` on the topic the sink is silently inert. Log Router raises no error, the topic receives nothing, and from the detector's side that is indistinguishable from "no drift happened."

## What this module does not do

- **It does not create a service account.** `detector_service_account_email` names an existing GSA. The GSA and its Workload Identity binding belong to [`kube-agents-iam`](../kube-agents-iam/), which already creates both; minting one here would produce a second identity for the same workload.
- **It does not enable APIs.** No module in this repository calls `google_project_service` — the root composition does, with `disable_on_destroy = false`, so that destroying one component cannot disable an API the rest of the project depends on.
- **It does not filter principals.** The sink exports every mutating call, including the ~78% from `system:` controllers. The detector classifies principals itself and needs the unfiltered volume to measure its noise profile; a sink-side filter would discard the denominators that make a mistuned automation allowlist debuggable.

## Relationship to the provisioning scripts

Unlike the other modules here, this one has **no `provision_NN_*.sh` counterpart** and therefore no mutually-exclusive-with-the-script caveat: Terraform is the only path. Its identifiers are correspondingly absent from `k8s-operator/scripts/common.sh`.

## Prerequisites

The caller must have `pubsub.googleapis.com` and `logging.googleapis.com` enabled on the project. `logging.googleapis.com` is unconditional in [`full-install`](../../examples/full-install/), but **`pubsub.googleapis.com` is currently gated behind `enable_google_chat`** there — an install without Chat will not have it. Move Pub/Sub out of that conditional before wiring this module into the composition.

## Usage

```hcl
module "drift_pubsub" {
  source                         = "git::https://github.com/gke-labs/kube-agents.git//terraform/modules/drift-pubsub?ref=vX.Y.Z"
  project_id                     = "my-gcp-project"
  detector_service_account_email = "kubeagents-platform-gsa@my-gcp-project.iam.gserviceaccount.com"
}
```

`cluster_names` defaults to empty, which exports every GKE cluster in the project through one sink and leaves the detector to route on `resource.labels.cluster_name`. Set it to narrow the export:

```hcl
  cluster_names = ["platform-agent-host", "prod-us-east4"]
```

`subscription_id` is the output to feed the detector; it is the fully-qualified path its `--subscription` flag expects.

See the [Release versioning & promotion guide](../../../docs/site/src/content/docs/deploy/release-versioning.md) for SemVer pinning instructions.
