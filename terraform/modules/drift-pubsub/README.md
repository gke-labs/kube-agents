# Drift Audit-Log Pub/Sub Routing Module

Reusable Terraform module for provisioning the GKE audit log → Pub/Sub delivery path the drift detector consumes: the Log Router sink, the drift-audit topic and pull subscription, and the IAM bindings that let the sink publish and the detector subscribe — plus, where `topic_publishers` is set, publisher on the topic for each member it names, and, where `source_projects` names other projects, a sink in each of them routed into the same topic.

The detector cannot read audit logs from the Kubernetes API. On GKE the control plane is managed, so the API server's audit backend is not the operator's to configure and the stream surfaces only in Cloud Logging — hence a sink rather than an informer.

The sink's writer-identity grant is load-bearing: without `roles/pubsub.publisher` on the topic the sink is silently inert. Log Router raises no error, the topic receives nothing, and from the detector's side that is indistinguishable from "no drift happened."

## Why the sink is created last and destroyed first

Cloud Logging starts exporting the moment a sink exists and keeps exporting for some minutes after one is deleted, because the Log Router holds sink configuration on a fleet that converges on its own schedule. An export that lands outside the window where the topic exists and the grant is in place mails an "[ACTION REQUIRED] Cloud Logging sink configuration error" to every principal holding `roles/owner` on the project. Two of the three orderings below are arranged around that; the middle one answers a different failure, an apply that stops before any sink exists:

- **On apply**, the grant names the Logging service agent as `service-<project-number>@gcp-sa-logging.iam.gserviceaccount.com`, derived rather than read from `google_logging_project_sink.drift_audit.writer_identity`. Reading it from the sink is what orders the grant after the sink; deriving it puts the grant first. `google_project_service_identity` asks Service Usage for the agent up front, since otherwise the first sink in a project is what creates it and the grant has nothing to bind. Enabling `logging.googleapis.com` is not enough on its own: in a project with the API on and no sink ever created, granting the role to that agent fails with "Service account … does not exist" until the Service Usage call is made. The call returns an _empty_ identity for Logging — no email — so it reads as though it achieved nothing; the minting is a side effect of making it.
- **Still on apply**, `time_sleep.logging_identity` holds for `logging_identity_propagation_duration` (60s by default) between that call and the grant. Minting the agent and being able to bind it are different moments: on a project that did not already have one, the grant run straight after the call fails with the same "Service account … does not exist", at about one project in five — six of 32 in one measured sweep. This is a timer rather than a poll because IAM reports a missing account and a not-yet-propagated one identically. It is paid on the first apply that carries this resource — the first apply of the module on a new install, and the next apply of any kind on an install that already had it, where the agent exists and the wait buys nothing — and after that only on an apply that re-mints the identity or changes the duration, the two keys in the wait's `triggers`.
- **On destroy**, `time_sleep.sink_drain` holds for `sink_drain_duration` (120s by default) between deleting the sink and removing the topic and the grant. Revoking publish early trades `topic_not_found` for `topic_permission_denied`, so both sit on the far side of the wait. Changing the duration takes an apply to land before the destroy that should honour it: `time_sleep` reads `destroy_duration` from state, since a provider's delete is handed prior state and no configuration. A caller that raises it and goes straight to `terraform destroy` waits the value already recorded.

None of the three closes its window completely. Google documents no bound on how long the Log Router takes to stop exporting, nor on how long a freshly minted service agent takes to become bindable, so both durations are chosen margins rather than measured convergence times. They take the email from every destroy to rarely, and the failed apply from one project in five to rarely. Renaming `topic_name` on a live install is a fourth case none of them covers: that replaces the topic under a sink which stays live and is only updated in place, and the drain does not participate because nothing is being destroyed.

Deriving the identity means a project where Logging returns some other writer identity would be granted the wrong principal and left with an inert sink. The sink carries a `postcondition` comparing the two, so that fails the apply naming both. A postcondition runs after the resource is created and does not roll it back, so the failed apply leaves the sink live and exporting as an identity that holds no publish role: `topic_permission_denied` on every export, and the owner-wide mail this section exists to prevent, now continuous rather than momentary. Deleting the sink stops it immediately, and granting the role by hand stops it without clearing the check. Because a postcondition is re-evaluated on later plans, such a project would also be unable to apply anything in the composition — `sink_writer_identity_override` is the way out, moving the grant and the check together onto the identity Logging reported. The full-install composition passes it through as `drift_pubsub_sink_writer_identity_override`, which is the name the error gives an operator who reached it from there; `sink_drain_duration` and `logging_identity_propagation_duration` are exposed the same way. None has an installer key, so an install driven by `install.sh` or `upgrade.sh` sets them as `TF_VAR_` passthrough lines in `install.env` — the front doors regenerate `terraform.tfvars` on every run, so an override added to that file by hand survives one apply and is dropped by the next, which puts the sink back in the state this paragraph describes. A hand-driven apply uses `terraform.tfvars`.

Deriving it also makes the grant's `member` a function of a data source, where reading it off the sink made it a function of a resource already in state. A caller that defers that read defers the member with it, and `member` is ForceNew: with `depends_on = [google_project_service.required]` on the module — which is how full-install calls it — any plan that adds or removes an API leaves `data.google_project.this` unread until apply, and the binding is planned for replacement while the sink stays live. That is this section's own window on another trigger, unfixed; setting the override pins the member and avoids it meanwhile. Narrowing the caller's `depends_on` to the APIs this module needs does not work, because Terraform resolves an indexed `depends_on` reference to the whole resource.

Three of the four links in that chain are `depends_on` edges, and the source sinks below carry the same chain each, with a wait and a drain of their own; every edge is pinned by [`tests/test_drift_pubsub_ordering.py`](../../../tests/test_drift_pubsub_ordering.py), since removing one is otherwise invisible — no plan can show the ordering. The fourth link — the wait after the Service Usage call, the host's and each source project's — is a reference inside the wait's own `triggers` rather than an edge, so it shows up in a plan as a value and the module's `terraform test` suite pins it there, along with the second trigger key that makes a raised duration take effect.

## Exporting the scope's other projects

An install whose `PlatformAgent` declares a `spec.scope` discovers clusters in projects beyond the one it runs in ([`docs/designs/multi-project-scope.md`](../../../docs/designs/multi-project-scope.md)). Admin Activity audit logs are per project by construction — a project-level sink exports its own project's entries and nothing else — so each such project needs a sink of its own, and `source_projects` is that list. For every project it names, less `project_id`, the module creates the host's pieces again in the host's order: the project's Logging service agent minted up front, `roles/pubsub.publisher` on the host topic for that agent, derived from the project's number so that the grant precedes the sink, a `time_sleep` drain of that project's own, and a sink in that project whose destination is the host topic. The drain is per project rather than the host's because a source sink is destroyed on its own — the project removed from the scope or excluded — while the host's drain stays in the plan and a drain that stays waits for nothing; keyed on the project, it leaves with the sink it guards, so a shrink deletes that sink, waits, and only then revokes its grant, in the source project whose owners the mail would otherwise reach. One topic and one subscription, because the detector already routes each record by the project, location and cluster it names and reads the cluster through the Cluster Agent profile the reconcile wrote for it; what it joins is bounded by which projects' records reach the subscription, and these sinks are what decide that.

Each source sink is named `sink_name` with `project_id` appended (`source_sink_name` in the outputs), because a source project may hold a sink of another install's: its own, under `sink_name`, when it is that install's management project, or another install's source sink when two installs list it. Its filter is the host's shared clauses and the lease carve-out; `cluster_names` does not reach it, since those are bare names in `project_id` and a cluster of the same name elsewhere is another cluster. `source_sink_writer_identity_overrides`, keyed by project ID, is `sink_writer_identity_override` for a source project, with the same postcondition behind it and the same way out; its failure mails every owner of the _source_ project, and the message says which.

The full-install composition feeds `source_projects` from the projects its declaration lists at plan time — `scope.projects` and the selectors' members, each less an exact `exclude.projects` entry (`kube-agents-iam`'s `scope_export_projects`) — less its `drift_pubsub_source_exclude_projects`, the per-project way to keep reading a project's clusters without exporting its logs. A folder's or organisation's members are never among them, even while the scoped service account pool lists them: that listing is one Cloud Asset Inventory answer with no grace, and a member the index omitted for one plan would lose its sink under an auto-approved apply and its records until the next plan recreated it, with the apply green. A container's members are discovered at runtime and are not exported here; an aggregated sink on the folder or organisation would cover them in one piece and is its own design, since Logging documents no writer-identity form for such a sink, so its grant could not precede it, and `include_children` would export every project under the container, excluded ones included.

The identity applying this needs, in each source project, `resourcemanager.projects.get` to read the number, the Service Usage call that mints the project's Logging service agent, and `logging.sinks.create`; `roles/owner` carries all three, and the identity that bound the scope's read roles there ordinarily does. Nothing in the composition probes for them beforehand, and what a missing one costs depends on which. A number that cannot be read fails the plan when the plan can make the read, and otherwise the apply at that project's read, with the host's pieces and everything independent of it already created: the composition's `depends_on` defers the module's data sources whenever a required API changes, the first install included, as the section above says of the host's own read. An agent that cannot be minted stops the apply with the other projects' agents in place and every source grant and sink held back; a grant that cannot be made leaves the other grants in place and every source sink held back (each grant waits on all of the agents and each drain on all of the grants, since `depends_on` orders whole resources, not instances); a sink that cannot be created stops at that project alone. The host's pieces are untouched in every case. The next apply after the permission is granted, or the project dropped, creates what was held back.

A source project's `logging.googleapis.com` has to be enabled for its sink and its service agent; every project has it on unless someone turned it off, and nothing here enables it there — the composition enables APIs in `project_id` alone.

What the sinks are to the detector is also what bounds who can write to it. On an install the sinks are the topic's only publishers, and `unique_writer_identity` makes each publish as its project's Logging service agent — per project, not per sink — so whoever can create a sink in a project whose agent holds publish on the topic can route entries of their choosing into it. Without a scope that is the host project's `logging.sinks.create` holders; with one it is theirs in every source project too. The detector classifies on the principal inside each record, so that is the set of people who could make it report a change nobody made, the same caution the composition's README gives for `topic_publishers`.

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

Two more follow from the ordering above, and `full-install` already satisfies both; the section on the scope's other projects says what a project listed in `source_projects` needs on top. The module reads the project number, so `cloudresourcemanager.googleapis.com` must be enabled and the applying identity needs `resourcemanager.projects.get`; and it mints the Logging service agent through Service Usage, so it takes a `google-beta` provider configuration from the root.

## Usage

```hcl
module "drift_pubsub" {
  source                         = "git::https://github.com/gke-labs/kube-agents.git//terraform/modules/drift-pubsub?ref=vX.Y.Z"
  project_id                     = "my-gcp-project"
  detector_service_account_email = "kubeagents-platform-gsa@my-gcp-project.iam.gserviceaccount.com"
}
```

`source_projects` defaults to empty, which exports `project_id` alone. Name the projects an install's scope lists to export theirs into the same topic, one sink each:

```hcl
  source_projects = ["corp-payments-stage", "corp-analytics-prod"]
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

`topic_publishers` defaults to empty, which leaves the sinks' writer identities — the host's and each source project's — as the topic's only
publishers — the shape the sections above assume. Each member listed here takes
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
