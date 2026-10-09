# Drift Audit-Log Pub/Sub Routing Module

Reusable Terraform module for provisioning the GKE audit log → Pub/Sub delivery path the drift detector consumes: the Log Router sink, the drift-audit topic and pull subscription, and the IAM binding that lets the sink publish — plus, where `topic_publishers` is set, publisher on the topic for each member it names. The detector's own subscriber and viewer grants are the caller's: see "What this module does not do".

The detector cannot read audit logs from the Kubernetes API. On GKE the control plane is managed, so the API server's audit backend is not the operator's to configure and the stream surfaces only in Cloud Logging — hence a sink rather than an informer.

The sink's writer-identity grant is load-bearing: without `roles/pubsub.publisher` on the topic the sink is silently inert. Log Router raises no error, the topic receives nothing, and from the detector's side that is indistinguishable from "no drift happened."

## Why the sink is created last and destroyed first

Cloud Logging starts exporting the moment a sink exists and keeps exporting for some minutes after one is deleted, because the Log Router holds sink configuration on a fleet that converges on its own schedule. An export that lands outside the window where the topic exists and the grant is in place mails an "[ACTION REQUIRED] Cloud Logging sink configuration error" to every principal holding `roles/owner` on the project. Two of the three orderings below are arranged around that; the middle one answers a different failure, an apply that stops before any sink exists:

- **On apply**, the grant names the Logging service agent as `service-<project-number>@gcp-sa-logging.iam.gserviceaccount.com`, derived rather than read from `google_logging_project_sink.drift_audit.writer_identity`. Reading it from the sink is what orders the grant after the sink; deriving it puts the grant first. `google_project_service_identity` asks Service Usage for the agent up front, since otherwise the first sink in a project is what creates it and the grant has nothing to bind. Enabling `logging.googleapis.com` is not enough on its own: in a project with the API on and no sink ever created, granting the role to that agent fails with "Service account … does not exist" until the Service Usage call is made. The call returns an _empty_ identity for Logging — no email — so it reads as though it achieved nothing; the minting is a side effect of making it.
- **Still on apply**, `time_sleep.logging_identity` holds for `logging_identity_propagation_duration` (60s by default) between that call and the grant. Minting the agent and being able to bind it are different moments: on a project that did not already have one, the grant run straight after the call fails with the same "Service account … does not exist", at about one project in five — six of 32 in one measured sweep. This is a timer rather than a poll because IAM reports a missing account and a not-yet-propagated one identically. It is paid on the first apply that carries this resource — the first apply of the module on a new install, and the next apply of any kind on an install that already had it, where the agent exists and the wait buys nothing — and after that only on an apply that re-mints the identity or changes the duration, the two keys in the wait's `triggers`.
- **On destroy**, `time_sleep.sink_drain` holds for `sink_drain_duration` (120s by default) between deleting the sink and removing the topic and the grant. Revoking publish early trades `topic_not_found` for `topic_permission_denied`, so both sit on the far side of the wait. Changing the duration takes an apply to land before the destroy that should honour it: `time_sleep` reads `destroy_duration` from state, since a provider's delete is handed prior state and no configuration. A caller that raises it and goes straight to `terraform destroy` waits the value already recorded.

  The drain covers the Log Router's propagation lag and nothing else, which is the whole of the problem only when the clusters being exported outlive this module's destroy. Where the same state owns a cluster, there is a second and larger source: the control plane keeps emitting matching audit records until it is gone, and if the topic is deleted first every one of them mails the owners. No value of `sink_drain_duration` fixes that — a teardown's length varies with the cluster, so a timer is racing it. The caller orders it instead, by depending on this module from whatever creates the cluster, so that Terraform destroys the cluster first. `full-install` carries that edge (`depends_on = [..., module.drift_pubsub]` on its cluster module) and [`tests/test_drift_pubsub_ordering.py`](../../../tests/test_drift_pubsub_ordering.py) pins it, because no plan shows a destroy order. Measured on a CI teardown before the edge existed: topic deleted at t+124s, last matching audit record at t+366s, with the error burst in the minute after the topic went.

None of the three closes its window completely. Google documents no bound on how long the Log Router takes to stop exporting, nor on how long a freshly minted service agent takes to become bindable, so both durations are chosen margins rather than measured convergence times. They take the email from every destroy to rarely, and the failed apply from one project in five to rarely. Renaming `topic_name` on a live install is a fourth case none of them covers: that replaces the topic under a sink which stays live and is only updated in place, and the drain does not participate because nothing is being destroyed.

Deriving the identity means a project where Logging returns some other writer identity would be granted the wrong principal and left with an inert sink. The sink carries a `postcondition` comparing the two, so that fails the apply naming both. A postcondition runs after the resource is created and does not roll it back, so the failed apply leaves the sink live and exporting as an identity that holds no publish role: `topic_permission_denied` on every export, and the owner-wide mail this section exists to prevent, now continuous rather than momentary. Deleting the sink stops it immediately, and granting the role by hand stops it without clearing the check. Because a postcondition is re-evaluated on later plans, such a project would also be unable to apply anything in the composition — `sink_writer_identity_override` is the way out, moving the grant and the check together onto the identity Logging reported. The full-install composition passes it through as `drift_pubsub_sink_writer_identity_override`, which is the name the error gives an operator who reached it from there; `sink_drain_duration` and `logging_identity_propagation_duration` are exposed the same way. None has an installer key, so an install driven by `install.sh` or `upgrade.sh` sets them as `TF_VAR_` passthrough lines in `install.env` — the front doors regenerate `terraform.tfvars` on every run, so an override added to that file by hand survives one apply and is dropped by the next, which puts the sink back in the state this paragraph describes. A hand-driven apply uses `terraform.tfvars`.

Deriving it also makes the grant's `member` a function of a data source, where reading it off the sink made it a function of a resource already in state. A caller that defers that read defers the member with it, and `member` is ForceNew: with `depends_on = [google_project_service.required]` on the module — which is how full-install calls it — any plan that adds or removes an API leaves `data.google_project.this` unread until apply, and the binding is planned for replacement while the sink stays live. That is this section's own window on another trigger, unfixed; setting the override pins the member and avoids it meanwhile. Narrowing the caller's `depends_on` to the APIs this module needs does not work, because Terraform resolves an indexed `depends_on` reference to the whole resource.

Three of the four links in that chain are `depends_on` edges, pinned by [`tests/test_drift_pubsub_ordering.py`](../../../tests/test_drift_pubsub_ordering.py); removing one is otherwise invisible, since no plan can show the ordering. The fourth — the wait after the Service Usage call — is a reference inside the wait's own `triggers` rather than an edge, so it shows up in a plan as a value and the module's `terraform test` suite pins it there, along with the second trigger key that makes a raised duration take effect.

## What this module does not do

- **It does not create a service account, and no longer grants to one.** The detector's GSA and its Workload Identity binding belong to [`kube-agents-iam`](../kube-agents-iam/); minting one here would produce a second identity for the same workload. This module used to take that GSA's email and grant it subscriber and viewer on the subscription, which was the odd half of the same rule — granting on an identity it does not own. Those two bindings are now the caller's, made against the `subscription_id` output.

  The reason for moving them is ordering rather than tidiness. Naming the GSA made this module a dependent of whatever creates it, and in `full-install` that is `kube-agents-iam`, which depends on the cluster. Terraform destroys dependents before dependencies, so the whole ingress — topic included — was torn down ahead of the cluster, while the control plane went on emitting matching audit records for minutes after the topic was gone, mailing the project's owners for each one. With no GSA input the module depends on nothing cluster-side, so a caller can order its cluster ahead of this module instead. `full-install` does exactly that, with `depends_on = [..., module.drift_pubsub]` on its cluster module.

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
  source     = "git::https://github.com/gke-labs/kube-agents.git//terraform/modules/drift-pubsub?ref=vX.Y.Z"
  project_id = "my-gcp-project"
}

# The detector's access to the subscription, which this module does not grant.
# Keep these outside the module and downstream of whatever creates the GSA:
# Pub/Sub IAM validates that a principal exists, so a binding that runs before
# the account does fails outright.
resource "google_pubsub_subscription_iam_member" "detector_subscriber" {
  project      = "my-gcp-project"
  subscription = module.drift_pubsub.subscription_id
  role         = "roles/pubsub.subscriber"
  member       = "serviceAccount:kubeagents-platform-gsa@my-gcp-project.iam.gserviceaccount.com"
}
```

A caller upgrading from a version that made those grants itself needs `moved` blocks for the two
bindings, or the first apply plans a destroy and create of the detector's own access;
`full-install`'s `main.tf` has the pair to copy.

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
on an install carrying real traffic, leave it empty. Never list the detector's own service
account — the agent would be writing the stream its own pod reads.

Lowering `ack_deadline_seconds` below its 60s default means passing the detector a matching
`--batch-join-budget`. The detector holds a whole batch while it reads live objects, and the two
values are not wired together — it reads this one at startup and warns when its budget takes more
than half of it, but it does not adopt it.
[The detector's README](../../../k8s-operator/cmd/drift-detector/README.md) is canonical for what
happens when the budget outlasts the deadline.

See the [Release versioning & promotion guide](../../../docs/site/src/content/docs/deploy/release-versioning.md) for SemVer pinning instructions.
