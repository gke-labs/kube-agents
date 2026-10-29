# Producing the PlatformAgent Usage Counters

**Status:** implemented. The facts table below is the tree as read on 2026-10-01, before the poller; the sections after it describe what ships. [`proposal-outcome-readings.md`](proposal-outcome-readings.md) will replace the event-counted `remediationsMergedTotal` and `remediationsClosedUnmergedTotal` rows below, and what the summary, the `lastActiveTime` row and the testing record say about them. That design is of record and not yet implemented, so the rows here still describe what ships.

## Summary

`PlatformAgent.status.usage` declares cumulative counters, `sessionsTotal`, `eventsIngestedTotal`,
`toolExecutionsTotal`, `toolExecutionsBlockedTotal`, `remediationsProposedTotal`, `remediationsMergedTotal`,
`chatMessagesInboundTotal`, `brokerFailuresTotal`, `chatTurnsErroredTotal`, `backgroundTasksGaveUpTotal`, `negativeFeedbackTotal`, `remediationsClosedUnmergedTotal` and `remediationsAppliedTotal`, and a `lastActiveTime` and a `lastHumanActiveTime`, and until the poller this document describes nothing wrote them. The schema shipped that way on purpose: the agent's
ServiceAccount holds no write verb on the status, and the operator, which does, saw no session,
event or tool call. All but one of those now have an in-cluster source, and the watcher's per-cluster gauge gives two more fields a reading. The credential broker
serves `kubeagents_tool_invocations_total`, `kubeagents_vcs_requests_total`,
`kubeagents_vcs_proposals_merged_total`, `kubeagents_vcs_proposals_closed_unmerged_total` and `kubeagents_broker_execution_failures_total` on its metrics-only listener, and the event watcher serves `k8s_event_watcher_events_injected_total` and `k8s_event_watcher_triage_sessions_total` on the gateway
pod's `agent-api-auth` sidecar, and the `chat_metrics` plugin serves
`kubeagents_chat_messages_inbound_total`, `kubeagents_chat_sessions_total`, `kubeagents_chat_turns_errored_total`, `kubeagents_kanban_tasks_gave_up_total` and `kubeagents_chat_feedback_negative_total` on the `platform-agent` container, all for the
managed-Prometheus collector.

This document settles how the operator turns those series into the status fields: a poller
that runs on the leader off the reconcile path, scrapes the three endpoints over a NetworkPolicy
rule that admits the operator's pods and nothing else new, accumulates per-pod deltas into
totals it keeps beside their baseline in a ConfigMap, so the counters stay monotonic across pod,
process and operator restarts, and patches the status at most once per interval, only when the
status is behind. `toolExecutionsTotal`, `toolExecutionsBlockedTotal`, `eventsIngestedTotal`, `remediationsProposedTotal`,
`remediationsMergedTotal`, `chatMessagesInboundTotal`, `sessionsTotal`, `brokerFailuresTotal`, `chatTurnsErroredTotal`, `backgroundTasksGaveUpTotal`, `negativeFeedbackTotal`, `remediationsClosedUnmergedTotal`, `lastActiveTime` and `lastHumanActiveTime` land this way, the proposals counters from the
broker's version-control series, and `clustersRegistered` and `clustersMonitored` ride the same
poll as gauges: the watcher's `k8s_event_watcher_cluster_up` series counted and projected as the
latest reading, outside the ConfigMap. `remediationsAppliedTotal` stays
unwritten until a series exists for it, and the section on what stays unwritten says what that
series is.

## What was verified

Read from `main` and, where marked, observed read-only on a running install on 2026-10-01.
These are the facts the design rests on.

| Fact                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      | Where                                                                                                                                                                                                                           |
| ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| The watcher's listener is port 9095, named `event-metrics`, on `agent-api-auth`, which the gateway pod runs as a native sidecar: an entry in `initContainers` with `restartPolicy: Always`, not in `containers`. The broker's is port 8766, named `cred-metrics`, on the `envoy-credential-proxy` container. Both are container ports, not Services, and both bind every interface.                                                                                                                                       | `platformagent_manifests.go` (`eventWatcherMetricsPort`, `credentialProxyMetricsPort`), `credential_proxy_manifests.go` for the broker container; `start-services.sh`, `credential_proxy.py` for the bind; the golden manifests |
| Each pod's NetworkPolicy admits its metrics port from one peer: pods in the `gke-gmp-system` namespace. The gateway policy's first rule admits every pod in the agent's namespace, but on the API ports and, when the dashboard is enabled, its port only.                                                                                                                                                                                                                                                                | `buildNetworkPolicy`, `buildCredentialProxyNetworkPolicy`; live: the gateway policy's 9095 rule names that one namespace                                                                                                        |
| The operator's pod carries `app.kubernetes.io/name: kube-agents-operator` in both install paths: the chart's `operatorSelectorLabels` and the kustomize manager manifest.                                                                                                                                                                                                                                                                                                                                                 | `_helpers.tpl`, `config/manager/manager.yaml`; live: the running operator pod                                                                                                                                                   |
| The operator reads its own namespace from the ServiceAccount namespace file and its own Pod through the API reader to discover its image, in a branch the chart's `OPERATOR_IMAGE` skips, and keeps neither; the policy renderers are pure functions of the CR and the start-up flags, and the golden tests render them with no pod around. For its own callout the operator already renders a Downward-API `POD_NAMESPACE` on a container it manages.                                                                    | `cmd/main.go`, `platformagent_a2a_callout.go`, `internal/testing`                                                                                                                                                               |
| The gateway Deployment has one replica; the operator caps `spec.deployment.availability.replicas` at one, so the leader-election wrapper is not rendered, and the HA fixture renders one. Every gateway pod runs the sidecar and its watcher; nothing gates the watcher on leadership. The broker Deployment has one replica and rolls with `Recreate`.                                                                                                                                                                   | the golden manifests, `start-services.sh`; live: both at 1                                                                                                                                                                      |
| `spec.harness.eventWatcher.enabled=false` makes the entrypoint start no watcher, while the container port and the collector's rule render regardless, so nothing listens on `event-metrics`; the reconciler resolves the switch through `eventWatcherEnabled` for the `EventWatcher` condition.                                                                                                                                                                                                                           | `deploy/shared/start-services.sh`, `platformagent_manifests.go`, `platformagent_controller.go`                                                                                                                                  |
| The entrypoint supervises the watcher in a retry loop and restarts it in place after any exit, in the same container and pod, so the watcher's counters reset more often than its pod does.                                                                                                                                                                                                                                                                                                                               | `deploy/shared/start-services.sh`                                                                                                                                                                                               |
| `k8s_event_watcher_events_seen_total` increments on every delivery the informer makes, before the reason filter: that includes the initial list, which replays every Event still inside the API server's retention, and the redeliveries of watch-connection rotations. `k8s_event_watcher_events_injected_total` increments once an event has passed the filter and the dedup cache, has been handed to the agent, and was neither graded informational nor dropped on the daily ceiling by the daemon that received it. | `cmd/k8s-event-watcher/main.go` (`Dispatch`), `watcher.go`, the watcher's README ("Replay Shielding", "On-Disk Snapshots")                                                                                                      |
| The dedup caches are snapshotted to the data volume, under `event-watcher/` in the agent's home (`/opt/data` unless `spec.harness.hermes.agentHome` moves it), every thirty seconds and at shutdown, and restored at start, so a restart re-offers nothing already triaged; a lost or unreadable snapshot costs one replay of the retained Events.                                                                                                                                                                        | the watcher's README; `platformagent_manifests.go` (the sidecar writes its snapshots to the shared volume)                                                                                                                      |
| The watcher's `k8s_event_watcher_session_creates_total{…,outcome}` records `outcome` as `ok` or `error`. The broker's series is `kubeagents_tool_invocations_total{tool,subcommand,status}` with `status` one of `success`, `error`, `blocked`, `busy`, `abandoned`; `abandoned` is recorded both for a running command killed when its caller left and for a caller that left the queue before the command started.                                                                                                      | `cmd/k8s-event-watcher/main.go`, `credential_proxy.py`                                                                                                                                                                          |
| The gateway pod's containers share one network namespace, `spec.deployment.sidecars` renders any container a CR author names into that pod, and the watcher holds 9095 only while its process runs: when the port is taken it logs an ALERT and runs on without a listener. The manager runs under a memory limit set in both install paths.                                                                                                                                                                              | `platformagent_manifests.go`, `common_types.go`, `cmd/k8s-event-watcher/main.go`, `config/manager/manager.yaml`, `charts/kube-agents/values.yaml`                                                                               |
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

1. lists the pods the policies select, in the CR's namespace, once per distinct selector and
   from the API server rather than the cache: `app: <name>-gateway` for the gateway, read as two
   targets off one list, one per listener on that pod; `app: <name>-a2a-gateway` for the A2A gateway a `next`
   install runs, which selects no pod elsewhere; and for the broker the two labels `credentialProxySelector` returns,
   `app: <name>-credential-proxy` and `kubeagents.x-k8s.io/component: credential-proxy`. When
   `eventWatcherEnabled` is false for the CR, the gateway pods' watcher target is left out: the entrypoint
   starts no watcher, so nothing listens on `event-metrics`, and a refused connection there
   would be the install's choice, not a failure; the chat target has no such switch, since the plugin is part of the chat profile;
2. for each running pod, finds the container port by name, `event-metrics` and `chat-metrics` on the gateway pod
   and `cred-metrics` on the broker pod, looking through `initContainers` as well as
   `containers` because the watcher's sidecar is a native one while the chat plugin runs in the `platform-agent` container, and reads `/metrics` at the pod IP and that port, joined with
   `net.JoinHostPort` so an IPv6 pod IP works, with a short deadline. The body is not parsed
   whole: the watcher's exposition grows with the fleet, clusters times the namespaces that
   ever raised a warning times the reasons, per family, for the life of the process, so a
   ceiling on the body would be a bound sized against no population, and `expfmt`
   materialises every family of whatever it is handed. The reader scans the body line by
   line and keeps none of it: a line of a family the design wants, each counter's own, the
   start-time gauge, and for the watcher's body its per-cluster up gauge, is parsed on its own
   with `expfmt` and folded as it is read, the counter's sample into the running per-pod sum
   and the up gauge into two per-pod counts, and every other line is skipped unread. The one bound is
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
   section says which), and takes one sample per pod per counter, and from the watcher's body
   its two cluster counts, the up-gauge series present and those at `1`. A sample that is
   negative or not finite is a failed scrape for that pod, as is a body with more up-gauge
   series than `usageClusterGaugeCeiling`: it contributes nothing, and the log line names the
   pod. What a body may add to a total in one poll is bounded in the resets section, and that
   bound, not the sample, is the one that matters;
4. folds the samples into the totals through the per-pod baseline described below. Totals and
   baseline live together in one ConfigMap, which is the accumulator's source of truth; the
   counters in the status are a projection of it. The cluster counts are not folded and not
   stored: the poll keeps the largest of each across the gateway pods it read, as the reading
   to project;
5. writes the ConfigMap when a total or the baseline changed, recording with the totals the
   time of the poll that moved them, then patches `status.usage` when the status is behind the
   ConfigMap, copying its totals and that time as `lastActiveTime`, and the time a chat message last moved its counter as `lastHumanActiveTime`. The order, ConfigMap first,
   is deliberate; the resets section says why. The same patch carries the two cluster gauges
   when this poll's reading differs from the status in either direction, a zero reading
   included; clears them to absent when the watcher is off, and on the second poll in a row in
   which no gateway pod could be read; and leaves them as they were on the first such poll.

Nothing in `Reconcile`'s accounting changes: the reconcile loop keeps writing `activeInterfaces`
through the Ready writer as it does today, the poller never touches that field, and the Ready
writer never computes a counter. What the loop renders does change, by the two policy rules and
the `POD_NAMESPACE` input the Reach section owns, and the one piece of code the two writers
share is the echo check in the served-CRD section, generalised so that both call it.

## Counter sources

| Status field                              | Series                                                                                                                                                                                                                                                                                                              | Aggregation                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| ----------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `toolExecutionsTotal`                     | `kubeagents_tool_invocations_total`, broker pod                                                                                                                                                                                                                                                                     | Sum over `tool` and `subcommand`, over `status` in `success` and `error`: the commands the broker ran to an exit. `blocked` and `busy` are refusals that never ran. `abandoned` is left out because the series cannot say whether the command had started: the broker records it for a command killed mid-run and for a caller that left the queue before the start. Those three sum into `toolExecutionsBlockedTotal` below. `error` also covers a rejected request and a broker fault, so the count is the broker's view of "ran", a little wide.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| `toolExecutionsBlockedTotal`              | `kubeagents_tool_invocations_total`, broker pod                                                                                                                                                                                                                                                                     | Sum over `tool` and `subcommand`, over `status` in `blocked`, `busy` and `abandoned`: the requests the broker did not run to an exit, the complement of `toolExecutionsTotal`'s statuses in the same body, read through a second filter in the one scrape, so the two together are every request the exec route took as far as its policy; a request rejected as malformed before that, a bad length or an unparsable body, reaches neither. A refusal moves `lastActiveTime`: the field marks agent activity of any origin, and a refused command is the agent at work.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| `eventsIngestedTotal`                     | `k8s_event_watcher_events_injected_total`, every gateway pod                                                                                                                                                                                                                                                        | Within a pod, sum over every label: cluster, project, location, reason and namespace. Across gateway pods, the largest per-pod delta in the poll rather than the sum: each replica's watcher works the same event stream, so the sum would count an event once per replica. With one pod the two are the same.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| `remediationsProposedTotal`               | `kubeagents_vcs_requests_total`, broker pod                                                                                                                                                                                                                                                                         | Sum over `verb="proposal-create"` and `status="success"` alone, over every `provider`: the proposals the forge accepted. The same body as `toolExecutionsTotal`, read once and folded into the broker's counters, each with its own baseline entry under the pod; the other verbs and outcomes, and the per-forge split, stay in Prometheus.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| `remediationsMergedTotal`                 | `kubeagents_vcs_proposals_merged_total`, broker pod                                                                                                                                                                                                                                                                 | Summed whole; the series carries no label. The broker counts, once each per broker process, the proposals of its own that one of its reads, `proposal-list` or `proposal-view`, shows merged, under three rules that need no store: the author is the credential's login, the merge time is not before the process started (so a restart counts no merge the previous process could have), and each proposal counts once. A merge no read ever shows is not counted, and a credential whose login the broker cannot name counts nothing (the broker logs that once per process), so the field under-counts rather than over-counts and stays absent on such an install; it is the forge's word, where `remediationsAppliedTotal` is the install's. The restart rule holds to within the forge's and the pod's clocks agreeing. `proposal-acknowledge` is not a counting point: its answer is a reaction's result and carries no proposal state, so the remediation SOP's acknowledge step, which views the proposal first, is what makes a merge the broker never reads again rare, and no scheduled `proposal-list` is added.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| `chatMessagesInboundTotal`                | `kubeagents_chat_messages_inbound_total`, every gateway pod, on the `chat_metrics` plugin's listener (port `chat-metrics`, 9097, on the `platform-agent` container); and on a `next` install `kubeagents_a2a_gateway_messages_inbound_total`, the A2A gateway pod, on its `a2a-metrics` port (9096)                 | Summed over the `platform` label and across pods. Under `spec.mode: next` Chat ingress is the A2A gateway's, a separate program, and a message that becomes an A2A task never reaches the plugin's hook; the A2A gateway counts it where every adapter meets (`handleInbound`), before the sender is verified, under the same label vocabulary extended for its adapters (`googlechat`, `slack`, `discord`, `console`, `other`; a door's turns are programs', not chat, and are not counted), and the poller reads that pod as a fourth target into the same counter, with an entry and a streak of its own; its body carries `process_start_time_seconds` as every listener's does, so an in-place restart of the gateway container is told from a fall. The selector matches no pod on a `today` install. Read from the pod that runs Hermes: above one replica the container runs `leader_elect.py`, which starts the gateway on the lease holder alone and labels it `kubeagents.io/is-leader`, so the standby has nothing on the port by design, stays live and is not read; leadership moves between pods, so each pod's own count is summed. A new leader that predates the document but whose Hermes started after it was first recorded adds its whole first sample, so the messages it took between the lease and the first poll are counted; a leader whose Hermes predates the document is baselined at its first sample, as any such pod is. Counted on `pre_gateway_dispatch`, the hook the gateway runs once per inbound event after dropping its own internal events and before it checks the sender against the allowlist, so an unlisted user's message counts and nothing the gateway never hands to the hook does. Labelled by the platform Hermes names the event with, `googlechat`, `slack`, `teams`, `api` (the gateway's own API port) or `other`; the dashboard is not a platform Hermes names on a gateway event. The gateway pod now carries two listeners, read as two targets with a failure streak each. |
| `brokerFailuresTotal`                     | `kubeagents_broker_execution_failures_total`, broker pod                                                                                                                                                                                                                                                            | Summed over the `kind` and `tool` labels: the kinds (`exec_timeout`, `exec_failed`, `vcs_failed`, `forge_refresh_failed`, `api_relay_failed`, `chat_pull_failed`, `other_failed`) stay in Prometheus, and so does the tool a timed-out command ran (`tool`, the invocations counter's closed set; `none` on every other kind, which has no tool). The broker records the timeout where the command completes and reads every other kind off the response it writes, in the one hook that already counts requests, by route family: on its own routes a 500, 502 or 504, never its busy 503 (contention, and the exec route's busy is already an outcome of the tool counter), with the one 503 that is a fault, the version-control route's unreadable managed-repository list, recorded at its site; on the relays the whole 5xx class, since the upstream's status passes through and a 503 there is the upstream saying unavailable; on the chat and Slack relays nothing from the hook, since their 502 is the caller's channel or scope, while their event pulls failing on the broker's own subscription are recorded at the three pull sites as `chat_pull_failed`; a method the broker does not serve, which the base class answers 501 before authentication, counts nothing. A second count beside the tool counter rather than a new status in it, so `toolExecutionsTotal` keeps its meaning across versions: a timed-out command is an `error` there and an `exec_timeout` here. A counter that does not move `lastActiveTime`: a watcher retrying an unreachable fleet cluster through the relay would otherwise keep an idle install looking active.                                                                                                                                                                                                                                                                                                                                                                     |
| `remediationsClosedUnmergedTotal`         | `kubeagents_vcs_proposals_closed_unmerged_total`, broker pod                                                                                                                                                                                                                                                        | Summed whole; the series carries no label. The broker counts, once each per broker process, the proposals of its own that one of its reads, `proposal-list` or `proposal-view`, shows closed without a merge, by a person, a forge rule or a bot (a close the broker made itself through `proposal-close`, the fleet audit tidying a stale proposal, is marked seen before the forge call, so a close the forge applied but whose answer was lost, a 502 or 503, never counts either; a close the forge refused with its own status, 404 or 409 say, applied nothing, so the mark comes off and a person's later close of that proposal counts), under the three rules `remediationsMergedTotal` uses: the author is the credential's login, the close time is not before the process started, and each proposal counts once. A close no read ever shows is not counted, so the field under-counts; a proposal closed, reopened and merged counts once here and once there. The epic asked for a bounded-window query of recent proposals; the broker makes no scheduled read and keeps no store, as the merged counter's row says, so the count rides on the same reads. A counter that does not move `lastActiveTime`, like `brokerFailuresTotal`: a close is not the agent at work. Feeds the proposal-rejection signal against `remediationsMergedTotal`.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           |
| `clustersRegistered`, `clustersMonitored` | `k8s_event_watcher_cluster_up`, every gateway pod                                                                                                                                                                                                                                                                   | Gauges, not counters, read from the same body as the injected series: the number of `cluster_up` series the watcher exports is the clusters it built a client for, and the number at `1` the clusters whose informer is delivering events. Across gateway pods, the largest of each: every replica's watcher builds the same fleet, so a replica mid-startup reports fewer, not others. The latest poll's reading, projected as it stands: they fall after a restart without a cluster, are cleared when the watcher is disabled, are left as they were by one poll that could read no replica, and are cleared to absent by the second such poll in a row, because the operator then has no current reading and an absent field says so where a held one would read as current; whether the watcher is down, its pod not running, or merely unreachable is for the CR's events and the pod's state to say. The fields are pointers so that a zero, the watcher's fleet built and nothing synced yet, is written as a reading rather than serialised away. They do not move `lastActiveTime`. The watcher discovers its fleet once per process, so the registered count is the fleet as of the watcher's last start: a cluster that joins or leaves is counted after the gateway pod restarts.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| `sessionsTotal`                           | Two document counters, one status field. `k8s_event_watcher_triage_sessions_total`, every gateway pod, the watcher's body; and `kubeagents_chat_sessions_total`, the `chat_metrics` listener on the pod that runs Hermes, with `kubeagents_a2a_gateway_sessions_total` from the A2A gateway pod on a `next` install | The field is the sum of two halves kept as separate document counters (`sessionsTriage`, `sessionsChat`), because they aggregate differently and the fold aggregates per counter. The triage half is the watcher's triage sessions summed over cluster: a session it opened whose event the daemon then delivered, counted after the inject returns delivered, so a create whose inject the daemon filtered, suppressed or failed, and the re-create after a failed inject, add nothing (`k8s_event_watcher_session_creates_total` by outcome stays the operational view of every attempt), read on every replica over the one event stream, so across gateway pods it takes the largest delta as `eventsIngestedTotal` does. The chat half is the sessions the chat gateways opened for conversations, summed over `platform` and across pods as `chatMessagesInboundTotal` is: on `today` the `session_store` plugin counts a session on the first message that creates its `session_metadata` row, under the same lock as the write, on the `chat_metrics` registry its listener serves (a session idle past the plugin's retention window, seven days by default, whose row was swept is counted again when it resumes); on `next` the A2A gateway counts a conversation once, on the `mintSession` create that won, under the platform of its first message (a door's conversations are not chat and are not counted, and the loser of a mint race adopts the winner's record and counts nothing). The status value is one number for the pair: it seeds the chat half, and the read-back bound holds the pair's sum, not each half, to it. The halves share one name, `sessionsTotal`, in the Warning for a failed listener; the field is in `status.usage`'s first schema and shares the first counters' pruning record.                                                                                                                                                                                                         |
| `chatTurnsErroredTotal`                   | `kubeagents_chat_turns_errored_total`, the `chat_metrics` listener on the pod that runs Hermes; and on a `next` install `kubeagents_a2a_gateway_task_terminals_total` under `state` in `failed` and `rejected`, the A2A gateway pod, its sibling family with a filter of its own                                    | Summed over the `platform` label and, on the A2A gateway, over every `source`; a sibling family now takes its own filter (`usageSiblingsFor`), since the terminals family also carries `completed` and `canceled`. Hermes fires no hook on a failed turn, so the gateway patch `apply_chat_turn_metrics.py` counts at the three places a synchronous turn fails: the `except` body of the turn (`_hmwa_agent_error_reply`), the delivery of a result the agent marked `failed` (a provider 429 or 5xx, a connection error and the gateway timeout come back this way) and the provider-authentication failure in the turn runner; each counts under the turn's platform on the plugin's registry, through `gateway/chat_turn_metrics.py`, which never raises and counts nothing when the plugin is absent, and `verify_chat_turn_metrics.py` holds every inserted name bound at image build. A failed result whose delivery then raises counts twice, accepted as rare. The terminals family carries no platform, so on `next` every A2A task's terminal counts, a door's program-to-program task included; `rejected` there is a task the executor refused, counted as the epic asks. A counter that does not move `lastActiveTime`, like `brokerFailuresTotal`: the message that started the turn already did.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        |
| `backgroundTasksGaveUpTotal`              | `kubeagents_kanban_tasks_gave_up_total`, the `chat_metrics` listener on the pod that runs Hermes, read beside the chat counter; not asked of the A2A gateway, which has its own counter list                                                                                                                        | Summed whole; no label. No Hermes hook fires when a card settles, so the plugin reads the board at scrape time, read-only, through `gitops_workspace.board_path()`. Hermes parks a card whose breaker tripped as `status = 'blocked'` and appends a `gave_up` event to `task_events` (`gave_up` is an event kind and a run outcome, never a task status), so the read is `SELECT DISTINCT task_id FROM task_events WHERE kind = 'gave_up'`. The poller treats a new process as starting from zero and adds its whole sample, so the plugin's first successful read, made when the listener starts, is a baseline that counts nothing and every later read adds the task_ids it has not seen: a card counts once however many times it gives up, stays counted after the workspace GC deletes it, and a card that gives up while the gateway restarts is missed, an under-count. A board it cannot open keeps the last value. A counter that does not move `lastActiveTime`, like `brokerFailuresTotal`: a card giving up is a failure, not the agent at work.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           |
| `negativeFeedbackTotal`                   | `kubeagents_chat_feedback_negative_total`, the `chat_metrics` listener on the pod that runs Hermes; not asked of the A2A gateway, whose counter list (`usageA2AChatCounters`) omits it because its families carry no reactions                                                                                      | Summed over the `platform` label. The `chat_feedback_reactions` profile hook (`agents/chat/defaults/hooks/`) sits on Hermes' `reaction:added` event, which the Slack adapter fans out for every reaction not the bot's own with the reactor, the reacted-to message's author, the channel and the message timestamp, and counts a `-1` or `thumbsdown` reaction, with or without a skin-tone modifier, when the reactor is not the bot and the message is the bot's, once per reactor and message within one gateway process; the hook does not subscribe to removals, so a thumbs-down taken off and put back in the same process is one, and after a gateway restart the same person's thumbs-down on the same message counts again. The bot's own Slack identities come from the gateway the plugin's inbound hook noted, the same `_bot_user_id` and `_team_bot_user_ids` the shipped Slack UX patches read and `verify_slack_ux_clicks.py` pins at image build, so before the first inbound message after a gateway start, or without a Slack adapter, nothing counts and the first such reaction is logged once. Slack through the Hermes gateway only: Google Chat delivers no reaction events, and the A2A gateway's Slack adapter does not subscribe to them, so a `next` install with Slack through A2A reads 0. A counter that does not move `lastActiveTime`, like `brokerFailuresTotal`: the message the reactor reacted to already moved it.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| `lastActiveTime`                          | derived                                                                                                                                                                                                                                                                                                             | The time of the last poll in which a total other than `brokerFailuresTotal`, `chatTurnsErroredTotal`, `backgroundTasksGaveUpTotal`, `negativeFeedbackTotal` or `remediationsClosedUnmergedTotal` moved: a command ran, a proposal was opened or merged, a chat message arrived, a session was opened, or an event was accepted for triage. The field's documented meaning is the most recent interaction or event triage, and a chat turn that runs no brokered command now moves it through `chatMessagesInboundTotal`.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| `lastHumanActiveTime`                     | derived                                                                                                                                                                                                                                                                                                             | The time of the last poll in which the one counter only a person moves advanced: `chatMessagesInboundTotal`. The document keeps it as `lastHumanMoved` beside `lastMoved`, stamped in the same fold when the advancing counter is that one. Every other counter has a producer that is not a person: the Controller Stall Watch cron runs brokered commands every 30 minutes, the event watcher accepts events on cluster churn, the fleet audits open proposals on their own, and a merge or a close can be a forge rule or a bot; the sessions counters add nothing, since every chat session begins with a counted message. So `lastActiveTime` never rests on an install nobody uses; this field does, which is what a stalled-onboarding check needs.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              |

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
that rule reads the stored markers alone, so it holds across a leader change between
replicas that both stay live; a former leader that terminates mid-straddle is the exception, in
the resets section below. It makes the layout under-count rather than over-count: informer skew that
straddles a poll
boundary, one replica ahead at one poll and the other catching up by the next, drops the
catch-up instead of counting it twice, and a replica's events its sibling did not inject, a
sibling whose daemon dropped them on its daily ceiling or whose dedup snapshot was kept while
this one's was lost, are lost whenever this replica was quiet for a poll in which the sibling
moved. That is the error this document tolerates, on a layout that is opt-in and in a series
that carries no replica attribution; the fix, if it ever matters, is a watcher that counts only
on the leader, not a smarter maximum. The broker runs one pod, so its series needs no such
rule.

## Reach: how the operator gets to the endpoints

The scrape is a direct read of the pod IP, and before the poller the policies admitted the
metrics ports from the collector's namespace alone. The implementation adds one ingress rule to each policy that fences a metrics port (the gateway's, the broker's, and the A2A gateway's three fences on a `next` install),
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
would open every listener to the shell sandbox and to anything else that lands in the
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
the last sample it took from each pod, keyed by pod UID and counter, since the broker's one
body feeds several counters (document layout version 2; a version-1 document, or one that predates a counter, is migrated in place on the first poll, its totals and baseline kept, so an upgrade loses nothing: a total the document lacked starts at the status floor, so a rollback and return does not have the read-back refuse the document, and when that floor is zero the counter is recorded as unfolded in the document, so the first sample of each pod read in the poll that first records a pod for it adds whole, whether that is the migrating poll or a later one, since no document and no status has seen any of it; the counter leaves the set in that poll, and a pod first read later is baselined like any pod older than the document). On
a poll it adds, for each pod and counter it scraped:

- the difference from the pod's last sample, when the pod UID is known, the body's start time
  is the recorded one, and the sample is not below the last one;
- the whole sample, when the pod UID is new, which means created after the document was first
  recorded and not merely absent from it (a new pod starts from zero, so everything it has
  counted is new), unless a live gateway sibling's marker is later than the pod's creation,
  in which case the sibling supplied the events this pod injected in the meantime and the pod
  is recorded at its sample with the sibling's marker, adding nothing, as a known replica behind
  its sibling would be; or the body's start time is present and later than the recorded one
  (the process restarted inside the same pod); or, for a Sum counter, when the pod UID is new
  and the body's start time is later than the document was first recorded, whatever the pod's
  own age (the standby that took the lease: its Hermes began after the baseline did, so none of
  its count was seen);
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
  intervals. The drop runs before the markers are snapshotted, so it has a cost in the
  mirror case: a replica trailing a leader that then terminates is never reset against the gone
  leader's marker, and its pending catch-up -- the events the leader already counted -- is taken
  a second time when it next advances, an over-count of one straddle's events and one of the two
  places this layout over-counts rather than under-counts -- the re-seed skew straddle below is the
  other. That cost is accepted: snapshotting the
  departed markers would close it, but a departed sibling would then suppress a genuinely new
  replica's first advance when it is read late in a rollout, and a real interval would be lost
  for good; a bounded one-time double count is preferred to a permanent loss. A reset that took
  the sibling's marker after the poll would close the missed-sibling
  loss as well and open an over-count instead, a replica trailing its sibling by one poll having
  its catch-up counted whenever the sibling is quiet, and the under-count is the one preferred.

Entries for pods that no longer exist are dropped when the baseline is next written; their
counts are already in the totals.

The start time is why every listener gains `process_start_time_seconds`, as a gauge each
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
counter, the time of the last poll that moved a total, and per pod UID and counter the pod's name for a reader, its last sample,
its last start time, and the poll at which the entry last supplied a delta the total took,
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
with the CR's. A mismatch means the document is treated as absent and its counters re-seeded,
and the ConfigMap holding it overwritten: it is what a CR deleted and re-applied under the same
name sees while the collector has not yet removed the old ConfigMap, or was kept from removing
it, and without the check the new CR would inherit a predecessor's totals, through the status
patch that fires whenever the status is behind. The overwrite is gated on ownership: the poller
rewrites a ConfigMap under the name only when it is the operator's own -- carrying the instance
label, or an owner reference naming a PlatformAgent of this name, which a predecessor's does on
a delete-and-recreate. A ConfigMap another writer parked under the name has neither; the poller
leaves it untouched, records a `UsageConfigMapForeign` Warning on the CR, and `status.usage`
stays where it was until the object is removed. A name is not ownership; the finalizer applies
the same rule to the data volume. What the poller reads back
is bounded before it is used, with a bound of its own for the totals, which honestly outgrow
any per-pod or per-poll figure: every sample non-negative and finite, every total non-negative,
finite and no higher than 2^53, the largest integer a float64 keeps exact, so that a reader which
decodes the CR untyped, a dynamic client or `jq`, reads the number written, no total below the status it
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
it should be counted. The whole-sample branch therefore applies to a pod created after the document was
first recorded, where "a new pod starts from zero" is true, and, for a Sum counter, to a
never-read pod whose body carries a process start time later than that: the document carries
the time it was first recorded, and a pod's `creationTimestamp` from the list in step 1, or the
start time in the body, is compared with it. A process younger than the document on an older
pod is the standby that took the lease; its count began from zero after the baseline did, and
a pod keeps its entry once read, so a restart inside a known pod takes the later-start branch
above and never this one. Every poll that treats the document as absent, the first poll and each re-seed after a
mismatch or a failed bound, records its own time there rather than carrying a predecessor's
forward: "a new pod starts from zero and none of it was counted" holds only for pods, or
processes, started after the last point at which every pod was re-baselined, and a time carried forward would
make a pod the re-seed could not scrape "new" on its next scrape, with its lifetime counted
once more on top of totals that already hold it. Absence from the document is not newness: a pod
the first poll could not scrape, or that one of the policies kept out while another
already admitted the operator, is recorded on its next scrape and adds nothing. `lastActiveTime`
is first stamped by a poll in which something ran. What a fresh install loses is the count
between each pod's start and the first poll after it, the under-count this document prefers
everywhere else.

Recording every pod level is right only where the replicas are level. A re-seed that scrapes two
gateway replicas at different samples -- informer skew on the shared stream -- records both at the
seed poll's own time and takes the larger delta on the next joint advance. Where the larger delta
belongs to a replica catching up from before the seed, it carries that pre-seed backlog on top of
the interval's own events, and the maximum counts the backlog once. The fold cannot tell that from
its mirror -- a smaller delta from a replica lagging after the seed on ordinary skew, where the
larger delta is the true interval and the maximum is exactly right -- because the two produce the
same deltas; no rule on the deltas separates them, so the plain maximum is the honest choice,
counting the backlog in the first case rather than losing a real interval in the second. The
residual is bounded -- one straddle's backlog, counted once -- and is the second place this layout
over-counts rather than under-counts, beside the terminating-leader straddle earlier in this section.

The ConfigMap is written before the status. A crash between the two leaves the status one poll
behind the totals, and the next poll repairs it, because the status patch is issued whenever the
status is behind the ConfigMap, not only when this poll moved a total; the repair stamps the
time the ConfigMap recorded, not its own, so `lastActiveTime` says when the counters last moved
rather than when the status caught up. That time is in the ConfigMap for this reason: in the
poller's memory alone it would die with the process that took it. The other order would
leave the totals behind the status after a crash, and the next poll would add the interval's
deltas a second time. Under-counting until the next poll is the error this document prefers where
the over-count it trades against would be permanent, as this crash's double-add would be. Two cases
run the other way, each accepting a bounded over-count because there it is the under-count that
would be permanent: the terminating-leader straddle and the re-seed skew straddle, both above.

## Write cadence and the status writers

The status is written with `Status().Patch` and a merge patch from the CR as read, touching only
the counters, the two cluster gauges and the two times, at most once per poll and only when the
status is behind the ConfigMap's totals or a gauge differs from this poll's reading in either
direction. The gauges are not in the ConfigMap: they are a reading, not an accumulation, so an
operator restart re-reads them on its first poll and loses nothing. Every status write re-enqueues the CR through the unfiltered `PlatformAgent`
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

### The status as gauges

The reconciler publishes the CR as it reads it. Right after the `Get` that opens every
reconcile, the operator sets three gauge families on controller-runtime's registry
(`k8s-operator/internal/controller/usage_gauges.go`): `kubeagents_platformagent_usage`
with a `field` label per numeric field of `status.usage`, `kubeagents_platformagent_usage_time_seconds`
for the two times, and the CR's creation time, as unix seconds, and `kubeagents_platformagent_phase`, one-hot over the
phase vocabulary. The labels are the agent's namespace and name as `agent_namespace` and `agent` (a Managed Prometheus scrape owns `namespace` as a target label and renames a metric's to `exported_namespace`, as the watcher's `cluster` and `location` already arrive) and those two closed sets;
nothing from conditions, messages or `activeInterfaces` is exported, and a reflection test
holds the field list to the struct so a new counter cannot be added to the status without a
sample. No writer publishes: every status write, the poller's patch and the Ready and
Degraded writers' updates alike, re-enqueues the CR through the unfiltered watch, so the
reconcile it triggers publishes what was written, and the informer's initial list after an
operator restart or a leader change publishes the whole fleet on the first pass. One site
makes the gauges a copy of the status by construction; a publish from a writer would miss
the writers that do not call it and the early returns on an unchanged status. The manager
serves them on its metrics listener, which the chart binds to `:8080` over plain HTTP
(`--metrics-secure=false`): the scrape is GKE Managed Prometheus from `gke-gmp-system`, which
carries no bearer token the manager's authorization filter would accept. What bounds the
plain listener is the same control the broker's and the gateway's metrics-only ports have:
the chart renders `<release>-operator-netpol` over the operator pod, ingress only, admitting
the collector's namespace, and `platformAgent.monitoringScraperNamespace` when set, on the
metrics port and nothing else; the health port stays open for the kubelet's probes and the
webhook port, when the webhooks render, for the API server. The chart renders a
`PodMonitoring` over the operator's Deployment behind `operator.metrics.podMonitoring`, the
same tri-state as `platformAgent.podMonitoring`; `operator.metrics.enabled: false` binds
nothing and renders neither the port, the scrape nor the policy. The three value reads go
through `| default dict`, so a `helm upgrade --reuse-values` from a release that predates the
key renders with the listener off rather than aborting. The kustomize base leaves the
listener off, as before: nothing on that path scrapes it.

A gauge of a counter is a departure from the producers' families, which are counters, and it
is deliberate: the samples are a copy of the status, which already folded restarts out, so a
collector reads the same monotonic totals the status holds and a `rate()` over them is the
rate over the status's five-minute writes. A deleted agent's samples are dropped when its
reconcile finds it gone. The phase vocabulary is the API's four phases plus `Degraded`, drawn from the
constants the controller writes; a test fails on a phase assigned from a string literal through
`Status.Phase`, `newPhase` or `phase` anywhere in the package, since a phase outside the list
would read all zeros. Under the served-CRD skew
below, the reconciler reads a status the server pruned, and its zeros mean unknown. The publish
reads the writers' own records, at any age, since a record's freshness is the writers' reprobe
gate and a sample must not lapse into zero with it: while the shared whole-block record is held,
or the block reads entirely empty (a pruned status before this process recorded it), every usage
and time sample of the block is dropped for the agent and the phase and `createdTime` alone are served; while a per-field record
is held, the records the poller keeps for the newer fields a CRD at an intermediate schema prunes
while serving the rest, that field's sample alone is dropped and the others are published. The
per-field records therefore live on the reconciler beside the shared one, written and read by
the poller as before and read by the publish. What remains is a window of at most one poll: after
an operator restart a status pruned per field reads as zeros until the first poll writes, is
echoed, and records the pruning, and a new agent's block carries zeros from the Ready writer's
first write until its first poll, which for a new agent is the truth.

### Health-cohort rules

The gauges exist so that a rule can read them. The chart renders one Managed Prometheus
`Rules` object per install, `<name>-health-rules`
(`charts/kube-agents/templates/platform-agent-health-rules.yaml`), behind
`platformAgent.healthRules.enabled`, off by default: a scrape is silent and an alert is not, so
an upgrade must not start one. It is a plain boolean rather than the `PodMonitoring`s'
tri-state for the same reason. Recording rules compute the ratios once, per agent by
`agent_namespace` and `agent`. Three rules of PromQL hold throughout, each pinned by the chart
test: an `increase()` is folded with `sum by` over the agent's labels, since an operator roll-over
splits a window's increase across two pod series and the sum is the total; an instantaneous read
is `max by`, which folds the old leader's series for its staleness window; and every `and` or
`unless` matches `on` the agent's labels, because the rule-evaluator appends its scoping labels to
a recorded series and an inline side does not carry them. A day-long condition is a plain `for`: a window
in the expression over a subquery would survive a rule-evaluator restart, which resets a pending
alert, but it fires on a young series, since five minutes of Degraded after an upgrade reads as a
whole day when the day's other samples do not exist; a restart delays an alert and a young series
would invent one. The stalled-onboarding rule reads the status's own `lastHumanActiveTime` rather
than an `increase()` over the chat counter for the same reason, with the chat counter present at zero as
its never-spoken case, not the time's absence, since under the served-CRD skew the operator
holds that time's sample back while `createdTime` stays; it departs from the epic's form by not reading proposals, which
are the agent's own activity and not the person's. Enabling the rules without the operator's scrape
fails at render, since nothing would ever fire; the alerts read the recorded ratios and the gauges, and one reads `up`, labelled `severity: at_risk` or `friction`.
Every threshold and sample floor is a chart value with its default in `values.yaml`, and
the template requires the thresholds block when the rules are on, so a `--reuse-values`
upgrade from an older release fails with a message rather than rendering numbers the template
made up.

The stalled-onboarding rule needs an install's age before anyone has spoken to it, which no
status field carries, so the time family gains one sample that is not `status.usage`'s:
`createdTime`, the CR's creation timestamp, published beside the phase and outside every hold
on the usage block. `rate()` and `increase()` over gauges that mirror monotonic counters read
as they would over the counters; a status pruned by the served CRD has no sample rather than a
zero, so a ratio over it is absent, not infinite.

Not in the first set, with the reason on the epic: pod restarts, the memory limit and the
operator's observed version, whose series a stock install does not produce (kube-state-metrics
would), and the "GMP enabled but no `PodMonitoring` matched" cohort, since a job that matches
no pod has no `up` series to alert on; the scrape alert covers a job whose targets fail.

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
The fields added after `status.usage`'s first schema, the two cluster gauges,
`remediationsMergedTotal`, `chatMessagesInboundTotal`, `toolExecutionsBlockedTotal`, `brokerFailuresTotal`, `chatTurnsErroredTotal`, `backgroundTasksGaveUpTotal`, `negativeFeedbackTotal`, `remediationsClosedUnmergedTotal` and `lastHumanActiveTime`, have a record each, in the poller, because a CRD at a previous
schema prunes some or all of those and serves the first-schema counters (every CRD with
`status.usage` has carried `remediationsProposedTotal` and `sessionsTotal`, so `sessionsTotal` shares the first three counters' treatment and has no record of its own), the shared record would hold those back
for the interval too, and one record for all the newer fields would hold the gauges back on the CRD
from the release between them, which serves the gauges and prunes the counter; each field is
probed again after the same interval, and a difference in it alone waits on its record rather
than re-patching every poll, while the others keep landing. The log line names the field.
The echo check moves from a function that knows about `activeInterfaces` to one that takes
whether the fields a writer wrote came back, and both writers call it.

## Failure behaviour

The schema has no field for an error, by design: static enums and integer counts only. An
install that switched the watcher off is not a failure: the poller reads the same switch the
reconciler does, scrapes no gateway pod while it is off, moves no counter, and writes once to
clear the two cluster gauges if they were set. The first poll after a start
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
plainly being triaged, and, from the second poll on, the two cluster gauges absent while the
watcher plainly has its fleet: under those causes the operator has no reading, and it says so
rather than holding the last one. A `kubectl describe`
of the CR shows the operator's events; the implementation records a warning event from the second
failing poll of a streak onward, re-recorded every poll with a stable message so the recorder folds
the repeats into one event with a rising count and a refreshed timestamp, keeping the cause beside
the symptom past the API server's one-hour event retention rather than an hour after a single write.

## What stays unwritten, and what lands it

| Status field               | Why it stays absent                                                                                                                                                                         | The series that lands it                                                                                                                                                                                                  |
| -------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `remediationsAppliedTotal` | An applied remediation is an approved one; approvals are recorded by the `tool_call_audit` plugin's `approval_*` records in the profile's audit file (`logs/audit.jsonl`), not as a metric. | Either a Hermes-side series from the same plugin, served from the `chat_metrics` listener, or a count of the broker's apply-side requests once those are distinguishable from reads. It is the one field still unwritten. |

It lands on the poller's existing path: a new row in the source table, a new series summed,
and the CRD description changed from "nothing writes it yet". It needs no second mechanism.
`sessionsTotal` left this table the same way, as two document counters under one status field,
because its halves aggregate differently and the fold aggregates per counter; the source table
has the row.

## Security and privacy

Zero external egress: the operator reads three in-cluster listeners over the pod network, four on a `next` install, and
writes two objects in the cluster. Zero PII or secret exposure: the status receives integer
totals, two integer gauges and one timestamp; label values are summed away and never written anywhere, and the
baseline ConfigMap holds pod UIDs, pod names and integers. The new NetworkPolicy rules admit
the operator's pods on the metrics ports, three on a `today` install and the A2A gateway's fourth on `next`, and nothing else; the collector's rule is
unchanged. No new RBAC: pods are listed and ConfigMaps managed with verbs the ClusterRole
already grants, and the status subresource is already the operator's to write. No credential
of any kind is involved; the listeners are unauthenticated by design, as they are for the
collector, and for the same reason the operator treats what it reads as untrusted. The gateway
pod shares its network namespace across the agent container, the sidecar and any container
`spec.deployment.sidecars` adds, and the watcher's port is free whenever its process is down, so
a body on any of the ports can come from something other than the listeners. Folding each line as
it is read keeps the operator's memory per poll at one line, under its memory limit whatever the
fleet's exposition grows to, and the per-poll ceiling bounds what one body can do to a total; neither makes the body more trusted. What is left is stated
rather than hidden: a workload in the pod can refuse the operator a count, can raise one by at
most `usageDeltaCeiling` per poll for as long as it holds the port, and can never set one. The
two cluster gauges are the exception, by their nature: they are a reading, not an accumulation,
so a body on the watcher's port sets them to whatever up-gauge series it serves, up to
`usageClusterGaugeCeiling`, past which the body is refused whole. Nothing reads them for a
decision; a value no fleet could have is the same visible drift as a counter rising on a quiet
install, and the same authentication would be the only way to close it. At
twelve polls an hour that is a visible drift, a counter rising on an install whose log shows no
commands and no triage, not a value chosen; closing it would mean authenticating the listeners,
which the collector's scrape does not do either. The scrape is therefore not the grant the
alternatives section refuses, a workload describing its own activity: it is a reader of counts
the workload can withhold or nudge, never write.

One more residual sits in the peer selector. The chart renders the CR into the operator's
namespace, and above one replica the agent's Role would hold `get` and `patch` on every pod there,
because RBAC cannot say "only your own pod". The operator runs the gateway as one replica today, so
no install renders that grant; on an HA chart install, if the cap were lifted, anything running as the
agent's ServiceAccount can put the operator's label on a pod of its choosing, the sandbox
included, and that pod is then admitted to the three metrics ports. What it gains is the
listeners' counters: the watcher's and the chat plugin's, which the agent container already reaches over its own pod's
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
ServiceAccount's write grants, leader-election leases and, at more than one replica (which the
operator does not render today), `get` and `patch` on the pods of its namespace, so the leader can
label itself, and the status is not
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
start time, a sample not below the last); the whole-sample branch (a pod created after the document was first recorded; a never-read
pod whose process started after it, on a Sum counter; a later start time), including such a pod above the ceiling, which is recorded and adds nothing, the first
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
largest-delta rule across two gateway pods and its agreement with the sum for one; the cluster
gauges (written on the first poll, falling with the watcher's reading, left as they were by one
failed scrape and cleared by the second in a row, the largest of each across two replicas, cleared when the watcher is off, and
never moving `lastActiveTime`, `lastHumanActiveTime` stamped by a chat message and not by a command, a proposal or a merge, and a CRD at a previous schema pruning it alone while the counters keep landing, a newer-field probe under a CRD without `status.usage` recorded in
the newer fields' own record and not the counters', and a CRD at a previous schema pruning the
gauges and the merged counter, or the blocked counter alone, while the first-schema counters keep landing); the watcher's body yielding a zero gauge reading and the
broker's none, and one past `usageClusterGaugeCeiling` refused; a re-seed that
scrapes two replicas at different samples, taking the larger delta on the next advance and counting
the furthest replica's pre-seed backlog once; the baseline-absent-with-counters-present case; a ConfigMap whose recorded CR UID is not the CR's
or whose values fail the read-back bounds, including a total above the float64-exact ceiling, one
below the status, and a first-recorded time in the future (treated as absent); a quiet poll writing no ConfigMap; the disabled-watcher
case (no gateway scrape, no log line); one broker body folded into six counters, each with its own baseline entry, the tool series read into two of them over disjoint statuses, through a restart, the failures counter and the closed-unmerged counter leaving `lastMoved` where it was; the gateway pod's two listeners read as two targets, the chat counter summed across replicas, the errored counter read from the plugin whole and from the A2A gateway's terminals under its own filter and leaving `lastMoved` where it was, the kanban counter read whole from the chat listener and leaving `lastMoved` where it was, the feedback counter read from the plugin whole and leaving `lastMoved` where it was, a failing chat listener with a streak and a Warning of its own and no effect on the gauges, the A2A gateway pod read as a fourth target into the chat counter with an entry, a streak and a Warning of its own; `sessionsTotal` as the sum of its two halves, the triage half taking the largest delta across gateway pods and the chat half the sum across the chat listener and the A2A gateway, either half moving `lastActiveTime`, a failing chat listener's Warning naming the status field and not a half, and the status value seeding the chat half and flooring the pair's sum; the watcher's body folded into the injected and the triage-sessions counters at once, the create attempts by outcome left unread, and the chat and A2A bodies into the messages and the chat-sessions counters at once; a current-layout document that predates a counter gaining its total rather than being re-seeded; a version-1 document migrated in place under a pruning CRD with its totals kept, the interval's deltas added and the new counter's first sample added whole, one whose migrating poll could not read the broker, so the unfolded counter is written with the document and the broker's first sample adds whole on the poll that reads it, and one migrated under a status that already carries the new counter, which starts at that floor and is not re-seeded; the Warning from a failing broker naming each of its fields; the Warning naming each status field a pod's counters project to; the series selection (the `status` values summed into each of the two tool
counters, and that the two sets are the broker's whole vocabulary; the injected series and not the observed one); and the port-by-name lookup when
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

On the broker's side, `test_credential_proxy_metrics.py` holds the version-control counter to its verb vocabulary, its five outcomes and its provider set (the forge a request resolved to; `none` on every exit before one did, the ungated `capabilities` verb included), one test per outcome the route reports, and renders the merged counter as one unlabelled sample; `test_vcs_broker.py` holds the merged count to its three rules: counted once across a list and a view and however the repository is spelled, not for another author, not for a merge before the process started, not for an open or closed proposal, and the identity asked once per repository and never for old merges; the same file holds the closed-unmerged count to the same rules, apart from a merge and an open proposal, counting a close a list has not seen on a view, counting a close and a merge in one read each in its own series, and never counting a close the broker made itself. `test_credential_proxy_metrics.py` renders the closed-unmerged counter as one unlabelled sample that a close moves and a merge does not. A live check, which is the acceptance criterion: on an install built from the branch, `remediationsProposedTotal` rises after a proposal the forge accepts and `remediationsMergedTotal` after a read shows it merged,
`toolExecutionsTotal` rises after commands run from the sandbox, `toolExecutionsBlockedTotal` after one the policy refuses, `eventsIngestedTotal` after
events the watcher accepts arrive and `chatMessagesInboundTotal` and `lastHumanActiveTime` after a chat message reaches the gateway, the latter unmoved by the commands the stall watch runs in between; a broker pod restart, a gateway pod restart, a restart of the
watcher process alone (the supervisor's), and an operator restart each leave the counters
where they were and they keep rising afterwards, the watcher's restart in particular adding nothing for
the replay of the retained Events; a quarter of an hour with no command, no accepted event and no chat message
produces no status write; and the policies show the new rule with the operator pod as the
only peer added.

## Documents the implementation changes

Each of these landed with the poller; the list is the record of where the facts moved, and the proposals counters, the two cluster gauges, the chat counter, the sessions counter, the blocked counter, the broker failures counter, the human time, the errored counter, the kanban counter, the feedback counter and the closed-unmerged counter followed later through the same pages.

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
- `docs/designs/cron-report-relay.md`, which recorded that the operator reads nothing the
  agent's pods write and binds no metrics endpoint of its own. The poller was the first
  exception and the operator's own gauges the second; that record points here for both.
- The map's identifier-sources table, which gains a row for the poll interval, the ConfigMap
  name and the operator-peer rule, with the event-watcher row as the model.
- The operator's own gauges (the section above): the observability page's metrics section
  names the three families and the scrape, the CRD reference's `status.usage` paragraph
  points there, the chart README documents `operator.metrics`, and the map gains a row for
  the family names and the field lists.
