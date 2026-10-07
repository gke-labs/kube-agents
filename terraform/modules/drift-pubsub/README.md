# Drift Audit-Log Pub/Sub Routing Module

Reusable Terraform module for provisioning the GKE audit log → Pub/Sub delivery path the drift detector consumes: the Log Router sink, the drift-audit topic and pull subscription, and the IAM bindings that let the sink publish and the detector subscribe — plus, where `topic_publishers` is set, publisher on the topic for each member it names.

The detector cannot read audit logs from the Kubernetes API. On GKE the control plane is managed, so the API server's audit backend is not the operator's to configure and the stream surfaces only in Cloud Logging — hence a sink rather than an informer.

The sink's writer-identity grant is load-bearing: without `roles/pubsub.publisher` on the topic the sink is silently inert. Log Router raises no error, the topic receives nothing, and from the detector's side that is indistinguishable from "no drift happened."

## Why the sink is created last and destroyed first

Cloud Logging starts exporting the moment a sink exists and keeps exporting for some minutes after one is deleted, because the Log Router holds sink configuration on a fleet that converges on its own schedule. An export that lands outside the window where the topic exists and the grant is in place mails an "[ACTION REQUIRED] Cloud Logging sink configuration error" to every principal holding `roles/owner` on the project. Both orderings are arranged around that:

- **On apply**, the grant names the Logging service agent as `service-<project-number>@gcp-sa-logging.iam.gserviceaccount.com`, derived rather than read from `google_logging_project_sink.drift_audit.writer_identity`. Reading it from the sink is what orders the grant after the sink; deriving it puts the grant first. `google_project_service_identity` asks Service Usage for the agent up front, since otherwise the first sink in a project is what creates it and the grant has nothing to bind. Enabling `logging.googleapis.com` is not enough on its own: in a project with the API on and no sink ever created, granting the role to that agent fails with "Service account … does not exist" until the Service Usage call is made. The call returns an _empty_ identity for Logging — no email — so it reads as though it achieved nothing; the minting is a side effect of making it.
- **On destroy**, `time_sleep.sink_drain` holds for `sink_drain_duration` (120s by default) between deleting the sink and removing the topic and the grant. Revoking publish early trades `topic_not_found` for `topic_permission_denied`, so both sit on the far side of the wait.

Neither closes the window completely. Google documents no bound on how long the Log Router takes to stop exporting, so 120s is a chosen margin rather than a measured convergence time, and the apply side still depends on IAM propagation. They take the email from every run to rarely. Renaming `topic_name` on a live install is a third case neither covers: that replaces the topic under a sink which stays live and is only updated in place, and the drain does not participate because nothing is being destroyed.

Deriving the identity means a project where Logging returns some other writer identity would be granted the wrong principal and left with an inert sink. The sink carries a `postcondition` comparing the two, so that fails the apply naming both. A postcondition runs after the resource is created and does not roll it back, so the failed apply leaves the sink live and exporting as an identity that holds no publish role: `topic_permission_denied` on every export, and the owner-wide mail this section exists to prevent, now continuous rather than momentary. Deleting the sink stops it immediately, and granting the role by hand stops it without clearing the check. Because a postcondition is re-evaluated on later plans, such a project would also be unable to apply anything in the composition — `sink_writer_identity_override` is the way out, moving the grant and the check together onto the identity Logging reported. The full-install composition passes it through as `drift_pubsub_sink_writer_identity_override`, which is the name the error gives an operator who reached it from there; `sink_drain_duration` is exposed the same way.

Deriving it also makes the grant's `member` a function of a data source, where reading it off the sink made it a function of a resource already in state. A caller that defers that read defers the member with it, and `member` is ForceNew: with `depends_on = [google_project_service.required]` on the module — which is how full-install calls it — any plan that adds or removes an API leaves `data.google_project.this` unread until apply, and the binding is planned for replacement while the sink stays live. That is this section's own window on another trigger, and #2487 carries the fix; setting the override pins the member and avoids it meanwhile. Narrowing the caller's `depends_on` to the APIs this module needs does not work, because Terraform resolves an indexed `depends_on` reference to the whole resource.

The three `depends_on` edges that carry all of this are pinned by [`tests/test_drift_pubsub_ordering.py`](../../../tests/test_drift_pubsub_ordering.py); removing one is otherwise invisible, since no plan can show the ordering.

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

Both principal clauses matter. Matching `^system:` alone leaves the GKE service agent behind — in the same sample `container-engine-robot` made 287 Lease writes, which would have inflated the surviving stream by 65%. The second clause matches any `*.iam.gserviceaccount.com`, covering it and any future service agent that carries the `iam` label. It does not cover the Google-managed accounts, which do not carry it — `<number>-compute@developer.`, `@cloudbuild.`, `@appspot.` and `@cloudservices.` — so their Lease writes survive this exclusion and reach the topic. That costs delivered volume and nothing else: the detector matches the whole `.gserviceaccount.com` domain and drops them as automation. Widening the suffix here would cut volume, and is left for its own change because it alters what a deployed install receives.

Set the variable to `false` to export the unfiltered stream while debugging.

## Prerequisites

The caller must have `pubsub.googleapis.com` and `logging.googleapis.com` enabled on the project. [`full-install`](../../examples/full-install/) enables both when it instantiates this module (`enable_drift_pubsub = true`; `logging.googleapis.com` is unconditional there, and `pubsub.googleapis.com` is enabled whenever any of its Pub/Sub-backed features is on). A standalone caller enables them itself: no module in this repository calls `google_project_service`.

Two more follow from the ordering above, and `full-install` already satisfies both. The module reads the project number, so `cloudresourcemanager.googleapis.com` must be enabled and the applying identity needs `resourcemanager.projects.get`; and it mints the Logging service agent through Service Usage, so it takes a `google-beta` provider configuration from the root.

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

The filter matches on the bare cluster name, which is unique within a project and location but not
across locations, so listing `prod` here exports every `prod` in the project. That is the safe
direction — the detector matches on the full `project/location/cluster` triple and reports anything
it cannot reach as `unreachable` rather than reading the wrong cluster — but it does mean a narrowed
`cluster_names` can still carry more traffic than the list suggests.

`subscription_id` is the output to feed the detector's `--subscription` flag, alongside `--project`:

```bash
drift-detector --project my-gcp-project --subscription "$(terraform output -raw subscription_id)"
```

The flag takes either form — this fully-qualified path, or the bare `subscription_name`, which it
qualifies with `--project`. `--project` is required either way, because the detector's credentials
are resolved against it.

`topic_publishers` defaults to empty, which leaves the sink's writer identity as the topic's only
publisher — the shape the section above assumes. Each member listed here takes
`roles/pubsub.publisher` on the topic as well:

```hcl
  topic_publishers = ["serviceAccount:bench-runner@my-gcp-project.iam.gserviceaccount.com"]
```

Weigh that against what the detector does with what arrives. It reads
`protoPayload.authenticationInfo.principalEmail` out of each record and classifies on it, and
Pub/Sub does not attach the publishing identity to the message, so the detector cannot tell a
record the sink exported from one a listed member composed. Anything that can publish here can
therefore make the detector report a change nobody made, under any principal it chooses. The
intended use is a test harness injecting synthetic audit records on a project set aside for it;
on an install carrying real traffic, leave it empty. Never list the detector's own
`detector_service_account_email` — the agent would be writing the stream its own pod reads.

Lowering `ack_deadline_seconds` below its 60s default means passing the detector a matching
`--batch-join-budget`. The detector holds a whole batch while it reads live objects, and the two
values are not wired together — it reads this one at startup and warns when its budget takes more
than half of it, but it does not adopt it.
[The detector's README](../../../k8s-operator/cmd/drift-detector/README.md) is canonical for what
happens when the budget outlasts the deadline.

See the [Release versioning & promotion guide](../../../docs/site/src/content/docs/deploy/release-versioning.md) for SemVer pinning instructions.
