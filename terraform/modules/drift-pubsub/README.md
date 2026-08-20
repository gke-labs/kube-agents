# Drift Audit-Log Pub/Sub Routing Module

Reusable Terraform module for provisioning the GKE audit log → Pub/Sub delivery path the drift detector consumes: the Log Router sink, the drift-audit topic and pull subscription, and the IAM bindings that let the sink publish and the detector subscribe.

The detector cannot read audit logs from the Kubernetes API. On GKE the control plane is managed, so the API server's audit backend is not the operator's to configure and the stream surfaces only in Cloud Logging — hence a sink rather than an informer.

The sink's writer-identity grant is load-bearing: without `roles/pubsub.publisher` on the topic the sink is silently inert. Log Router raises no error, the topic receives nothing, and from the detector's side that is indistinguishable from "no drift happened."

## What this module does not do

- **It does not create a service account.** `detector_service_account_email` names an existing GSA. The GSA and its Workload Identity binding belong to [`kube-agents-iam`](../kube-agents-iam/), which already creates both; minting one here would produce a second identity for the same workload.
- **It does not enable APIs.** No module in this repository calls `google_project_service` — the root composition does, with `disable_on_destroy = false`, so that destroying one component cannot disable an API the rest of the project depends on.
- **It does not tier principals.** Apart from the lease carve-out below, the sink exports every mutating call regardless of who made it, including the large majority from `system:` controllers. The detector classifies principals itself and needs the unfiltered volume to measure its noise profile; a sink-side tier filter would discard the denominators that make a mistuned automation allowlist debuggable.

## What the sink filter excludes

One category is dropped before publication: **Lease writes by machine identities**, controlled by `exclude_machine_lease_heartbeats` (default `true`).

`coordination.k8s.io` Leases are leader-election and node heartbeats. A Lease is created at runtime by whichever controller holds it, never applied from a manifest, so no Git-side object exists for it to diverge from — it cannot be drift. It is also overwhelmingly the bulk of the stream. Measured over a 15-minute window on a two-cluster project:

|                                   | count | share |
| --------------------------------- | ----- | ----- |
| `leases.update` + `leases.create` | 9,558 | 95.6% |
| Everything else                   | 442   | 4.4%  |

The 10,000 is the query's row cap rather than the window's true total, so it fixes the ratio but not the volume. A separate untruncated count put the surviving stream at 623 calls per 15 minutes — roughly **60k/day, against ~1.35M/day unfiltered**.

The exclusion is scoped by principal rather than dropping Leases outright, so a person running `kubectl patch lease` still reaches the detector. That is not GitOps drift, but it can knock an active controller off its lock, and discarding it silently is hard to defend.

Both principal clauses matter. Matching `^system:` alone leaves the GKE service agent behind — in the same sample `container-engine-robot` made 287 Lease writes, which would have inflated the surviving stream by 65%. The second clause matches any `*.iam.gserviceaccount.com`, covering it and any future service agent without a change here.

Set the variable to `false` to export the unfiltered stream while debugging.

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
