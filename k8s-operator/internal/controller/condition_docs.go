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
// for its row. Listed by constant, so a renamed reason fails to compile; a
// new reason has to be added here by hand (the writer passes it as a value,
// which the completeness test cannot follow).
var degradedConditionReasons = []string{
	conditionReasonInvalidGitRepoURL,
	conditionReasonCorruptManagedRepos,
	conditionReasonMinterPruningHeld,
	conditionReasonInvalidCredentialProxyResources,
	conditionReasonInvalidAgentAPIAuthResources,
	conditionReasonInvalidShellSandboxResources,
	conditionReasonInvalidCredentialProxyExec,
	reasonRBACIncomplete,
}

// platformAgentConditionDocs is the table, in page order.
var platformAgentConditionDocs = []conditionDoc{
	{
		Type:    readyConditionType,
		Written: "Always",
		Meaning: "Tracks `phase`; its `reason` and `message` carry whatever the reconcile is waiting on, including the reasons that park `phase: Degraded` (`RuntimeClassNotFound`, `ShellSandboxCannotBeDisabled`, `ForbiddenVolumeMount`, `EgressAllowlistRefused`, `ModeNotRecognized`, `ShellSandboxKeysMissing`, and under `mode: next` `A2AProvisionFailed`). [What `Ready` reports](#what-ready-reports) says what the last three mean and how a failed provisioning Job is cleared. Under `spec.deployment.scaleToZero: true` it reads `False` with reason `ScaledToZero` beside `phase: Paused` once the gateway has no ready replica; the same section says when.",
	},
	{
		Type:    degradedConditionType,
		Written: "Only while degraded",
		Meaning: "Something in the spec, cluster state, or operator install cannot be honoured. `Reason` is one of {reasons}; [What `Degraded` reports](#what-degraded-reports) says what each means. The refusals above ride the `Ready` condition instead of this one.",
		Reasons: degradedConditionReasons,
	},
	{
		Type:    busCredentialsReadyCondition,
		Written: "Under `mode: next`, and while a callout Deployment exists",
		Meaning: "Whether every replica the callout Deployment asks for is ready, and therefore both serving an identity map and attached to the bus. `Reason: CalloutServing` (`True`), `CalloutUnavailable`, or `CalloutAbsent`. The operator's own gates do not read it, and it reads `False` with `1 of 2 replicas ready` while the gateway exists and dispatches; [What `BusCredentialsReady` reports](#what-buscredentialsready-reports) has the gate it differs from and when it is removed.",
	},
	{
		Type:    dispatcherJobModeCondition,
		Written: "Under `mode: next`, while the operator runs the profile dispatcher (`A2A_DISPATCHER_IMAGE`) with the chart's `A2A_DISPATCHER_JOB_POLICY` signal set, from the first pass that reaches the dispatcher",
		Meaning: "Whether the dispatcher runs unattended profile tasks as Jobs. `Reason: InPlace` (`True`), `PolicyMissing`, `NamespaceNotSelected`, `NamespaceNotReady`, `ForeignJobTemplate`, or `GrantFailed`; [What `DispatcherJobMode` reports](#what-dispatcherjobmode-reports) has each.",
	},
	{
		Type:    a2aIdentityMapAppliedConditionType,
		Written: "Under `mode: next`, once the operator renders an identity map",
		Meaning: "Whether the auth callout took the identity map this operator rendered, read from every Ready callout pod's `/status` by pod IP, again every 30 seconds while unsettled. `True`, `Serving`: every Ready pod answered and serves it. `False`, `Refused`: a pod refused it and serves its previous map, which can be wider; the message quotes the refusal. `False`, `Pending`: a pod is still on an older map or mid-way through its shards. `Unknown`, `StatusUnreadable`: a Ready pod didn't answer and may hold a refusal, so turning to it is a Warning event (a NetworkPolicy on the callout must admit the operator). `Unknown`, `NoReadyCallout`: no pod is Ready. Removed when the A2A stack is off.",
	},
	{
		Type:    a2aCalloutStalledConditionType,
		Written: "Under `mode: next`, while the auth callout Deployment's rollout is past its progress deadline",
		Meaning: "`status: True`, `Reason: ProgressDeadlineExceeded`. The bus applies a config change without waiting for every callout replica to load the current identity map, so until the rollout finishes some connections may be placed by the previous map. Check the `<agent>-a2a-callout` Deployment's rollout (`kubectl rollout status`) and its pods' events. Removed once the rollout completes.",
	},
	{
		Type:    a2aGatewayConditionType,
		Written: "Under `mode: next`, only on an install upgraded from an operator that ran the A2A gateway at zero replicas for want of a chat backend: from the pass that scales it back to one replica until that replica is ready. Written and cleared by every pass that renders the stack, whichever phase it ends on (`Ready`, `Provisioning`, or `Degraded` for `A2AProvisionFailed` and `ShellSandboxKeysMissing`), so only a pass that stops before rendering leaves it as it was",
		Meaning: "`status: False`, `Reason: WaitingForReplica`. Every `mode: next` install runs the A2A gateway, on the console door alone when no chat backend is configured. After the upgrade the next pass renders it at one replica, `Ready` waits on it, and the condition clears once a replica is ready. [What `A2AGateway` reports](#what-a2agateway-reports) covers the older `NoChatBackend` reason and the `discord-bot` Secret.",
	},
	{
		Type:    busProvisionedConditionType,
		Written: "Under `mode: next`, from the first pass that sees the bus provisioning Job complete, whichever phase that pass ends on; kept through the Job's later re-runs and removed with the stack when the mode flips to `today`",
		Meaning: "`status: True`, `Reason: ProvisionJobComplete`. The record that lets `Ready` stop counting the Job: the finished Job is removed by its 24 h TTL and rendered again, and a spec edit renders a new one under a new digest, and neither re-run should flip a Ready install to `Provisioning` for the minute it takes. Until this condition exists the Job counts, so an install upgraded from an operator that never counted the Job (one that could read `Ready` over an unprovisioned bus) does not inherit a completion nobody saw. The message names the Job that first provisioned the bus. A re-run that fails is still reported as `A2AProvisionFailed`.",
	},
	{
		Type:    a2aVerifierConditionType,
		Written: "Under `mode: next`, while the capability verifier's Deployment exists and has no ready replica. A pass that cannot read the Deployment leaves it as it stands",
		Meaning: "`status: False`, `Reason: VerifierNotReady`. A capability check that gets no answer is refused, so every submission is refused terminally while this holds. The CR stays `Ready`: the verifier is on the request path, not the readiness path. Check the `<agent>-a2a-verifier` Deployment's pods and their logs, including the previous container's: a verifier that cannot bind its capability bucket exits, and its log says why. Removed once a replica is ready.",
	},
	{
		Type:    gatewayElectedConditionType,
		Written: "Under `mode: next` with the election on, from the first pass that reads a gateway pod holding the Lease. Kept while the gateway runs that image or the operator's own release. Removed on a roll to another image, with the election off, or when the mode flips to `today`",
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
		Meaning: "`True` (`NearlyFull`) from 90% used, back to `False` (`HasSpace`) only below 85%, so a volume at the line does not flap. A full volume fails every task-board and session write with a bare `disk I/O error`, so this is the early warning. It does not change `phase` or `Ready`, since the agent still answers. The message carries the figures from when the status last changed; `storageStatus.freeBytes` is current to within a hundredth of the volume. The reading comes from the event watcher, so with the watcher disabled or unreachable the condition is absent. The Session KV server in the agent container makes the same check and posts to the home channel when usage crosses 90% and when it is back below 85%.",
	},
	{
		Type:    kanbanDispatcherFailingCondition,
		Written: "From the first time the task-board dispatcher fails for a whole poll",
		Meaning: "`True` (`TicksFailing`) once the dispatcher has failed 60 ticks in a row on a board, about one five-minute poll at its 5-second interval. The message names the board, the streak and the last error class. Cards on that board are not dispatched; a full data volume is a common cause. `False` (`Ticking`) at the first poll where every streak is below 60. A good tick resets a streak, and a board the dispatcher stops ticking drops out after ten minutes. It does not change `phase` or `Ready`. A poll that cannot read the gateway's chat metrics listener leaves the condition as it is. A board quarantined as corrupt is not counted.",
	},
	{
		Type:    scopeUndeclaredCondition,
		Written: "While `spec.scope` names no project, folder, organization, Shared VPC host or Metrics Scope, `spec.harness` names the host cluster, and the event watcher registers a cluster in the management project other than the host",
		Meaning: "`status: True`, `Reason: ClustersAdoptedFromProject`. Without a membership list the reconcile adopts every GKE cluster in the management project. The message counts them, names the first eight as `name (project/location)`, and points at the remedy: name the management project in `spec.scope.projects` to keep them, or list each under `spec.scope.exclude.clusters` ([`spec.scope`](#specscope)); on an installer-made install, set `SCOPE_PROJECTS` or `SCOPE_EXCLUDE_CLUSTERS` in `install.env`. The management project stays in scope under any declaration, so a scope that names other projects silences the condition without retiring the neighbours. Written by the usage poll from the watcher's `k8s_event_watcher_cluster_up` series; absent with the watcher disabled or unreachable, removed once that project registers no cluster beyond the host or a scope is declared. Not `Degraded`; `Ready` is unaffected.",
	},
	{
		Type:    homeChannelUnsetConditionType,
		Written: "Under `mode: next`, while the A2A gateway holds a chat backend (Google Chat, or Slack with Google Chat off) whose `homeChannel` is empty (on Slack: and no learned channel is admitted)",
		Meaning: "`status: True`, `Reason: NoHomeChannel`. Proactive posts are refused while conversation replies still arrive. The gateway keeps them for one digest once a channel is set; with Slack on too they go to the Slack Hermes platform, which delivers them only if Slack has a home channel. The message says which. Not `Degraded`; `Ready` is unaffected. Written and cleared on a pass that ends `Ready` or `Provisioning`, and on one that renders the `next` stack and then parks `Degraded` (`ShellSandboxKeysMissing`, `A2AProvisionFailed`); a refusal that returns before the render leaves it as it stands. Set `homeChannel`, or on Slack under `homeChannelPolicy` `auto` mention the agent in a channel or pick one on the Home tab ([`spec.integration`](#specintegration) has the admission rule).",
	},
	{
		Type:    a2aBusConversionRefusedConditionType,
		Written: "Under `mode: next`, while `spec.harness.tuning.busReplicas` asks the existing A2A bus for a server count other than the one it runs",
		Meaning: "`status: True`, `Reason: ConversionNotSupported`. The bus keeps its count, since changing an existing bus's count is not supported yet. Setting the field to the running count, or removing it, clears the condition. Advisory: Ready stays on the bus that runs.",
	},
	{
		Type:    noChatPlatformConditionType,
		Written: "Under `mode: next`, while neither Google Chat nor Slack is enabled",
		Meaning: "`status: True`, `Reason: NoChatPlatform`. Nothing the agent raises unprompted reaches chat. Its audit findings stay in the report store on the data volume and reach the GitOps repository (ledger issues, fix pull requests) when one is registered; alerts are still triaged on the board; other scheduled reports are not delivered. Ask the agent for the audits in the console's chat pane, or read the volume with `kubectl exec`. Not `Degraded`; `Ready` is unaffected. A Discord-backed gateway carries it too, since no notifier posts to Discord. Written and cleared on the same passes as `HomeChannelUnset`. Enable one platform (`spec.integration.slack` or `spec.integration.googleChat` with `enabled: true`) to clear it.",
	},
	{
		Type:    shellSandboxImagePinnedConditionType,
		Written: "While `spec.harness.experimental.shellSandbox.image` names an image other than the one the operator would run without it",
		Meaning: "`status: True`, `Reason: ImageDiffersFromOperator`. The message names both images. A pin survives operator and agent upgrades, and a sandbox older than the agent fails every `kubectl` call at the credential proxy with a policy refusal. Not `Degraded`; `Ready` is unaffected, since a pin to a mirror or a test build can be deliberate. Written and cleared on a pass that ends `Ready` or `Provisioning`, and on one that parks `Degraded` on `ShellSandboxKeysMissing` or `A2AProvisionFailed`, in either mode. Clear the field, or set it to the operator's image, to clear it.",
	},
	{
		Type:    a2aMCPIdentityConditionType,
		Written: "Under `mode: next` with the operator's `A2A_MCP_DOOR` flag on, while a ServiceAccount named `<agent>-a2a-mcp` or a Secret named `<agent>-a2a-mcp-token` exists that this PlatformAgent does not control",
		Meaning: "`status: True`, `Reason: UnownedServiceAccount` or `UnownedTokenSecret`. Both names are reserved: an account somebody else created would run the MCP server as whatever Workload Identity it carries, and a foreign Secret would arm it with their credential. So the server is not rendered; a server and Service rendered earlier are removed, while the fence and the operator's own token stay. For an unowned account the broker's caller list and `CREDENTIAL_PROXY_MCP_CALLERS` do not name it. `Ready` is unaffected. The message names the object. Delete it; the condition clears on the next periodic pass, within 15 minutes. A controller reference is not a remedy: the operator would adopt the object as it is, annotations and contents included.",
	},
	{
		Type:    scopeResolvedConditionType,
		Written: "Under `mode: next`, while `spec.scope` declares a folder, organization, Shared VPC host or Metrics Scope",
		Meaning: "A Job named `<agent>-scope-publisher-<suffix>` (shortened for long names) resolves those members into the `<agent>-resolved-scope` ConfigMap 15 minutes after each start, or at once for a new membership when no run is active. `True`, `Resolved`: a lookup answered within twice the sum of 15 minutes and 5 minutes per started 100 of `maxProjects` (40 minutes at the default). `False` reasons: `Missing`, no readable result yet. `Stale`, no lookup answered within that bound. `HashMismatch`, the result predates the membership. `TooLarge`, the members did not fit. `Error`, read the Job's log. `UnownedServiceAccount`, somebody else owns `<agent>-scope-publisher`; delete it; it clears within 15 minutes. `ScopeRoleNotConferred`, the install has no `kube-agents-a2a-gateway-pod` admission policy from the chart, which the broker's scope role needs. `Ready` is unaffected.",
	},
	{
		Type:    hostPathDroppedConditionType,
		Written: "Only after a pass that rendered the Pod template around a `hostPath` on `spec.deployment.extraVolumes` or `.sidecarVolumes`",
		Meaning: "`status: True`, `Reason: HostPathVolumeDropped`. The render leaves the named `hostPath` entries and every mount naming them out of the Pod template the operator applies, and the message lists them by field and index. Not `Degraded`: the Pod runs without them. [What `VolumesDropped` reports](#what-volumesdropped-reports) says which passes write it and which leave it as it stands. Remove the entries to clear it.",
	},
	{
		Type:    replicasLimitedConditionType,
		Written: "Only while `spec.deployment.availability.replicas` is above 1, after a pass that rendered the gateway",
		Meaning: "`status: True`, `Reason: HighAvailabilityNotSupported`. The operator runs the gateway as a single replica, whatever the field asks for; the message names the requested count and why a second replica does not start. Not `Degraded`: the single replica runs. Written on whichever status the pass ends on, on the same terms as `VolumesDropped`: a refusal that returns before the render neither writes nor clears it. Set the field to 1, or remove it, to clear it.",
	},
	{
		Type:    credentialProxyExecReadyConditionType,
		Written: "Only while `credentialProxy.exec.replicas` is above 0 and the agent's name is accepted for the tier",
		Meaning: "`status: True`, `Reason: AllReplicasReady` when every exec-tier replica is Ready. `status: False` otherwise: `Reason: Unschedulable` when the scheduler cannot place a replica, with the pod's name and the scheduler's message (often `Insufficient cpu`); `Reason: ReplicasUnavailable` for any other shortfall. The message counts the Ready replicas against the field. Re-read every 30 s while short; a failed read leaves it as it stands. Not `Degraded`: a caller that cannot reach the tier falls back to the stateful broker. Each replica needs a node with 2 free CPU. Removed when the field is 0 or unset, or when the name is refused (`InvalidCredentialProxyExec`). A park that returns before the broker is reconciled (`ReservedServiceAccountName`, `ForbiddenVolumeMount`, `ShellSandboxCannotBeDisabled`, `RuntimeClassNotFound`) leaves it as it stands, count included, until the park clears.",
	},
	{
		Type:    sizingCappedByQuotaConditionType,
		Written: "Only while `spec.deployment.sizingProfile` is `auto` and the cluster count earns a higher tier than the namespace's ResourceQuota has room for, or than a LimitRange bound admits, or after a quota refused the pod a step recreated",
		Meaning: "`status: True`, `Reason: ResourceQuotaShort` or `LimitRangeShort`. The message names the tier earned, the tier the step was measured from, the tier that stays, and the short quota and key or the LimitRange, bound, resource and container; the figures go in the Warning event. Not `Degraded`: the running tier keeps running. Written before the render, so an earlier refusal neither writes nor clears it. Cleared by the pass that finds room or whose count no longer earns the step; a pass with no reading leaves it, so a gateway outage neither clears nor re-records it. The event watcher switched off by spec clears it. Raise the quota or the bound, or name the tier in `sizingProfile`.",
	},
	{
		Type:    dataVolumeExpansionConditionType,
		Written: "Only while a data claim is smaller than `spec.storage.size` (or the 50Gi default) and its StorageClass cannot expand it",
		Meaning: "`status: True`, `Reason: StorageClassCannotExpand`. The message names each blocked claim, its current request, the size asked for, and why the class cannot expand it. Not `Degraded`: the agent runs on the smaller volume. Written on whichever status the pass ends on, `Ready` or `Degraded`, and removed once the claims can grow or the size no longer asks for more. After an operator restart it is left as it stands until the next pass reads the claims. Set `allowVolumeExpansion: true` on the class to clear it.",
	},
}
