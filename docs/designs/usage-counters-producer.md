# Producing the PlatformAgent Usage Counters

**Status:** implemented. The facts table below is the tree as read on 2026-10-01, before the poller; the sections after it describe what ships.

## Summary

`PlatformAgent.status.usage` declares cumulative counters, `sessionsTotal`, `eventsIngestedTotal`,
`toolExecutionsTotal`, `remediationsProposedTotal` and `remediationsAppliedTotal`, and a
`lastActiveTime`, and until the poller this document describes nothing wrote them. The schema shipped that way on purpose: the agent's
ServiceAccount holds no write verb on the status, and the operator, which does, saw no session,
event or tool call. Two of the counters now have an in-cluster source. The credential broker
serves `kubeagents_tool_invocations_total` on its metrics-only listener, and the event watcher
serves `k8s_event_watcher_events_injected_total` on the gateway pod's `agent-api-auth` sidecar,
both for the managed-Prometheus collector.

This document settles how the operator turns those series into the status fields: a poller
that runs on the leader off the reconcile path, scrapes the two endpoints over a NetworkPolicy
rule that admits the operator's pods and nothing else new, accumulates per-pod deltas into
totals it keeps beside their baseline in a ConfigMap, so the counters stay monotonic across pod,
process and operator restarts, and patches the status at most once per interval, only when the
status is behind. `toolExecutionsTotal`, `eventsIngestedTotal` and `lastActiveTime` land this
way. `sessionsTotal`, `remediationsProposedTotal` and `remediationsAppliedTotal` stay unwritten
until a series exists for each, and the last section says what that series is.

## What was verified

Read from `main` and, where marked, observed read-only on a running install on 2026-10-01.
These are the facts the design rests on.

| Fact                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      | Where                                                                                                                                                                                                                           |
| ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| The watcher's listener is port 9095, named `event-metrics`, on `agent-api-auth`, which the gateway pod runs as a native sidecar: an entry in `initContainers` with `restartPolicy: Always`, not in `containers`. The broker's is port 8766, named `cred-metrics`, on the `envoy-credential-proxy` container. Both are container ports, not Services, and both bind every interface.                                                                                                                                       | `platformagent_manifests.go` (`eventWatcherMetricsPort`, `credentialProxyMetricsPort`), `credential_proxy_manifests.go` for the broker container; `start-services.sh`, `credential_proxy.py` for the bind; the golden manifests |
| Each pod's NetworkPolicy admits its metrics port from one peer: pods in the `gke-gmp-system` namespace. The gateway policy's first rule admits every pod in the agent's namespace, but on the API ports and, when the dashboard is enabled, its port only.                                                                                                                                                                                                                                                                | `buildNetworkPolicy`, `buildCredentialProxyNetworkPolicy`; live: the gateway policy's 9095 rule names that one namespace                                                                                                        |
| The operator's pod carries `app.kubernetes.io/name: kube-agents-operator` in both install paths: the chart's `operatorSelectorLabels` and the kustomize manager manifest.                                                                                                                                                                                                                                                                                                                                                 | `_helpers.tpl`, `config/manager/manager.yaml`; live: the running operator pod                                                                                                                                                   |
| The operator reads its own namespace from the ServiceAccount namespace file and its own Pod through the API reader to discover its image, in a branch the chart's `OPERATOR_IMAGE` skips, and keeps neither; the policy renderers are pure functions of the CR and the start-up flags, and the golden tests render them with no pod around. For its own callout the operator already renders a Downward-API `POD_NAMESPACE` on a container it manages.                                                                    | `cmd/main.go`, `platformagent_a2a_callout.go`, `internal/testing`                                                                                                                                                               |
| The gateway Deployment has one replica by default; `spec.deployment.availability.replicas` renders more, with a leader-election wrapper that points the Service at one pod, and the HA fixture renders three. Every gateway pod runs the sidecar and its watcher; nothing gates the watcher on leadership. The broker Deployment has one replica and rolls with `Recreate`.                                                                                                                                               | the golden manifests, `start-services.sh`; live: both at 1                                                                                                                                                                      |
| `spec.harness.eventWatcher.enabled=false` makes the entrypoint start no watcher, while the container port and the collector's rule render regardless, so nothing listens on `event-metrics`; the reconciler resolves the switch through `eventWatcherEnabled` for the `EventWatcher` condition.                                                                                                                                                                                                                           | `deploy/shared/start-services.sh`, `platformagent_manifests.go`, `platformagent_controller.go`                                                                                                                                  |
| The entrypoint supervises the watcher in a retry loop and restarts it in place after any exit, in the same container and pod, so the watcher's counters reset more often than its pod does.                                                                                                                                                                                                                                                                                                                               | `deploy/shared/start-services.sh`                                                                                                                                                                                               |
| `k8s_event_watcher_events_seen_total` increments on every delivery the informer makes, before the reason filter: that includes the initial list, which replays every Event still inside the API server's retention, and the redeliveries of watch-connection rotations. `k8s_event_watcher_events_injected_total` increments once an event has passed the filter and the dedup cache, has been handed to the agent, and was neither graded informational nor dropped on the daily ceiling by the daemon that received it. | `cmd/k8s-event-watcher/main.go` (`Dispatch`), `watcher.go`, the watcher's README ("Replay Shielding", "On-Disk Snapshots")                                                                                                      |
| The dedup caches are snapshotted to the data volume, under `event-watcher/` in the agent's home (`/opt/data` unless `spec.harness.hermes.agentHome` moves it), every thirty seconds and at shutdown, and restored at start, so a restart re-offers nothing already triaged; a lost or unreadable snapshot costs one replay of the retained Events.                                                                                                                                                                        | the watcher's README; `platformagent_manifests.go` (the sidecar writes its snapshots to the shared volume)                                                                                                                      |
| The watcher's `k8s_event_watcher_session_creates_total{…,outcome}` records `outcome` as `ok` or `error`. The broker's series is `kubeagents_tool_invocations_total{tool,subcommand,status}` with `status` one of `success`, `error`, `blocked`, `busy`, `abandoned`; `abandoned` is recorded both for a running command killed when its caller left and for a caller that left the queue before the command started.                                                                                                      | `cmd/k8s-event-watcher/main.go`, `credential_proxy.py`                                                                                                                                                                          |
| The gateway pod's containers share one network namespace, `spec.deployment.sidecars` renders any container a CR author names into that pod, and the watcher holds 9095 only while its process runs: when the port is taken it logs an ALERT and runs on without a listener. The manager runs under a 128Mi memory limit in both install paths.                                                                                                                                                                            | `platformagent_manifests.go`, `common_types.go`, `cmd/k8s-event-watcher/main.go`, `config/manager/manager.yaml`, `charts/kube-agents/values.yaml`                                                                               |
| The operator's ClusterRole lists pods and manages ConfigMaps, cluster-wide grants the poller uses in the agent's namespace; it patches the status subresource of other kinds already (`Status().Patch` on `AgentPlugin`).                                                                                                                                                                                                                                                                                                 | `config/rbac/role.yaml`, `platformagent_controller.go`                                                                                                                                                                          |
| The controller `Owns` ConfigMaps with no predicate: a write to any ConfigMap whose controller owner is the CR re-enqueues the CR. The `PlatformAgent` watch itself carries no predicate either, so every status write re-enqueues the CR.                                                                                                                                                                                                                                                                                 | `platformagent_controller.go` (`SetupWithManager`)                                                                                                                                                                              |
| Neither listener exports `process_start_time_seconds`: the watcher registers its counters on a registry of its own with no process collector, and the broker renders its exposition by hand. The Go client's process collector would not do as the source: it derives the start time on every scrape from `/proc/stat`'s `btime`, which moves by a second when the node's wall clock is stepped against its monotonic clock, so an unchanged process would read as restarted.                                             | `cmd/k8s-event-watcher/metrics.go`, `credential_proxy.py`                                                                                                                                                                       |
| On an upgrade the operator moves before the harness, `--upgrade-mode=operator` moves it alone, and the harness and broker images can be pinned behind it (`spec.deployment.image`, `CREDENTIAL_PROXY_IMAGE`, the chart's tag), so the poller will meet listeners from the previous release on every ordinary upgrade.                                                                                                                                                                                                     | the site's upgrade page, `manifest_helpers.go` (`resolveAgentImage`), `platformagent_manifests.go`                                                                                                                              |
| Kubernetes' default `edit` and `admin` ClusterRoles grant write on ConfigMaps and nothing on `platformagents` or their status; the agent's own ClusterRole holds get, list and watch on ConfigMaps. A namespace ConfigMap the operator reads back today, `<name>-gitops-state`, names its trusted writer and validates its content on every read; the minter's is another. The manager has no HTTP client of its own; the module's clients belong to the watcher and the drift detector binaries.                         | `platformagent_manifests.go` (the agent's role), `platformagent_controller.go` (`parseManagedRepos`), `charts/kube-agents/values.yaml`                                                                                          |
| The watcher's injected family and its siblings carry `cluster`, `project`, `location`, `reason` and `namespace`, no series is ever deleted, and under fan-in that product is multiplied by the cluster count, which is the stated reason the namespace label was dropped from the observed family. `expfmt`'s text decoder reads the whole input on its first decode; only the protobuf-delimited format streams, and the broker renders text.                                                                            | `cmd/k8s-event-watcher/metrics.go`, `github.com/prometheus/common/expfmt`                                                                                                                                                       |
| `github.com/prometheus/common`, which holds the text-format parser (`expfmt`), is already in the operator's module graph as an indirect dependency; `google.golang.org/api` is a direct one.                                                                                                                                                                                                                                                                                                                              | `k8s-operator/go.mod`                                                                                                                                                                                                           |
| The Ready writer gates its `Status().Update` on `status.usage.activeInterfaces` and keeps a per-CR record, `prunedUsageStatus`, of a served CRD that drops `status.usage`; it re-probes every `usageStatusReprobeInterval` (5 minutes).                                                                                                                                                                                                                                                                                   | `platformagent_controller.go` (`noteUsageStatusEcho`, `usageStatusPruned`)                                                                                                                                                      |
| The RBAC self-check is a manager `Runnable` on its own ticker, added in `main.go`, with `NeedLeaderElection` false because it is about the pod's own permissions.                                                                                                                                                                                                                                                                                                                                                         | `rbac_selfcheck.go`                                                                                                                                                                                                             |

## The decision

A `UsageCounterPoller`, a manager `Runnable` beside the RBAC self-check, with
`NeedLeaderElection` returning true: the counters are per cluster, so exactly one operator
replica advances them. One interval after the leader's election, by which time the first
reconcile pass has rendered the rules that admit the operator, and then every
`usageCountersPollInterval` (five minutes, the
interval the controller already uses for the RBAC re-probe and the pruned-status re-probe, on
the same reasoning: one status write per interval is a cost nobody notices) it lists the
`PlatformAgent`s from the cache and, for each:

1. lists the pods the two policies select, in the CR's namespace: `app: <name>-gateway` for the
   gateway, and for the broker the two labels `credentialProxySelector` returns,
   `app: <name>-credential-proxy` and `kubeagents.x-k8s.io/component: credential-proxy`. When
   `eventWatcherEnabled` is false for the CR, the gateway pods are left out: the entrypoint
   starts no watcher, so nothing listens on `event-metrics`, and a refused connection there
   would be the install's choice, not a failure;
2. for each running pod, finds the container port by name, `event-metrics` on the gateway pod
   and `cred-metrics` on the broker pod, looking through `initContainers` as well as
   `containers` because the sidecar is a native one, and reads `/metrics` at the pod IP and that port, joined with
   `net.JoinHostPort` so an IPv6 pod IP works, with a short deadline. The body is not parsed
   whole: the watcher's exposition grows with the fleet, clusters times the namespaces that
   ever raised a warning times the reasons, per family, for the life of the process, so a
   ceiling on the body would be a bound sized against no population, and `expfmt`
   materialises every family of whatever it is handed. The reader scans the body line by
   line and keeps none of it: a line of the two families the design wants, or of the start-time
   gauge, is parsed on its own with `expfmt` and its sample folded into the running per-pod
   sum as it is read, and every other line is skipped unread. The one bound is
   `usageScrapeMaxLineBytes` per line, and a line past it is a failed scrape; a bound on the
   number of lines would be a bound on the kept family's cardinality, which is the product the
   sentence above says cannot be sized, and a fleet that crossed it would freeze the counter
   until the watcher restarted. Memory per poll is one line whatever the fleet's cardinality,
   so no honest fleet size is a failed scrape. The port is held by a listener in a pod that runs other
   containers, so what answers is input, not the operator's own data. The client follows no redirect (`CheckRedirect` returns
   `http.ErrUseLastResponse`), runs on a transport with no proxy, since a pod-network scrape
   never has one and the Go default would send the GET to an `HTTP_PROXY` the operator's
   environment sets, and any status other than 200 is a failed scrape, so a body on
   the port cannot send the operator's GET, made from a network position the pod's own egress
   policy does not have, anywhere else. Selecting the port by its name means a renumbering in
   the manifests moves the scrape with it;
3. sums the series it wants over every label set as the lines are folded (the next
   section says which), and takes one sample per pod per counter. A sample that is negative or
   not finite is a failed scrape for that pod: it contributes nothing, and the log line names the
   pod. What a body may add to a total in one poll is bounded in the resets section, and that
   bound, not the sample, is the one that matters;
4. folds the samples into the totals through the per-pod baseline described below. Totals and
   baseline live together in one ConfigMap, which is the accumulator's source of truth; the
   status is a projection of it;
5. writes the ConfigMap when a total or the baseline changed, recording with the totals the
   time of the poll that moved them, then patches `status.usage` when the status is behind the
   ConfigMap, copying its totals and that time as `lastActiveTime`. The order, ConfigMap first,
   is deliberate; the resets section says why.

Nothing in `Reconcile`'s accounting changes: the reconcile loop keeps writing `activeInterfaces`
through the Ready writer as it does today, the poller never touches that field, and the Ready
writer never computes a counter. What the loop renders does change, by the two policy rules and
the `POD_NAMESPACE` input the Reach section owns, and the one piece of code the two writers
share is the echo check in the served-CRD section, generalised so that both call it.

## Counter sources

| Status field          | Series                                                       | Aggregation                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| --------------------- | ------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `toolExecutionsTotal` | `kubeagents_tool_invocations_total`, broker pod              | Sum over `tool` and `subcommand`, over `status` in `success` and `error`: the commands the broker ran to an exit. `blocked` and `busy` are refusals that never ran. `abandoned` is left out because the series cannot say whether the command had started: the broker records it for a command killed mid-run and for a caller that left the queue before the start. `error` also covers a rejected request and a broker fault, so the count is the broker's view of "ran", a little wide. |
| `eventsIngestedTotal` | `k8s_event_watcher_events_injected_total`, every gateway pod | Within a pod, sum over every label: cluster, project, location, reason and namespace. Across gateway pods, the largest per-pod delta in the poll rather than the sum: each replica's watcher works the same event stream, so the sum would count an event once per replica. With one pod the two are the same.                                                                                                                                                                             |
| `lastActiveTime`      | derived                                                      | The time of the last poll in which any total moved: a command ran, or an event was accepted for triage. The field's documented meaning is the most recent interaction or event triage; until `sessionsTotal` lands, a chat turn that runs no brokered command does not move it, and the CRD description the implementation ships says so.                                                                                                                                                  |

`eventsIngestedTotal` counts the events the watcher accepted for triage, past its reason filter
and its dedup window and not turned away by the daemon, not the events it observed. The observed series,
`k8s_event_watcher_events_seen_total`, is the wrong source for a cumulative status field for two
reasons. It counts the informer's initial list, which replays every Event the API server still
retains, and the redeliveries of watch rotations; the watcher restarts in place more often than
its pod, so every restart would add the fleet's retained Event volume to the status under the
"a new process starts from zero" rule below, and no monotonicity check would catch it, because
the counter only ever rises. And it moves with routine cluster churn, scheduling, pulls, probe
warnings, on every watched cluster, so `lastActiveTime` would never rest and would stop meaning
what the schema says. The injected series sits behind the dedup cache, whose snapshot persists
on the data volume, so a restart re-injects nothing already triaged; when a snapshot is lost the
replay is injected again, and the counter counts that when it fits within the per-poll ceiling the resets section
sets; a replay larger than the ceiling is not counted for that interval, and the counter
resumes from the sample it carried. The field's
description changes from "ingested and evaluated" to say "accepted for triage", and the observed
volume stays where it is, in Prometheus.

The largest delta is an estimate when replicas disagree: a replica that started mid-interval
has seen fewer events than one that was up throughout, and the largest is the closest to the
number of distinct events. During a rollout, when old and new pods overlap, the per-pod reset
handling below runs first, so a new pod's whole sample competes with an old pod's difference
and the larger wins, which is still the better estimate of the two. A replica whose entry is behind
its sibling's marker when it next advances is reset without adding and takes the sibling's
marker, as the resets section says, so one straddle does not cost the next lone advance, and
that rule reads the stored markers alone, so it holds across a leader change. It
makes the layout under-count rather than over-count: informer skew that straddles a poll
boundary, one replica ahead at one poll and the other catching up by the next, drops the
catch-up instead of counting it twice, and a replica's events its sibling did not inject, a
sibling whose daemon dropped them on its daily ceiling or whose dedup snapshot was kept while
this one's was lost, are lost whenever this replica was quiet for a poll in which the sibling
moved. That is the error this document tolerates, on a layout that is opt-in and in a series
that carries no replica attribution; the fix, if it ever matters, is a watcher that counts only
on the leader, not a smarter maximum. The broker runs one pod, so its series needs no such
rule.

## Reach: how the operator gets to the endpoints

The scrape is a direct read of the pod IP, and before the poller the two policies admitted the
metrics ports from the collector's namespace alone. The implementation adds one ingress rule to each policy,
the same shape as the collector's rule beside it: the operator's namespace by its
`kubernetes.io/metadata.name` label, and within it pods with
`app.kubernetes.io/name: kube-agents-operator`, on the metrics port only. The operator renders
these policies, so it can write the rule once it is told its own namespace, which until the
poller it was not: the namespace-file read in `main.go` serves image discovery, runs only when the chart has
not set `OPERATOR_IMAGE`, and keeps nothing, and the renderers take the CR and the start-up
flags alone. Both install paths therefore set a Downward-API `POD_NAMESPACE` on the manager
container, the pattern the operator already renders for its own callout; `main.go` reads it once
and hands it to the reconciler beside the other render inputs, and the golden tests fix it to a
constant so the rendered policies stay deterministic. When it is unset, as under `make run` off
the cluster, the rule is omitted, one start-up line says so and the poller is not started; nothing off the cluster could
reach a pod IP in any case. The label is the one both install paths put on its pod. A
deployment that relabels the operator pod breaks the scrape and nothing else; the failure
section says how that shows.

The rule is narrower than the obvious alternative of admitting the agent's namespace, which
would open both listeners to the shell sandbox and to anything else that lands in the
namespace. The listeners serve counters whose labels are tool, subcommand and status names on the
broker's side and cluster, project, location, namespace and reason names on the watcher's,
nothing secret, but the broker's metrics listener was admitted past the broker's own
reachable-off-pod refusal on the argument that it serves counters to a collector; a pod
selector keeps that argument true.

Two documents stated the pre-poller peer set as a security property rather than as a description:
`docs/security-requirements.md` and `docs/credential-isolation-design.md` both said the broker
opens 8766 to the collector's namespace and to no other peer. The rule here adds one peer, and
the implementation rewrites those sentences to say so, with the pod selector as the reason the
property holds in substance: the listener reaches the collector and the operator, both readers
of counters, and nothing else.

The operator's own egress is not restricted by any policy this repository renders, so no rule is
needed on its side.

## Resets and the state that keeps the counters monotonic

A Prometheus counter is the life of one process. The broker's restarts with its pod and with
its container; the watcher's with the gateway pod, with its sidecar container, and with every
restart the entrypoint's supervisor performs in place. A status counter that copied the sample
would fall back to zero on every one of those. The poller therefore keeps, per CR, a baseline:
the last sample it took from each pod, keyed by pod UID, per counter. On a poll it adds, for
each pod it scraped:

- the difference from the pod's last sample, when the pod UID is known, the body's start time
  is the recorded one, and the sample is not below the last one;
- the whole sample, when the pod UID is new, which means created after the document was first
  recorded and not merely absent from it (a new pod starts from zero, so everything it has
  counted is new), unless a live gateway sibling's marker is later than the pod's creation,
  in which case the sibling supplied the events this pod injected in the meantime and the pod
  is recorded at its sample with the sibling's marker, adding nothing, as a known replica behind
  its sibling would be; or the body's start time is present and later than the recorded one
  (the process restarted inside the same pod);
- nothing, when the body carries a start time earlier than the recorded one, or shows the
  sample falling under an unchanged start time: a counter cannot fall inside one process, so
  each of those is a body that is not the listener's. The body is refused, and because it
  parsed, the baseline advances to the sample and start time it carried: a refusal that kept
  the baseline would be repeated on every poll until the pod restarted, since the next body
  would carry the same fall;
- for a body that carries no start time, which is what every listener from a release before
  this one sends, the two rules above without the start time: the difference when the sample
  is not below the last one, the whole sample for a new pod UID, and a refusal that advances
  the baseline, as above, when the sample fell, so an in-place restart of such a listener
  costs one interval and not the rest of the pod's life. The stricter rule applies from the first
  body that carries the gauge, and the upgrade order makes that gap routine, as the failure
  section says; a body without the gauge for a pod whose entry already records a start time is
  not the listener's, since a listener does not lose the gauge inside one pod, and is refused
  with the baseline advanced, as above; and the first body that carries the gauge for an entry
  that recorded none is a process that started, a container restarted in place onto a newer
  image, so an absent recorded start time counts as earlier and the body takes the whole-sample
  branch under the ceiling;
- in every adding branch, at most `usageDeltaCeiling` per pod per counter per poll, a named
  bound sized to what a listener could plausibly count in one interval rather than in a pod's
  lifetime, and the same one ceiling whatever the gap since the pod was last counted: a gap's
  delta past one ceiling, after an operator outage of several intervals, is refused whole, the
  baseline advanced and that gap's count lost once, which is the under-count this document
  prefers, and an allowance scaled to the gap would be one the stored marker
  cannot compute, because a quiet poll does not move it, so after an idle stretch one body
  could claim the whole stretch's allowance at once. A body whose addition would exceed the
  ceiling is refused and, because it parsed, advances the baseline to the sample it carried and
  adds nothing: an
  honest burst past the ceiling, which the sizing makes rare, costs that interval's count and
  nothing after it, and a pod the ConfigMap has never seen is always recorded, whatever its
  sample, rather than refused on every poll until it restarts;
- nothing, when the scrape produced no body to read: a connection that failed, a status other
  than 200, a line past its bound or one of the wanted families `expfmt` could not parse, or a
  sample that is negative or not finite. Only then is the baseline entry kept; a body that parsed and was
  refused advances it, as the branches above say, which is the line between a scrape that said
  nothing and one that said something the poller will not count. The document records no
  "missed", only the marker, and a quiet pod and a missed one look the same in it; so the rule
  that keeps a gap from being counted twice reads the markers alone, on every poll: a gateway
  pod whose entry is behind another gateway pod's marker when it next advances is reset to its
  sample and adds nothing, because the largest-delta rule below would otherwise count what the
  sibling already supplied. The reset sets its marker to the sibling's, not to the current
  poll: set to the current poll, each reset would leave the sibling behind in turn, and every
  lone advance by either replica would be reset until both advanced together, losing whole
  bursts that neither counted; only an add moves a marker past a sibling's. For a known UID the
  comparison runs before either adding branch: a replica whose entry is behind a sibling's
  marker is reset whether its body is an advance or a restart with a later start time, recorded
  at its new sample and start time, taking the sibling's marker and adding nothing, because the
  events its new process replayed are the ones the sibling already supplied. In a poll where
  both replicas moved, the one whose delta the total took moves its marker, and so does a
  replica whose delta equalled it, since both injected the same events and neither has a
  catch-up pending; a replica whose delta was smaller moves its baseline to its sample, so its
  delta is not re-presented, but its marker stays, so its later catch-up is reset rather than
  counted on top of what the total already took from its sibling. The broker's one pod, and a single gateway pod, have no sibling and
  always add the difference across a gap. The error this leaves is an under-count, named in the
  sources section: a replica's events that its sibling did not inject are lost whenever it was
  quiet, or missed, for a poll in which the sibling moved; and a replica reset in a poll in
  which its sibling was taken keeps the sibling's marker as it was read, one poll behind, until
  it advances in a poll the sibling does not, so its events in a poll the sibling was missed are
  lost too. A terminating pod is never read again, so it is not live: its entry is dropped and
  its marker suppresses no sibling, which is what keeps a rollout from losing the new replica's
  intervals. A reset that took the sibling's marker after the poll would close the missed-sibling
  loss as well and open an over-count instead, a replica trailing its sibling by one poll having
  its catch-up counted whenever the sibling is quiet, and the under-count is the one preferred.

Entries for pods that no longer exist are dropped when the baseline is next written; their
counts are already in the totals.

The start time is why both listeners gain `process_start_time_seconds`, as a gauge each
captures once when its process starts, `time.Now()` at `main` and nothing re-derived after, so
it is constant for the life of the process by construction; the Go client's process collector
is not used for it, because it recomputes the value from the kernel's boot time on every
scrape and a stepped wall clock moves that by a second, which the rule below would read as a
restart in one direction and a forgery in the other. A sample that fell is not the only sign of a restart: a process that
restarts and overtakes its last sample inside one interval reads as a plain increase, and
without the start time the counts it made up to its last sample would be lost, an under-count
bounded by that sample per restart, silent, and most likely on exactly the busy install where
the watcher's supervisor restarts it under load. With the start time the reset is seen whatever
the sample did, and the rule demands positive evidence of one: reading any sample below the
last as a reset would let a body answering on the port between the watcher's restarts hand the
poller a small sample, have it kept as the new baseline, and have the real listener's next
sample added whole on top, the pod's lifetime count a second time per take-and-release cycle.
The evidence rule alone does not close that door, because a forged start time later than the
recorded one is evidence too; the per-poll ceiling is what sizes the damage. Under the rules
together, a body that is not the listener's can add at most `usageDeltaCeiling` to a counter in
a poll, whatever start time or sample it carries, and can keep the real listener's bodies
refused until the pod restarts, the denial this document already accepts; it cannot set a
total, and a forged earlier start time or a falling counter is refused on sight. That residual,
one ceiling per poll from a workload a CR author put in the pod, is stated in the security
section rather than designed away, because closing it means authenticating the listeners.

The totals and the baseline live together in a ConfigMap, `<name>-usage-counters`, in the CR's
namespace, holding one JSON document: the time the document was first recorded, the running total per
counter, the time of the last poll that moved a total, and per pod UID the pod's name for a reader, its last sample per counter,
its last start time, and the poll at which its entry last supplied a delta the total took,
which for the broker's one pod and a single gateway pod is every poll it advanced, or the
sibling's marker it was last reset against, which is not the poll it was last read at: a quiet
poll changes nothing in the document, so a quiet install writes nothing, and the marker still
tells the missed-pod rule whether another pod was counted during a gap. The ConfigMap is the source of truth the
accumulator reads at the start of every poll; the status is written from it, never the other
way round, so a cache that hands the poller a stale CR can never pull a total backwards. It
carries an owner reference to the CR, without the controller flag, so it is collected with the
CR but does not re-enqueue it: the controller `Owns` ConfigMaps with no predicate, and a
controller-owned one would cost a reconcile on every write. The document also records the UID
of the CR it was accumulated for, and the poller compares it, and the owner reference's UID,
with the CR's. A mismatch is treated as absent and overwritten: it is what a CR deleted and
re-applied under the same name sees while the collector has not yet removed the old ConfigMap,
or was kept from removing it, and without the check the new CR would inherit a predecessor's
totals, through the status patch that fires whenever the status is behind. A name is not
ownership; the finalizer applies the same rule to the data volume. What the poller reads back
is bounded before it is used, with a bound of its own for the totals, which honestly outgrow
any per-pod or per-poll figure: every sample non-negative and finite, every total non-negative,
finite and below the `int64` headroom the status field has, no total below the status it
projects to, and the first-recorded time no later than the poll reading it and no earlier than
the CR's own `creationTimestamp`, since a time in the future would quietly make every pod old
and a time in the past would make a missed pod new; a document that fails any of them is
treated as absent, and the seed-from-status path runs with this poll's time as the
first-recorded time. It is written only in a poll in
which a total or the baseline changed, so a quiet install writes nothing.

In memory alone the baseline would be lost with the operator, and the first poll after a
restart would see every pod as new and add its whole sample again, counting every command and
event since those pods started a second time; the totals, kept in memory, would start from zero
and the status would fall back. With the ConfigMap, an operator restart loses nothing.

A poll that finds no ConfigMap has one rule, whatever the status says: it records every pod's
current sample as its baseline and adds nothing, and it seeds the totals from the status when
the status carries counters. When the status carries them, something removed the state after
the counters had been written, and the poll under-counts whatever happened between the last
written poll and this one, once, rather than over-counting everything the pods have ever done.
On a fresh install, or the first poll after the upgrade that brings the poller, the status
carries nothing and the counters start at zero from this poll: a pod older than the poller has
a history the counters never saw, and whether it fits the ceiling says nothing about whether
it should be counted. The whole-sample branch therefore applies only to a pod created after the document was
first recorded, where "a new pod starts from zero" is true: the document carries the time it
was first recorded, and a pod's `creationTimestamp` from the list in step 1 is compared with
it. Every poll that treats the document as absent, the first poll and each re-seed after a
mismatch or a failed bound, records its own time there rather than carrying a predecessor's
forward: "a new pod starts from zero and none of it was counted" holds only for pods created
after the last point at which every pod was re-baselined, and a time carried forward would
make a pod the re-seed could not scrape "new" on its next scrape, with its lifetime counted
once more on top of totals that already hold it. Absence from the document is not newness: a pod
the first poll could not scrape, or that one of the two policies kept out while the other
already admitted the operator, is recorded on its next scrape and adds nothing. `lastActiveTime`
is first stamped by a poll in which something ran. What a fresh install loses is the count
between each pod's start and the first poll after it, the under-count this document prefers
everywhere else.

The ConfigMap is written before the status. A crash between the two leaves the status one poll
behind the totals, and the next poll repairs it, because the status patch is issued whenever the
status is behind the ConfigMap, not only when this poll moved a total; the repair stamps the
time the ConfigMap recorded, not its own, so `lastActiveTime` says when the counters last moved
rather than when the status caught up. That time is in the ConfigMap for this reason: in the
poller's memory alone it would die with the process that took it. The other order would
leave the totals behind the status after a crash, and the next poll would add the interval's
deltas a second time. Under-counting until the next poll is the error this document prefers
everywhere; an over-count is permanent.

## Write cadence and the status writers

The status is written with `Status().Patch` and a merge patch from the CR as read, touching only
the counters and `lastActiveTime`, at most once per poll and only when the status is behind the
ConfigMap's totals. Every status write re-enqueues the CR through the unfiltered `PlatformAgent`
watch, which is why the write is bounded by the interval and never issued from `Reconcile`: a
busy install costs one reconcile per five minutes, and the ConfigMap's non-controller owner
reference keeps it at one rather than two; a quiet one costs none.

The Ready writer and the poller write the same subresource from different goroutines. The
Ready writer's `Update` carries the counters as it read them, so it never zeroes a field the
poller wrote; when the two cross, the API server's resource-version check rejects the later one
and controller-runtime retries that reconcile, as it does for any conflict. The poller's patch
carries no resource version and cannot be rejected that way; it can only land on top of a Ready
write, which is fine, because the fields are disjoint and the values come from the ConfigMap,
not from the CR the poller read.

## The served-CRD skew

A served CRD older than this release prunes `status.usage` on every write, and the Ready writer
already stops gating on `activeInterfaces` while its `prunedUsageStatus` record for the CR is
fresh, probing again after the interval. The poller consults the same record and skips the
status patch while it is fresh, so an operator running ahead of its CRD costs one probe per
interval in total, not one per writer. The ConfigMap is still written on every poll that moved
a total, so nothing is lost during the skew: when the CRD is applied, the next patch carries the
totals accumulated since the operator was upgraded, not since the last interval, and the time
the last of them moved. After its own
patch the poller reads the echo the same way `noteUsageStatusEcho` does: counters it wrote that
come back absent mean the pruning, recorded in the shared map; counters that come back clear it.
The echo check moves from a function that knows about `activeInterfaces` to one that takes
whether the fields a writer wrote came back, and both writers call it.

## Failure behaviour

The schema has no field for an error, by design: static enums and integer counts only. An
install that switched the watcher off is not a failure: the poller reads the same switch the
reconciler does, scrapes no gateway pod while it is off, and records nothing. The first poll after a start
runs one interval after election, after the initial reconcile pass has rendered the rules
admitting the operator; a CR the poll reaches before its rules are applied costs one failed
poll, and a streak shorter than two polls records no event, so an upgrade leaves no Warning on a
healthy CR.
Listeners from a release before this one are not a failure either: the operator moves before the harness on an
upgrade, and an install can pin the harness image behind the operator, so the first polls after
an upgrade land on listeners that send no start time; the resets section reads them under the
rule without it, bounded all the same, rather than refusing them, and the stricter rule applies
from the first body that carries the gauge. A scrape that
fails, which includes a line past its bound and a sample outside its bounds, leaves the pod's
baseline untouched and the totals where they were. The
operator log carries one line when an endpoint first fails and one when it recovers, naming
the pod and the error type, never the body. The visible symptom of a standing failure, a
NetworkPolicy regime that blocks the rule, a relabelled operator pod, a listener that moved, a
proxy environment on the operator pod that a client without the no-proxy transport would obey, is
a `lastActiveTime` that stops advancing while commands are plainly running and events are
plainly being triaged. A `kubectl describe`
of the CR shows the operator's events; the implementation records one warning event per failure
streak so that the symptom has a cause beside it without a log search.

## What stays unwritten, and what lands it

| Status field                | Why it stays absent                                                                                                                                                                                                                                                          | The series that lands it                                                                                                                                                                                                                                             |
| --------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `sessionsTotal`             | The watcher's `k8s_event_watcher_session_creates_total{outcome="ok"}` counts the sessions it opens for triage and nothing else; chat sessions are opened by the gateway, which exports no series for them. A counter named for all sessions that counted half would mislead. | A gateway-side `kubeagents_sessions_total{origin}` beside the watcher's, summed with it. The `session_store` plugin sees every chat session start and is the natural emitter; it needs a listener, which the gateway pod does not have for Hermes-side series today. |
| `remediationsProposedTotal` | No series counts proposals by verb. The proposal path is the broker's version-control route, through which `proposal-create` reaches the forge provider, and that route is visible only as `kubeagents_credential_proxy_requests_total{endpoint="/v1/vcs"}` by status code.  | A broker series over the version-control route's verbs and the outcomes the forge provider returns, counted the way the exec route counts tool invocations; a `proposal-create` the forge accepted is a proposal.                                                    |
| `remediationsAppliedTotal`  | An applied remediation is an approved one; approvals are recorded by the `tool_call_audit` plugin's `approval_*` records in the log, not as a metric.                                                                                                                        | Either a Hermes-side series from the same plugin, which has the listener problem above, or a count of the broker's apply-side requests once those are distinguishable from reads. This is the least settled of the three and the last to land.                       |

Each lands on the poller's existing path: a new row in the source table, a new series summed,
and the CRD description changed from "nothing writes it yet". None needs a second mechanism.

## Security and privacy

Zero external egress: the operator reads two in-cluster listeners over the pod network and
writes two objects in the cluster. Zero PII or secret exposure: the status receives integer
totals and one timestamp; label values are summed away and never written anywhere, and the
baseline ConfigMap holds pod UIDs, pod names and integers. The new NetworkPolicy rules admit
the operator's pods on the two metrics ports and nothing else; the collector's rule is
unchanged. No new RBAC: pods are listed and ConfigMaps managed with verbs the ClusterRole
already grants, and the status subresource is already the operator's to write. No credential
of any kind is involved; both listeners are unauthenticated by design, as they are for the
collector, and for the same reason the operator treats what it reads as untrusted. The gateway
pod shares its network namespace across the agent container, the sidecar and any container
`spec.deployment.sidecars` adds, and the watcher's port is free whenever its process is down, so
a body on either port can come from something other than the two listeners. Folding each line as
it is read keeps the operator's memory per poll at one line, under its 128Mi limit whatever the
fleet's exposition grows to, and the per-poll ceiling bounds what one body can do to a total; neither makes the body more trusted. What is left is stated
rather than hidden: a workload in the pod can refuse the operator a count, can raise one by at
most `usageDeltaCeiling` per poll for as long as it holds the port, and can never set one. At
twelve polls an hour that is a visible drift, a counter rising on an install whose log shows no
commands and no triage, not a value chosen; closing it would mean authenticating the listeners,
which the collector's scrape does not do either. The scrape is therefore not the grant the
alternatives section refuses, a workload describing its own activity: it is a reader of counts
the workload can withhold or nudge, never write.

One more residual sits in the peer selector. The chart renders the CR into the operator's
namespace, and above one replica the agent's Role holds `get` and `patch` on every pod there,
because RBAC cannot say "only your own pod"; so on an HA chart install anything running as the
agent's ServiceAccount can put the operator's label on a pod of its choosing, the sandbox
included, and that pod is then admitted to both metrics ports. What it gains is the two
listeners' counters: the watcher's, which the agent container already reaches over its own pod's
loopback, and the broker's, to which it has no route today, integers under closed label
vocabularies; and the same grant already lets it swap a sibling's image, so the selector argument above holds in
full on the single-replica default and in substance above it; the pages the implementation
rewrites say so rather than "nothing else".

The ConfigMap has writers the pod does not: whoever holds ConfigMap write in the CR's
namespace, which Kubernetes' default `edit` and `admin` roles grant while granting nothing on
the CR's status. Such a principal can raise a counter within the bounds by editing the
document, and the design accepts that trust, as the gitops-state ConfigMap accepts its
administrator's: the counters are an activity summary, not the audit record, which is the log;
the bounds turn a careless edit or a restore into a refused document rather than a crash or an
absurd field; and the agent's own ServiceAccount holds get, list and watch on ConfigMaps, so
the workload keeps no door through it either.

## Alternatives not taken

**Counting observed events rather than accepted ones.** `k8s_event_watcher_events_seen_total`
is the closer match to the words "ingested and evaluated", and the sources section says why it
cannot be a cumulative status field: it replays the API server's retained Events on every
watcher start, and it never rests.

**Reading the series from Managed Prometheus through the Monitoring API.** The collector
already scrapes both endpoints and the Prometheus query endpoint would hand back `increase()`
over any window, which makes resets someone else's problem and needs no NetworkPolicy change.
It was not taken because the operator's ServiceAccount carries no Google identity, so it would
need a Workload Identity binding and `roles/monitoring.viewer` in the IAM module and the chart,
it would tie a core status field to GKE and to a collector an install may switch off, and it
would add the collector's ingestion lag on top of the poll interval, which is already the
counter's lag. The
poller's source is an interface with the pod scraper as its one implementation, so a
deployment that cannot admit operator-to-pod traffic can gain this source later without
changing the accumulation or the writer.

**Reaching the pods through the API server's pod proxy.** `GET .../pods/<pod>:<port>/proxy/metrics`
needs no new pod-network rule, but it needs `pods/proxy` on the operator's ClusterRole, which
is the power to reach any port of any pod, and on a cluster that applies policy to control-plane
traffic the proxy's own source would need admitting. Broader than the problem.

**Letting the agent write its own counters.** The security reference lists the agent
ServiceAccount's write grants, leader-election leases and, at more than one replica, `get` and
`patch` on the pods of its namespace, so the leader can label itself, and the status is not
among them. The operator is the only writer, and a workload
with cluster privileges describing its own activity is not a grant to add.

**Keeping the baseline, or the totals, in memory.** Simpler, and wrong on every operator
restart, as the resets section says.

**Polling from `Reconcile`.** The loop is event-driven and can be quiet for hours; a counter
that advanced only when something else changed the CR would read as broken on exactly the
installs where it is most useful. The reconcile loop's steady-state requeue is also capped by
the probes it schedules, and a poller there would either shorten every requeue or move with
them.

## Testing

What shipped beside the poller, kept as the record of what each test is for.

Unit tests, beside the poller, one per branch of the resets section as it stands, each asserting
the poll after as well as the poll itself: the difference branch (a known pod, the recorded
start time, a sample not below the last); the whole-sample branch (a pod created after the document was first recorded; a later start
time), including such a pod above the ceiling, which is recorded and adds nothing, the first
poll with no ConfigMap, which records every pod and adds nothing whatever the samples, and a
pod that first poll could not scrape, created before the document, recorded on its next scrape
and adding nothing, on the first poll and on a re-seed alike, the re-seed recording its own
time as the first-recorded time; the first body carrying the gauge for an entry that recorded
none, taking the whole-sample branch under the ceiling; and a
forged later start time with a large sample, which adds nothing and advances the baseline; the
two refused shapes (a start time earlier than the recorded one; a fall under an unchanged start
time), refused with the baseline advanced to the body's sample and start time, nothing added,
and the next poll counting from it; the rule without the gauge (the difference, a new pod, a
fall that advances the baseline, and a body without the gauge after a start time was recorded,
refused the same way); the per-poll ceiling on both adding branches (an honest burst past it
costs that interval and the next poll's delta is the new interval alone; a gap of several polls
whose delta passes one ceiling is refused whole, the baseline advanced, that gap's count lost
once); the scrapes that produce no body (a connection
that failed, a 3xx answer, a line past its bound, a wanted line `expfmt` cannot parse, a sample
that is negative or not finite), each keeping the baseline and nothing more; a fleet-sized body
of the wanted family and of other families alike, summed correctly within a memory bound of one
line; a pod missing this
poll and back in the next, with and without a second gateway pod counted in between, asserting
that the reset pod takes the sibling's marker and the sibling's next lone advance is counted;
a partial straddle, both replicas moving by different amounts and the lagger catching up alone
the poll after, asserting the catch-up is reset and the total took the larger delta once; an in-place restart
of the lagging replica with a later start time, reset rather than taken whole; the
largest-delta rule across two gateway pods and its agreement with the sum for one; the
baseline-absent-with-counters-present case; a ConfigMap whose recorded CR UID is not the CR's
or whose values fail the read-back bounds, including a total above the `int64` headroom, one
below the status, and a first-recorded time in the future (treated as absent); a quiet poll writing no ConfigMap; the disabled-watcher
case (no gateway scrape, no log line); the series selection (the `status` values summed and the
three excluded; the injected series and not the observed one); and the port-by-name lookup when
the port sits on a native sidecar among several containers. The accumulator takes samples and
the ConfigMap's document and returns the next document, so none of these needs a socket.

An envtest, beside the existing `usage_status_envtest_test.go`: a `PlatformAgent` served by
this release's CRD receives one patch per poll in which a stub source moves and none in which
it does not; a status left behind the ConfigMap (the crash between the two writes, staged by hand) is
repaired by the next poll without the totals moving and with `lastActiveTime` set to the time
the ConfigMap recorded, not the repair's; under the CRD without `status.usage`, the
poller writes the status once while the pruning record is fresh and once more when it has
expired, shares that record with the Ready writer, and keeps the ConfigMap current throughout.
That a ConfigMap written with the non-controller owner reference enqueues no reconcile is a
unit test against the owner handler `Owns` uses, which needs no API server.

A live check, which is the acceptance criterion: on an install built from the branch,
`toolExecutionsTotal` rises after commands run from the sandbox and `eventsIngestedTotal` after
events the watcher accepts arrive; a broker pod restart, a gateway pod restart, a restart of the
watcher process alone (the supervisor's), and an operator restart each leave both counters where
they were and they keep rising afterwards, the watcher's restart in particular adding nothing for
the replay of the retained Events; a quarter of an hour with no command and no accepted event
produces no status write; and the two policies show the new rule with the operator pod as the
only peer added.

## Documents the implementation changes

Each of these landed with the poller; the list is the record of where the facts moved.

- The CRD reference's `status.usage` rows for the two counters and `lastActiveTime`, from
  "declared; nothing writes it yet" to what they count and how often they move, including that
  `eventsIngestedTotal` counts events accepted for triage, and the rows for the counters that
  stay unwritten pointing at this document's last table.
- The security reference's NetworkPolicy summary, which lists what the gateway and broker
  policies admit on ingress, gains the operator's pods on the two metrics ports.
- The observability page's metrics section gains one sentence: the operator reads these two
  series into `status.usage`, which is where an operator without a collector sees the counts.
- The `AgentUsageStatus` field comments, which say nothing writes the counters, and the
  `EventsIngestedTotal` comment's meaning.
- Every page that states the metrics ports' peer set as closed: the security reference's two
  NetworkPolicy bullets (the gateway's names the collector's namespace as the peer; the
  broker's ends in "nothing else"), the credential-isolation reference's "and nothing else" on
  the broker's ports, the operator page's parenthetical on who may reach the broker, the
  telemetry page's two sentences ("admit the collector on those ports and no other" and "admit
  only `gke-gmp-system`"), the kustomize page's list of gateway ingress peers, and the chart
  README's note that the operator's policies admit the collector's namespace on those ports.
  Each gains the operator's pods on the metrics port.
- `docs/security-requirements.md` and `docs/credential-isolation-design.md`, which state 8766 as
  open to the collector's namespace and to no other peer; the Reach section says what replaces
  that sentence.
- `docs/designs/cron-report-relay.md`, which records that the operator reads nothing the agent's
  pods write and binds no metrics endpoint of its own. The poller is the exception, and that
  record points here.
- The map's identifier-sources table, which gains a row for the poll interval, the ConfigMap
  name and the operator-peer rule, with the event-watcher row as the model.
