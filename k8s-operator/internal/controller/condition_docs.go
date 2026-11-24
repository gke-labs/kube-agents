package controller

// The PlatformAgent's status conditions as the CRD reference page documents
// them (docs/site/src/content/docs/operator/platformagent-crd.md, the table
// under status). scripts/generate_docs.py renders the table from this slice
// through scripts/gen_condition_docs.go, which reads this file's syntax rather
// than importing the package, so every value here must be a string literal or
// a string constant of this package. The page and the controller cannot drift:
// TestEveryWrittenConditionIsDocumented fails when the controller writes a
// condition type that has no entry here, and docs-check fails when the page
// differs from what this renders. Add a row here with the constant the
// controller writes, then run `make docs-generate`.
//
// Written says when the condition is present; Meaning says what it holds and
// what to do about it. Both are markdown, rendered into a table cell as they
// stand. A Meaning that contains {reasons} has it replaced by the entry's
// Reasons, rendered as a code-spanned list ("`A`, `B` or `C`"). Reasons is a
// package-level []string variable, named rather than written inline.

// conditionDoc is one row of the page's conditions table.
type conditionDoc struct {
	Type    string
	Written string
	Meaning string
	Reasons []string
}

// degradedConditionReasons are the reasons the Degraded condition carries,
// for its row, in the order updateStatusReady ranks them. A parked pass reads
// the ones ahead of A2ABusStreamsMissing as ranked above it
// (degradedRanksAboveBusStreams), so a new reason goes where the chain puts
// it. Listed by constant, so a renamed reason fails to compile; a
// new reason has to be added here by hand (the writer passes it as a value,
// which the completeness test cannot follow).
var degradedConditionReasons = []string{
	conditionReasonInvalidGitRepoURL,
	conditionReasonCorruptManagedRepos,
	conditionReasonMinterPruningHeld,
	conditionReasonA2ABusStreamsMissing,
	conditionReasonA2AGatewayWaitingForTasks,
	conditionReasonInvalidCredentialProxyResources,
	conditionReasonInvalidAgentAPIAuthResources,
	conditionReasonInvalidShellSandboxResources,
	conditionReasonInvalidCredentialProxyExec,
	conditionReasonInvalidKanbanAdmission,
	reasonRBACIncomplete,
}

// platformAgentConditionDocs is the table, in page order.
var platformAgentConditionDocs = []conditionDoc{
	{
		Type:    readyConditionType,
		Written: "Always",
		Meaning: "Tracks `phase`; its `reason` and `message` carry whatever the reconcile is waiting on, including the reasons that park `phase: Degraded` (`ReservedServiceAccountName`, `RuntimeClassNotFound`, `ShellSandboxCannotBeDisabled`, `UniverseDomainMismatch`, `ForbiddenVolumeMount`, `EgressAllowlistRefused`, `ShellSandboxKeysMissing`, and `A2AProvisionFailed`). [What `Ready` reports](#what-ready-reports) says what `UniverseDomainMismatch`, `ShellSandboxKeysMissing`, and `A2AProvisionFailed` mean and how a failed provisioning Job is cleared. Under `spec.deployment.scaleToZero: true` it reads `False` with reason `ScaledToZero` beside `phase: Paused` once the gateway has no ready replica; the same section says when. With the A2A gateway's election on, the gateway counts as ready only while its elected leader is, after a 30-second grace from the leader's election; with no current leader (no holder, its pod gone, or a holder that stopped renewing) it counts its ready replicas.",
	},
	{
		Type:    degradedConditionType,
		Written: "Only while degraded",
		Meaning: "Something in the spec, cluster state, or operator install cannot be honoured. `Reason` is one of {reasons}; [What `Degraded` reports](#what-degraded-reports) says what each means. The refusals above ride the `Ready` condition instead of this one.",
		Reasons: degradedConditionReasons,
	},
	{
		Type:    busCredentialsReadyCondition,
		Written: "While a callout Deployment exists",
		Meaning: "Whether every replica the callout Deployment asks for is ready, and therefore both serving an identity map and attached to the bus. `Reason: CalloutServing` (`True`), `CalloutUnavailable`, or `CalloutAbsent`. The operator's own gates do not read it, and it reads `False` with `1 of 2 replicas ready` while the gateway exists and dispatches; [What `BusCredentialsReady` reports](#what-buscredentialsready-reports) has the gate it differs from.",
	},
	{
		Type:    dispatcherJobModeCondition,
		Written: "While the operator runs the profile dispatcher (on by default; `operator.profileDispatcher.enabled`) with the chart's `A2A_DISPATCHER_JOB_POLICY` signal set, from the first pass that reaches the dispatcher",
		Meaning: "Whether the dispatcher runs profile tasks as Jobs: unattended ones, and permit-carrying ones at any tier. `Reason: InPlace` (`True`), `PolicyMissing`, `NamespaceNotSelected`, `NamespaceNotReady`, `ForeignJobTemplate`, or `GrantFailed`; [What `DispatcherJobMode` reports](#what-dispatcherjobmode-reports) has each.",
	},
	{
		Type:    dispatcherCeilingCondition,
		Written: "While the profile dispatcher is switched on",
		Meaning: "How the dispatcher's identity-map entry and its config compare with what one shard and one ConfigMap can hold. `WithinBudget` (`True`); `NearBudget` (`True`), past four fifths of either, with a Warning event; `ProfilesRefused` (`False`), with a Warning event: the newest profiles aren't bound and the dispatcher serves the rest; `OverBudget` (`False`), with a Warning event, only from draining profiles: no dispatcher until their drains run out. About 650 profiles at two tiers; the config fills first only for profiles that declare many parameters.",
	},
	{
		Type:    a2aIdentityMapAppliedConditionType,
		Written: "Once the operator renders an identity map",
		Meaning: "Whether the auth callout took the identity map this operator rendered, read from every Ready callout pod's `/status` by pod IP, again every 30 seconds while unsettled. `True`, `Serving`: every Ready pod answered and serves it. `False`, `Refused`: a pod refused it and serves its previous map, which can be wider; the message quotes the refusal. `False`, `Pending`: a pod is still on an older map or mid-way through its shards. `Unknown`, `StatusUnreadable`: a Ready pod didn't answer and may hold a refusal, so turning to it is a Warning event (a NetworkPolicy on the callout must admit the operator). `Unknown`, `NoReadyCallout`: no pod is Ready. Removed when the A2A stack is off.",
	},
	{
		Type:    inferenceKeysServedCondition,
		Written: "While the namespace runs the inference gateway (the `litellm` Deployment)",
		Meaning: "Whether every Ready inference gateway pod has loaded the caller keys the operator mints, one per caller class: `agent`, `a2a-session`, `curator`, and `tier-<tier>` for each AgentProfile tier in use. Each key is in Secret `<name>-inference-key-<class>`, a tier's in the profile namespace, and the `litellm-caller-keys` Secret lists their SHA-256 digests. These names are reserved for the operator: it records the UID of each Secret it creates in `status.inferenceKeys`, and replaces any other Secret it finds at one of them, with an `InferenceKeySecretReplaced` Warning event naming it, so its key never reaches the map. After a rollback to a release that drops that record, every key is replaced once and the condition comes back on a later pass. The operator also mints a probe key, class `gateway-probe` in Secret `<name>-inference-key-gateway-probe`, which no caller is given and the gateway admits on `GET /v1/models` alone. Every map the operator writes carries a fresh probe key, its digest in the same update and the previous one dropped, so a pod still on an older map refuses it. It sends only the probe key, as `GET /v1/models` by pod IP, to each Ready pod the `litellm` Deployment owns through its ReplicaSet, skipping pods on the host network. `True`, `Served`: every pod answered 200. `False`, `NotServed`: a pod answered something else; the message names the pod and the status, and the operator looks again every 15 seconds, since a pod loads a new map when the kubelet refreshes its mounted Secret. `Unknown`, `ProbeIncomplete`: a pod didn't answer, and the operator looks again every minute. `Unknown`, `NoReadyGatewayPod`: no gateway pod is Ready, and the operator looks again every minute. `False`, `CRDOutdated`: the served PlatformAgent CRD has no `status.inferenceKeys`, so the operator writes no key Secret and no map until the current CRD is applied (Helm doesn't upgrade CRDs on `helm upgrade`); it looks again on its steady interval. `False`, `NotWritten`: the keys or the map weren't written, because another PlatformAgent shares the namespace, the CRD couldn't be read (nothing is written on that pass), a Secret at one of those names is being deleted (the operator creates its own once it's gone), a Secret to replace couldn't be checked against the record as the API server holds it (nothing is replaced on that pass), or an API call failed; the message says which.",
	},
	{
		Type:    a2aCalloutStalledConditionType,
		Written: "While the auth callout Deployment's rollout is past its progress deadline",
		Meaning: "`status: True`, `Reason: ProgressDeadlineExceeded`. The bus applies a config change without waiting for every callout replica to load the current identity map, so until the rollout finishes some connections may be placed by the previous map. Check the `<agent>-a2a-callout` Deployment's rollout (`kubectl rollout status`) and its pods' events. Removed once the rollout completes.",
	},
	{
		Type:    a2aGatewayConditionType,
		Written: "Only on an install upgraded from an operator that ran the A2A gateway at zero replicas for want of a chat backend: from the pass that scales it back to one replica until that replica is ready. Written and cleared by every pass that renders the stack, whichever phase it ends on (`Ready`, `Provisioning`, or `Degraded` for `A2AProvisionFailed` and `ShellSandboxKeysMissing`), so only a pass that stops before rendering leaves it as it was",
		Meaning: "`status: False`, `Reason: WaitingForReplica`. Every install runs the A2A gateway, on the console door alone when no chat backend is configured. After the upgrade the next pass renders it at one replica, `Ready` waits on it, and the condition clears once a replica is ready. [What `A2AGateway` reports](#what-a2agateway-reports) covers the older `NoChatBackend` reason and the `discord-bot` Secret.",
	},
	{
		Type:    busProvisionedConditionType,
		Written: "From the first pass that sees the bus provisioning Job complete, whichever phase that pass ends on; kept through the Job's later re-runs",
		Meaning: "`status: True`, `Reason: ProvisionJobComplete`. The record that lets `Ready` stop counting the Job: the finished Job is removed by its 24 h TTL and rendered again, and a spec edit renders a new one under a new digest, and neither re-run should flip a Ready install to `Provisioning` for the minute it takes. Until this condition exists the Job counts, so an install upgraded from an operator that never counted the Job (one that could read `Ready` over an unprovisioned bus) does not inherit a completion nobody saw. The message names the Job that first provisioned the bus. A re-run that fails is still reported as `A2AProvisionFailed`.",
	},
	{
		Type:    a2aProfileSupervisorFormCondition,
		Written: "`True` once, from the first pass that finds every profile-task reader (the A2A gateway, the agent pod and, with its door on, the MCP server) on this release's image and fully rolled out and the bus provisioning Job that adds the gateway relay's third filter complete; never removed. `False` while this install renders a reader on an image other than this release's (a pinned `spec.deployment.image`)",
		Meaning: "`status: True`, `Reason: SixTokenSupervisorSubject`. The dispatcher writes a profile task's terminal on the six-token supervisor subject (`a2a.tasks.p.<profile>.<task>.supervisor`) instead of the five-token one, and the gateway relay filters both. It is a latch: a later upgrade that rolls a reader again keeps the dispatcher on the six-token form rather than moving it back for the length of the roll. One pass after it is written, the operator renders `A2A_PROFILE_SUPERVISOR_CUTOFF` on the A2A gateway and the MCP server, set to this condition's `lastTransitionTime` plus ten minutes (on an install that latched earlier, that time may already be in the past at the upgrade), and mounts the `<agent>-a2a-profile-names` ConfigMap on both, which rolls each once: past that time they no longer take a five-token terminal as the end of a task addressed to a profile that ConfigMap lists. From the latch the dispatcher no longer falls back to the five-token form when the six-token one is refused, and settles the task without a terminal after its retry bound (`terminal-unpublished`). Until it is written, the dispatcher writes the five-token form, which every reader watches. `status: False`, `Reason: ReaderNotOnReleaseImage`: the operator renders the reader the message names on an image other than this release's, the hermes-bridge of a pinned `spec.deployment.image`, so the install stays on the five-token form. A later release that drops that form keeps it for this install while this holds. Setting the pin to this release's tag, or removing it, clears it. A reader still on an older release's image, before or during its rollout, writes nothing.",
	},
	{
		Type:    busStreamsPresentConditionType,
		Written: "Once `BusProvisioned` is True",
		Meaning: "Whether the streams and KV buckets the provisioning Job creates are still on the bus, read every five minutes from the bus's metrics exporter on port 7777. `True`, `AllPresent`: every one was seen. `False`, `StreamsMissing`: three polls in a row found the ones the message names missing, and the operator deletes the completed provisioning Job so it runs again and recreates them (`Reprovisioning` while it runs); once `TASKS` is back after a loss, however it came back, the operator restarts the A2A gateway and the agent pod whose client container has been running since before the loss, because their `TASKS` consumers don't come back on their own (`A2ABusClientsRestarted`); one that started since binds by itself, and a restart that fails is retried on every poll, with `Reprovisioning` saying so. A loss of `TASKS` counts once three polls in a row saw it, or once the operator re-runs the Job while it's missing. It's kept in the `kubeagents.x-k8s.io/a2a-bus-tasks-lost` annotation, so removing the re-run record doesn't clear an owed restart; removing that annotation does. Re-runs are at most one per 15 minutes and three per 6 hours, recorded in the `kubeagents.x-k8s.io/a2a-bus-reprovision` annotation. A re-run that completed without bringing some of them back stops the re-runs for exactly those names (`ReprovisionDidNotRestore`), each lifted on its own once seen back or all of them when the annotation is removed. A loss of any other stream is still confirmed and re-run while a stop holds, and its message names the stopped ones. A failed Job is not re-run. While `False`, `Degraded` reads `A2ABusStreamsMissing`. `Unknown`, `DetectionUnavailable`: the bus runs without its exporter ([`A2A_NATS_EXPORTER_IMAGE`](/kube-agents/deploy/docker-images/)). `Unknown`, `ExporterUnreachable`: three polls in a row had no usable answer. No verdict is taken while the bus rolls, converts or provisions, and the condition keeps its last one meanwhile, however long that lasts; the bus's own conditions report the rollout.",
	},
	{
		Type:    a2aVerifierConditionType,
		Written: "While the capability verifier's Deployment exists and has no ready replica. A pass that cannot read the Deployment leaves it as it stands",
		Meaning: "`status: False`, `Reason: VerifierNotReady`. A capability check that gets no answer is refused, so every submission is refused terminally while this holds. The CR stays `Ready`: the verifier is on the request path, not the readiness path. Check the `<agent>-a2a-verifier` Deployment's pods and their logs, including the previous container's: a verifier that cannot bind its capability bucket exits, and its log says why. Removed once a replica is ready.",
	},
	{
		Type:    a2aBusMetaConditionType,
		Written: "On a multi-server bus, on a pass that checks the JetStream meta group before creating the provision Job: present while the group isn't formed, a server isn't running, or a server's view can't be read, and removed on the pass that sees it formed. Also removed on any pass where the provision Job already exists, on a one-server bus, on a pass where another hold (the callout, or the bus rolling out) keeps the check from running, and when the mode flips to `today`",
		Meaning: "`status: False`. `Reason: ServerOutsideMetaGroup` names the server or servers that don't report the meta leader a quorum agrees on, or that the leader doesn't list as current. The provision Job waits, so no stream is created, and the CR isn't `Ready`. A server still outside 3 minutes later is restarted once, with an `A2ABusServerRestarted` Warning, when it is the only one outside, the rest are a quorum, the bus hasn't been provisioned yet with no server holding a stream or consumer, and its own meta node has started. A server already restarted once and still outside is named as left for a person: delete its pod, or wipe the bus. When no leader is agreed at all, on a fresh bus under the same guards (never provisioned, no server holding a stream or consumer, every meta node started, none restarted before), every server is restarted once after the same 3 minutes, with an `A2ABusMetaGroupRestarted` Warning; a bus still without a leader after that, or one where a server was already restarted, is named as left for a person (wipe the bus). `Reason: ServerNotRunning` names a server whose pod is absent, being deleted, or not Running while the others run, such as the one just restarted: the Job waits for it, and nothing is restarted on that account. `Reason: MetaGroupUnknown` names a running server whose view couldn't be read, or says no server is running; nothing is held or restarted on its account. The check also runs when the Job is re-created on a running bus, where it can hold the re-run and set this condition; it never restarts anything on a bus that has been provisioned, so it doesn't repair a server that drops out of a running bus.",
	},
	{
		Type:    gatewayElectedConditionType,
		Written: "With the election on, from the first pass that reads a gateway pod holding the Lease. Kept while the gateway runs that image or the operator's own release. Removed on a roll to another image, or with the election off",
		Meaning: "`status: True`, `Reason: LeaseHeld`. The message is the image the Lease holder runs. The operator renders `availability.a2aGateway.replicas` gateway replicas only while it is set, since a gateway from before the election would act beside an elected one. A status condition doesn't survive most restores, so a restored install runs one replica until a gateway takes the Lease again.",
	},
	{
		Type:    eventWatcherConditionType,
		Written: "Only while `eventWatcher.enabled` is `false`",
		Meaning: "`status: False`, `Reason: DisabledBySpec`. The emergency stop is still pressed and no cluster events are reaching the agent.",
	},
	{
		Type:    dataVolumeNearlyFullCondition,
		Written: "While the operator has a reading of the data volume",
		Meaning: "`True` (`NearlyFull`) from 90% used, back to `False` (`HasSpace`) only below 85%, so a volume at the line does not flap. A full volume fails every task-board and session write with a bare `disk I/O error`, so this is the early warning. It does not change `phase` or `Ready`, since the agent still answers. The message holds no figure and points to `storageStatus` and the `kubeagents_data_volume_bytes` metric; `freeBytes` is current to within a hundredth of the volume, and exact when the status changes. The reading comes from the event watcher, so it clears at once while the watcher is disabled or answers ready without the volume series. With no fleet reading (unreachable or `k8s_event_watcher_watch_set_ready` below `1`), it holds for one poll and clears after two in a row. The Session KV server in the agent container makes the same check and posts to the home channel when usage crosses 90% and when it is back below 85%.",
	},
	{
		Type:    kanbanDispatcherFailingCondition,
		Written: "From the first time the task-board dispatcher fails for a whole poll, or a tick on a board runs past 300 s or fails on a database lock after a busy timeout",
		Meaning: "`True` with reason `TicksFailing` once a board's dispatcher fails 60 ticks in a row (about one five-minute poll), or 3 in a streak at least a poll old. `True` with reason `TickStuck` once a board's tick runs past 300 s or fails on a database lock after waiting the busy timeout (120 s by default), as on a locked board; a stuck board takes precedence over a streak, and a good tick that runs past 300 s can set it for one poll. The message names the board with the last error class, or the stuck board, and no tick count or duration; it is written again when the reason, the board, the error class or the count of failing boards changes. Cards on that board are not dispatched; a full data volume is a common cause. `False` (`Ticking`) at the first poll where no board meets any rule. A good tick resets a streak and a stuck board, and a quick failure of another kind ends a stuck board; a board the dispatcher stops trying drops out after ten minutes. It does not change `phase` or `Ready`. A poll that cannot read the gateway's chat metrics listener leaves it as it is. A board quarantined as corrupt drops out after ten minutes too.",
	},
	{
		Type:    scopeUndeclaredCondition,
		Written: "While `spec.scope` names no project, folder, organization, Shared VPC host or Metrics Scope, `spec.harness` names the host cluster, and the event watcher registers a cluster in the management project other than the host",
		Meaning: "`status: True`, `Reason: ClustersAdoptedFromProject`. Without a membership list the reconcile adopts every GKE cluster in the management project. The message counts them, names the first eight as `name (project/location)`, and points at the remedy: name the management project in `spec.scope.projects` to keep them, or list each under `spec.scope.exclude.clusters` ([`spec.scope`](#specscope)); on an installer-made install, set `SCOPE_PROJECTS` or `SCOPE_EXCLUDE_CLUSTERS` in `install.env`. The management project stays in scope under any declaration, so a scope that names other projects silences the condition without retiring the neighbours. Written by the usage poll from the watcher's `k8s_event_watcher_cluster_up` series. Absent with the watcher disabled or after two polls in a row without a fleet reading, and removed once that project registers no cluster beyond the host or a scope is declared. Not `Degraded`; `Ready` is unaffected.",
	},
	{
		Type:    homeChannelUnsetConditionType,
		Written: "While the A2A gateway holds a chat backend whose `homeChannel` is empty (Google Chat, Slack, or Microsoft Teams with the operator's `A2A_TEAMS_BACKEND` switch on, any of them) and no learned channel or space is admitted for it",
		Meaning: "`status: True`, `Reason: NoHomeChannel`. Conversation replies still arrive. When another platform has a home channel, proactive posts go there meanwhile (Chat's first, then Slack's, then Teams'). When no platform has one, the gateway refuses them and keeps them for one digest once a channel is set. The message names each platform; the `install.env` key beside the field only for the platforms the installer configures. Not `Degraded`; `Ready` is unaffected. Written and cleared on a pass that ends `Ready` or `Provisioning`, and on one that renders the A2A stack and then parks `Degraded` (`ShellSandboxKeysMissing`, `A2AProvisionFailed`); a refusal that returns before the render leaves it as it stands. Set `homeChannel`, or under `homeChannelPolicy` `auto` mention the agent in a channel or a space, or on Slack pick one on the Home tab ([`spec.integration`](#specintegration) has the admission rule).",
	},
	{
		Type:    a2aBusMigratingConditionType,
		Written: "While a bus migration runs (True), and after one ended without converting the bus (False, `Abandoned`) or stopped for a person (False, `Failed`); absent once one converted the bus, or when none has run",
		Meaning: "`status: True`, `Reason: InProgress` while a run is under way; the message names its phase, which `status.busMigration` also carries. `False`, `Abandoned`: the run ended and the bus runs on as before, data kept. `False`, `Failed`: the run stopped where a person has to act, and the message names the step. A reshape (leg `Reshape`, [recreating TASKS wider](#recreating-tasks-wider)) reads the same way, its message naming TASKS rather than a server count. `True`, `LegUnknown`: the run's leg is one this operator does not run, a later release's found after a rollback; it is held where it is, with the freeze, until that release is back or a person abandons it ([a run this operator does not run](#a-run-this-operator-does-not-run)). An install upgraded from an operator that refused bus conversions loses that operator's `A2ABusConversionRefused` condition on its first status write. [Changing the bus's server count](#changing-the-buss-server-count) has the phases.",
	},
	{
		Type:    noChatPlatformConditionType,
		Written: "While neither Google Chat nor Slack is enabled, and the gateway's Microsoft Teams backend is not armed",
		Meaning: "`status: True`, `Reason: NoChatPlatform`. Nothing the agent raises unprompted reaches chat. Its audit findings stay in the report store on the data volume and reach the GitOps repository (ledger issues, fix pull requests) when one is registered; alerts are still triaged on the board; other scheduled reports are not delivered. Ask the agent for the audits in the console's chat pane, or read the volume with `kubectl exec`. Not `Degraded`; `Ready` is unaffected. A Discord-backed gateway carries it too, since no notifier posts to Discord. Written and cleared on the same passes as `HomeChannelUnset`. Enable one platform (`spec.integration.slack` or `spec.integration.googleChat` with `enabled: true`, or `spec.integration.teams` on an operator running with `A2A_TEAMS_BACKEND=true`) to clear it.",
	},
	{
		Type:    shellSandboxImagePinnedConditionType,
		Written: "While `spec.harness.experimental.shellSandbox.image` names an image other than the one the operator would run without it",
		Meaning: "`status: True`, `Reason: ImageDiffersFromOperator`. The message names both images. A pin survives operator and agent upgrades, and a sandbox older than the agent fails every `kubectl` call at the credential proxy with a policy refusal. Not `Degraded`; `Ready` is unaffected, since a pin to a mirror or a test build can be deliberate. Written and cleared on a pass that ends `Ready` or `Provisioning`, and on one that parks `Degraded` on `ShellSandboxKeysMissing` or `A2AProvisionFailed`. Clear the field, or set it to the operator's image, to clear it.",
	},
	{
		Type:    a2aMCPIdentityConditionType,
		Written: "While the operator runs the MCP server (on by default; `operator.mcpServer.enabled`) and a ServiceAccount named `<agent>-a2a-mcp` or a Secret named `<agent>-a2a-mcp-token` exists that this PlatformAgent does not control",
		Meaning: "`status: True`, `Reason: UnownedServiceAccount` or `UnownedTokenSecret`. Both names are reserved: an account somebody else created would run the MCP server as whatever Workload Identity it carries, and a foreign Secret would arm it with their credential. So the server is not rendered; a server and Service rendered earlier are removed, while the fence and the operator's own token stay. For an unowned account the broker's caller list and `CREDENTIAL_PROXY_MCP_CALLERS` do not name it. `Ready` is unaffected. The message names the object. Delete it; the condition clears on the next periodic pass, within 15 minutes. A controller reference is not a remedy: the operator would adopt the object as it is, annotations and contents included.",
	},
	{
		Type:    scopeResolvedConditionType,
		Written: "While `spec.scope` declares a folder, organization, Shared VPC host or Metrics Scope",
		Meaning: "A Job named `<agent>-scope-publisher-<suffix>` (shortened for long names) resolves those members into the `<agent>-resolved-scope` ConfigMap 15 minutes after each start, or at once for a new membership when no run is active. `True`, `Resolved`: a lookup answered within twice the sum of 15 minutes and 5 minutes per started 100 of `maxProjects` (40 minutes at the default). `False` reasons: `Missing`, no readable result yet. `Stale`, no lookup answered within that bound. `HashMismatch`, the result predates the membership. `TooLarge`, the members did not fit. `Error`, read the Job's log. `UnownedServiceAccount`, somebody else owns `<agent>-scope-publisher`; delete it; it clears within 15 minutes. `ScopeRoleNotConferred`, the install has no `kube-agents-a2a-gateway-pod` admission policy from the chart, which the broker's scope role needs. `Ready` is unaffected.",
	},
	{
		Type:    builtinSchedulesConditionType,
		Written: "While something about the built-in AgentSchedules needs saying",
		Meaning: "`status: False`, one of {reasons}. `OverrideUnknown`: an entry in `spec.schedules.overrides` names no built-in schedule, and is ignored; overrides key on the rendered name, so a canary's entry is `<name>-canary`. `OverrideRefused`: an override sets `suspend: false` on a run-now-only schedule such as `onboarding`, which would put it on its nominal cron; only `suspend: true` is taken there. `WritesForbidden`: a write the renderer needs was refused with a 403; the message names the verb, the schedule and the API's reason. If RBAC refused it, as an operator image ahead of its RBAC sees, apply the chart's RBAC: the writes are on the operator's namespaced Role in the install namespace. `ReplacedUnownedSchedule`: an AgentSchedule sat on a reserved built-in name without the operator's marks, and was deleted to be created again as the built-in (an Event says so too). Built-in names are reserved; don't create a schedule under one. Fix the override, or let the replacement finish, to clear it.",
		Reasons: builtinSchedulesConditionReasons,
	},
	{
		Type:    hostPathDroppedConditionType,
		Written: "Only after a pass that rendered the Pod template around a `hostPath` on `spec.deployment.extraVolumes` or `.sidecarVolumes`, or a volume or mount that would hand a container the credential proxy's watcher token",
		Meaning: "`status: True`, `Reason: HostPathVolumeDropped` or `WatcherTokenVolumeDropped`. The render leaves the named `hostPath` or watcher-token entries and every mount naming them out of the Pod template the operator applies, and the message lists them by field and index. Not `Degraded`: the Pod runs without them. [What `VolumesDropped` reports](#what-volumesdropped-reports) says which passes write it and which leave it as it stands. Remove the entries to clear it.",
	},
	{
		Type:    replicasLimitedConditionType,
		Written: "Only while `spec.deployment.availability.replicas` is above 1, after a pass that rendered the gateway",
		Meaning: "`status: True`, `Reason: HighAvailabilityNotSupported`. The operator runs the gateway as a single replica, whatever the field asks for; the message names the requested count and why a second replica does not start. Not `Degraded`: the single replica runs. Written on whichever status the pass ends on, on the same terms as `VolumesDropped`: a refusal that returns before the render neither writes nor clears it. Set the field to 1, or remove it, to clear it.",
	},
	{
		Type:    credentialProxyExecReadyConditionType,
		Written: "Only while `credentialProxy.exec.replicas` is above 0 and the agent's name is accepted for the tier",
		Meaning: "`status: True`, `Reason: AllReplicasReady` when every exec-tier replica is Ready. `status: False` otherwise: `Reason: Unschedulable` when the scheduler cannot place a replica, with the pod's name and the scheduler's message (often `Insufficient cpu`); `Reason: ReplicasUnavailable` for any other shortfall. The message counts the Ready replicas against the field. Re-read every 30 s while short; a failed read leaves it as it stands. Not `Degraded`: a caller that cannot reach the tier falls back to the stateful broker. Each replica needs a node with 2 free CPU. Removed when the field is 0 or unset, or when the name is refused (`InvalidCredentialProxyExec`). A park that returns before the broker is reconciled (`ReservedServiceAccountName`, `ForbiddenVolumeMount`, `ShellSandboxCannotBeDisabled`, `RuntimeClassNotFound`, `UniverseDomainMismatch`) leaves it as it stands, count included, until the park clears.",
	},
	{
		Type:    sizingCappedByQuotaConditionType,
		Written: "Only while `spec.deployment.sizingProfile` is `auto` and the cluster count earns a higher tier than the namespace's ResourceQuota has room for, or than a LimitRange bound admits, or after a quota refused the pod a step recreated",
		Meaning: "`status: True`, `Reason: ResourceQuotaShort` or `LimitRangeShort`. The message names the tier earned, the tier the step was measured from, the tier that stays, and the short quota and key or the LimitRange, bound, resource and container; the figures go in the Warning event. Not `Degraded`: the running tier keeps running. Written before the render, so an earlier refusal neither writes nor clears it. Cleared by the pass that finds room or whose count no longer earns the step; a pass with no reading leaves it, so a gateway outage neither clears nor re-records it. The event watcher switched off by spec clears it. Raise the quota or the bound, or name the tier in `sizingProfile`.",
	},
	{
		Type:    sessionCapFittedConditionType,
		Written: "Only while the session cap is fitted below `maxSessions` on an install that sets `spec.harness.tuning.defaultRoute: session`, the install fell back to `platform`, its cap waits on its first reading of the bus, or the served CRD drops the cap. Never on `platform`, which an unset field resolves to",
		Meaning: "`status: True`, `Reason: TasksStreamNarrow`: the live `TASKS` stream has room for fewer sessions than `maxSessions`, so `status.effectiveSessionCap` holds the fitted cap, which the gateway's cap, the session quota and the provision Job follow. `Reason: NoRoomForASession`: not one session fits, so plain conversations go to `platform`. The cap stays fitted until `TASKS` is widened: on a one-server bus, setting `busReplicas` to 3 widens it and keeps its history; on any bus, a new value on the `kubeagents.x-k8s.io/bus-reshape` annotation recreates it at the rendered width, keeping its messages. `status: Unknown`, `Reason: WaitingOnBusWidth`: on a provisioned bus the width can't be read yet, so plain conversations go to session pods at `maxSessions` and the provision Job waits until it is read. `status: True`, `Reason: StatusFieldPruned`: the served PlatformAgent CRD is older than the operator and drops `status.effectiveSessionCap`, so plain conversations stay with `platform` until the chart's `crds/` is applied; the operator checks again every five minutes. The message carries no figures; the Warning event names the live width, what `maxSessions` needs and the fitted cap. Removed once none of these holds, and on `platform`.",
	},
	{
		Type:    dataVolumeExpansionConditionType,
		Written: "Only while a data claim is smaller than `spec.storage.size` (or the 50Gi default, raised by `expectedClusterProfiles` for the agent's claim) and its StorageClass cannot expand it",
		Meaning: "`status: True`, `Reason: StorageClassCannotExpand`. The message names each blocked claim, its current request, the size asked for, and why the class cannot expand it. Not `Degraded`: the agent runs on the smaller volume. Written on whichever status the pass ends on, `Ready` or `Degraded`, and removed once the claims can grow or the size no longer asks for more. After an operator restart it is left as it stands until the next pass reads the claims. Set `allowVolumeExpansion: true` on the class to clear it. The agent's claim, when it is below that size and will not grow (its class cannot expand it, or this PlatformAgent does not control it), also gets one `DataVolumeUndersized` Warning Event per state.",
	},
	{
		Type:    externalModelEgressConditionType,
		Written: "Only while the resolved Google Cloud universe domain is not `googleapis.com` and `status.inference.provider` is an external SaaS provider (any provider other than `openai_compatible`, `vertex_ai`, `hosted_vllm`, `vllm`, `ollama`, or `custom`)",
		Meaning: "`status: True`, `Reason: ExternalSaaSProviderInCustomUniverse`. Prompts, cluster telemetry excerpts, and tool results leave the sovereign or partner universe boundary to reach the external model provider. Not `Degraded`: `Ready` is unaffected, since an operator may authorize external model egress deliberately. Switch LiteLLM to an in-universe endpoint (`openai_compatible` or `vertex_ai` with `platformAgent.harness.universeDomain` set on the chart) to clear it.",
	},
}
