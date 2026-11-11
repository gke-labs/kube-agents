# Scheduling on Profiles and Dispatch

- **Author:** [@bnaylor]
- **Date:** 2026-11-03
- **Status:** decided (#4714); epic tracked in #4685 (part of #1721 background automation and #3654 profiles and dispatch).
- **Companions:** [`spec-profiles-dispatch.md`](spec-profiles-dispatch.md) - profile dispatch, Job mode, and the `Unattended` tier; [`spec-subagent-profiles.md`](spec-subagent-profiles.md) - the `AgentProfile` CRD and operator-rendered identities; [`spec-chatops-gateway.md`](spec-chatops-gateway.md) - gateway doors, capability minting, and `chat.notify`; [`gateway-replicas.md`](gateway-replicas.md) - active-standby gateway Lease election; [`cron-report-relay.md`](cron-report-relay.md) - daily report threads and incident context; [`fleet-audit-report-store.md`](fleet-audit-report-store.md) - structured audit envelopes and run-over-run baselines; [`oobe.md`](oobe.md) - first-boot audit chaining.

## Purpose

This spec moves kube-agents' recurring work out of the Hermes in-pod cron scheduler and onto `AgentProfile` dispatch (#3654). Today every recurring job ticks inside the single Hermes gateway pod (`deploy/docker/Dockerfile` cron patches and `agents/chat/scripts/profile_cron_tick.py`), sharing the gateway pod's ServiceAccount, memory ceiling, and `/opt/data` volume. A gateway rollout kills any in-flight fleet audit after a 30-second drain, and a pod outage silently drops missed runs.

Under this design, a schedule is a Kubernetes resource reconciled by the operator. When a run is due, the scheduler submits an `Unattended` task through the A2A gateway, and the profile dispatcher runs it in an isolated Kubernetes `Job` pod with the target profile's `unattended` ServiceAccount and narrowed broker credentials. Deterministic `no_agent` helper scripts that do not run a model move to Kubernetes `CronJob`s, controller loops, or retire with kanban and the per-cluster Hermes profiles.

## 1. Where schedules live and who owns their state

### The `AgentSchedule` CRD (new)

Schedules live in a namespaced `AgentSchedule` custom resource (`kubeagents.x-k8s.io/v1alpha1`) in the `PlatformAgent`'s namespace (`kubeagents-system` by default), alongside `AgentProfile`. Keeping schedules in their own resource rather than embedding a list on `AgentProfile` separates schedule cadence and per-schedule run state from the profile definition: ten built-in fleet audits and any number of user-created schedules can target the same `platform` (or `fleet-audit`) profile without `helm upgrade` clobbering user schedules or sixty-second status writes contending on `AgentProfile`.

```yaml
apiVersion: kubeagents.x-k8s.io/v1alpha1
kind: AgentSchedule
metadata:
  name: compliance-audit
  namespace: kubeagents-system
  labels:
    kubeagents.x-k8s.io/agent: platform-agent
    kubeagents.x-k8s.io/origin: builtin # builtin | user
  annotations:
    # Set by the broker on origin: user schedules from the caller's attested AuthorityRequester:
    # kubeagents.x-k8s.io/created-by-principal: "<principal-hash>"
    # kubeagents.x-k8s.io/created-by-subject: "<verified-subject>"
spec:
  schedule: "20 6 * * *" # 5-field UTC cron expression
  suspend: false
  profileRef:
    name: platform
  prompt: |
    Run the daily GKE Fleet Compliance & RBAC Governance audit...
  parameters:
    stream: compliance-audit
  skills:
    - fleet-audit
    - gke-security-posture
  delivery:
    mode: chat # chat | local
    silentWhenClean: true
    jobKey: compliance-audit
  concurrencyPolicy: Forbid # sole supported value in v1alpha1
  startingDeadlineSeconds: 900
  activeDeadlineSeconds: 3600
  maxTurns: 250
status:
  observedGeneration: 1
  lastScheduleTime: "2026-11-03T06:20:00Z"
  nextScheduleTime: "2026-11-04T06:20:00Z"
  pendingSlot: null
  activeTaskID: ""
  activeTaskStartedAt: null
  lastTaskID: "task-01jc..."
  lastCompletionTime: "2026-11-03T06:41:12Z"
  lastOutcome: Succeeded # Succeeded | Failed | Cancelled | Lost | Skipped
  lastHandledRunNow: ""
  skipCounts:
    alreadyRunning: 0
    missedWindow: 0
    suspended: 0
    gatewayUnavailable: 0
    rateLimited: 0
  conditions:
    - type: Accepted
      status: "True"
      reason: ScheduleValid
```

`concurrencyPolicy: Forbid` is the only concurrency policy in `v1alpha1`, defaulted and enforced by CRD schema validation. Every Hermes cron job today runs under a per-job lock, and keeping `Forbid` as the single policy lets `AgentScheduleStatus` track one in-flight run (`activeTaskID` and `activeTaskStartedAt`) without concurrent runs overwriting each other's status or misleading `oobe`'s completion watch ([§7](#7-run-it-now-for-an-operator-or-the-agent)).

The operator renders the shipped schedules (`origin: builtin`) from the `PlatformAgent` CR when `spec.mode: next` is active. Today `charts/kube-agents/templates/profiles.yaml` renders only the `tse` and `cluster` `AgentProfile` resources (with `allowedTiers: [read]`); Track 2 adds the `platform` (or narrower `fleet-audit`) `AgentProfile` resource with `allowedTiers: [read, unattended]`, and Track 5 adds `unattended` to the `cluster` profile's `allowedTiers` (`[read, unattended]`) for `stall-watch`.

Operators pause or re-time a built-in schedule through `PlatformAgent.spec.schedules.overrides` (keyed by schedule name, allowing `suspend` and `schedule` overrides) or by editing an `origin: user` `AgentSchedule` directly. Because a CEL `ValidatingAdmissionPolicy` cannot dynamically fetch the `AgentProfile` named by `spec.profileRef.name` or run a full cron parser, validation splits across three layers:

1. **CRD / CEL admission:** Enforces structural invariants (5-field whitespace-separated cron token shape, `concurrencyPolicy == "Forbid"`, and the broker ServiceAccount's `origin: user` restriction in [§6](#6-user-created-schedules)).
2. **Reconciler validation (`Accepted` condition):** `AgentScheduleReconciler` parses `spec.schedule` with the Go cron parser and checks the referenced `AgentProfile`'s `spec.allowedTiers` on every reconcile, setting condition `Accepted=False` (`InvalidSchedule` or `ProfileTierDisallowed`) when either check fails.
3. **Gateway submission gate (authoritative):** Because an `AgentProfile`'s `spec.allowedTiers` can change after an `AgentSchedule` is created, the gateway re-verifies that the target profile exists and lists `unattended` in `spec.allowedTiers` whenever it mints a task grant ([§3](#3-how-the-gateway-authenticates-a-scheduler-and-other-triggers)).

### State ownership across ephemeral Job pods

Today `/opt/data` holds four kinds of cron state. Each moves to a home that does not depend on a shared ReadWriteOnce pod volume:

1. **Schedule definitions (`cron/jobs.json`):** Owned by `AgentSchedule` resources in the Kubernetes API.
2. **Run ledger and skip ledger (`cron/executions.db`, `cron_skip_ledger.py`):** Owned by `AgentScheduleStatus` (`lastScheduleTime`, `nextScheduleTime`, `activeTaskID`, `activeTaskStartedAt`, `lastTaskID`, `lastOutcome`, `skipCounts`, and a bounded 10-entry `recentRuns` ring) plus Prometheus counters (`kubeagents_schedule_runs_total{schedule,outcome}` and `kubeagents_schedule_skips_total{schedule,reason}`). This carries forward `already_running` and `missed_window` from `cron_skip_ledger.py` (while retiring the SQLite/flock-specific codes `already_running_elsewhere`, `interpreter_shutdown`, `dispatch_claim_rejected`, `create_execution_failed`, and `fire_claim_lost`), and adds `suspended`, `gateway_unavailable`, and `rate_limited` for the per-trigger cap in [`spec-profiles-dispatch.md`](spec-profiles-dispatch.md#scale-rules).
3. **Incident context for threaded replies (`incidents` table in Session KV):** Owned by the gateway's durable incident store in JetStream KV (see [§4](#4-where-an-unattended-result-goes)).
4. **Fleet-audit report store (`latest.json` and `runs/<timestamp>.json` in [`fleet-audit-report-store.md`](fleet-audit-report-store.md)):** `TierUnattended` tasks run in ephemeral one-shot `Job` pods ([`spec-profiles-dispatch.md`](spec-profiles-dispatch.md#execution)) that do not mount `/opt/data`. While forge-connected runs can seed a missing store once from the open GitHub ledger issue's `<!-- audit-findings: … -->` block, `no_forge/local` and `STORE_LEDGER` installs have no GitHub issue, and `report_status.py` / `fleet-audit-reports` need the 14-run ring across runs. The credential broker exposes `/v1/audit-reports/{audit_id}/{owner}/{repo}` (new; `GET /latest`, `GET /runs`, `PUT /runs/{ts}`), backed by the install's durable store (a JetStream Object Store / KV bucket or the session-kv persistent volume). Because the run-over-run baseline determines which findings count as new, built-in audit stream IDs are reserved for `origin: builtin` schedules and the broker authorizes `PUT /v1/audit-reports/{audit_id}/{owner}/{repo}/runs/{ts}` against the calling task's capability grant: a worker may write only when its task grant's bound `stream` parameter equals `{audit_id}`, `{owner}/{repo}` matches the install's configured GitOps repository (or the `--repo` target bound in the task's grant, or `no_forge/local` when no repository is configured), and—for any built-in `{audit_id}`—the grant carries `origin: "builtin"` and `triggerName == "schedule/" + {audit_id}` (an `origin: user` schedule may write only to `user/<schedule-name>` when `triggerName == "schedule/" + <schedule-name>`). `GET` endpoints are readable across all `{audit_id}` streams for any task grant carrying `audit-reports:read`, so `report_status.py`, `fleet-audit-reports`, and `upgrade-retrospective` can read baselines across streams while no user schedule or worker can overwrite a built-in audit's baseline.

## 2. The scheduler component

The scheduler runs as `AgentScheduleReconciler` (new) inside `k8s-operator`, gated by the operator's existing Lease leader election (`main.go`). Running the reconciler inside `k8s-operator` avoids a separate Deployment and pod budget on Autopilot, reuses the operator's controller-runtime informer cache and per-agent TLS NATS connection (`natsCardPublisher` in `k8s-operator/internal/controller/agentprofile_card.go`), and ensures only the elected leader fires schedules. Today the operator's `authcallout` identity (`operator-controller`) is narrowed to publishing agent cards on `a2a.agents.*`; Track 2 extends that grant in `a2a/authcallout` so `operator-controller` may also publish to `a2a.trigger.schedule.submit` (new) and subscribe to `_INBOX.operator-controller.>`.

### Two-phase slot claim and restart safety

For each `AgentSchedule`, `Reconcile` computes `nextScheduleTime` in UTC from `max(status.lastScheduleTime, metadata.creationTimestamp)` and returns `ctrl.Result{RequeueAfter: nextScheduleTime.Sub(now)}`. While `spec.suspend` is `true`, whenever `now >= status.nextScheduleTime` the reconciler increments `status.skipCounts.suspended`, advances `status.nextScheduleTime` to the next cron boundary after `now`, and requeues without submitting a task, so unsuspending a schedule later waits for the next future slot rather than firing a stale one or miscounting the pause as `missedWindow`. When `spec.suspend` is `false` and `now >= status.nextScheduleTime`, the reconciler executes a two-phase hand-off so neither an operator restart nor a gateway failover ([`gateway-replicas.md`](gateway-replicas.md)) can double-fire or silently drop a due run:

1. **Check in-flight run and liveness ceiling (`concurrencyPolicy: Forbid`):** If `status.activeTaskID` is non-empty, the reconciler queries the dispatcher/bus for the task's state. While the task holds a live lease on the dispatcher and `now.Sub(status.activeTaskStartedAt)` is within `spec.activeDeadlineSeconds` (defaulting to the target `AgentProfile.spec.lifecycle.activeDeadlineSeconds`) plus a 5-minute grace buffer, the reconciler increments `status.skipCounts.alreadyRunning`, advances `status.nextScheduleTime` to the next cron boundary after `now`, and requeues. If the task record has disappeared (for example across a gateway restart before Lease HA lands, KV expiry, or a missed terminal event) or `now.Sub(status.activeTaskStartedAt)` exceeds the deadline plus grace buffer, the reconciler records `status.lastOutcome = Lost`, increments `kubeagents_schedule_runs_total{outcome="lost"}`, clears `status.activeTaskID` and `status.activeTaskStartedAt`, and proceeds with the due slot so a vanished task never wedges the schedule in `alreadyRunning`.
2. **Collapse stale windows after an outage:** If `now.Sub(status.nextScheduleTime) > spec.startingDeadlineSeconds` (default 900 seconds), the reconciler increments `status.skipCounts.missedWindow`, advances `status.nextScheduleTime` to the most recent slot within the deadline (or the next future slot if none falls within the deadline), and persists status before proceeding.
3. **Phase 1 — Persist `pendingSlot`:** Before publishing to the gateway, the reconciler writes `status.pendingSlot = &PendingSlot{SlotTime: slot, IdempotencyKey: "schedule/<namespace>/<name>/<slot-unix>"}` to the Kubernetes API.
4. **Phase 2 — Submit over NATS with idempotency key:** The reconciler publishes the unattended submission request on NATS (see [§3](#3-how-the-gateway-authenticates-a-scheduler-and-other-triggers)) carrying `IdempotencyKey`. Publishing over NATS reaches the single gateway pod today and, once active-standby gateway Lease election ([`gateway-replicas.md`](gateway-replicas.md#what-only-the-active-replica-runs), #4206) lands, automatically reaches whichever gateway replica holds the Lease without `kubectl port-forward` or pod IP discovery. The gateway records `IdempotencyKey -> taskID` in JetStream KV (with a 24-hour TTL) when it mints the task's capability grant. If the operator crashes after the gateway mints the task but before the status update in step 5 completes, the new operator leader sees `status.pendingSlot` on startup, re-sends the same `IdempotencyKey`, and receives the existing `taskID` back rather than starting a second run.
5. **Phase 3 — Advance schedule:** On receiving `taskID` from the gateway, the reconciler sets `status.activeTaskID = taskID`, `status.activeTaskStartedAt = now`, `status.lastTaskID = taskID`, `status.lastScheduleTime = slot`, advances `status.nextScheduleTime` to the next slot after `now`, clears `status.pendingSlot`, and requeues. If the gateway is unreachable or in the middle of a rollout or Lease handover, the reconciler leaves `status.pendingSlot` set and retries with backoff until `startingDeadlineSeconds` expires; when `startingDeadlineSeconds` expires, it clears `status.pendingSlot`, increments `status.skipCounts.gatewayUnavailable`, and advances `status.nextScheduleTime` to the next cron boundary after `now` so the lost slot is counted once under `gatewayUnavailable` rather than counted a second time under `missedWindow` on the next reconcile.

When the dispatcher publishes the task's terminal event on `a2a.tasks.<profile>.<task>.events`, `AgentScheduleReconciler` records `status.lastCompletionTime`, sets `status.lastOutcome`, and clears `status.activeTaskID` and `status.activeTaskStartedAt`.

## 3. How the gateway authenticates a scheduler and other triggers

[`spec-chatops-gateway.md`](spec-chatops-gateway.md) §"The A2A door" noted that the verifier's `unattended` class was still to come. Three in-cluster system components submit unattended work with no human in the loop: `AgentScheduleReconciler` in `k8s-operator`, `k8s-event-watcher`, and `drift-detector` (plus the `stall-watch` and `github-repo-watcher` collectors in [`scheduling-on-profiles-inventory.md`](scheduling-on-profiles-inventory.md)). Today `k8s-event-watcher` (`k8s-operator/cmd/k8s-event-watcher/injector.go`) and `drift-detector` (`k8s-operator/cmd/drift-detector/inject.go`) do not call the A2A gateway at all: both `POST` to the Session KV daemon (`session_kv_server.py`, `/sessions` and `/sessions/{id}/inject`) using a shared bearer token (`SESSION_KV_BEARER_TOKEN`), which gives them no per-caller identity and bypasses the gateway's tier and capability-grant model (while the gateway's own `/inject` handler in `a2a/gateway/inject.go` is a bearer-authenticated `DEV AND EVAL ONLY` door rendered only when `A2A_INJECT_BACKEND=true`).

### Attested NATS trigger ingress (`a2a.trigger.<source>.submit`, new)

Every unattended trigger runs in the cluster under an operator-rendered Kubernetes ServiceAccount and connects to NATS through `a2a/authcallout`. We authenticate triggers on the bus using the same KSA `TokenReview` path (`a2a/authcallout/tokenreview.go`) that authenticates profile workers and the dispatcher:

1. **Callout identity and per-identity inbox narrowing:** The operator registers each trigger's ServiceAccount in the `authcallout` identity map (`agentprofile_identities.go`) with a fixed trigger source slug (`schedule` on `operator-controller`, plus `event-watcher`, `drift-detector`, `stall-watch`, and `repo-watcher`). `a2a/authcallout` narrows each trigger identity `<trigger-identity>` to publish only to `a2a.trigger.<source>.submit` and subscribe only to its own reply prefix `_INBOX.<trigger-identity>.>`, and the client connects with `nats.CustomInboxPrefix("_INBOX.<trigger-identity>")` (matching the per-identity inbox pattern in `a2a/authcallout/contract_test.go` and `dispatcher_contract_test.go`). No trigger receives a wildcard `_INBOX.>` subscribe grant, and a contract-test row in `a2a/authcallout` verifies that a trigger subscribing to `_INBOX.>` or another identity's `_INBOX.<other>.>` is refused. No profile worker, chat bridge, or console client can publish to `a2a.trigger.>`.
2. **Request payload:**
   ```json
   {
     "idempotencyKey": "schedule/kubeagents-system/compliance-audit/1793686800",
     "triggerName": "schedule/compliance-audit",
     "origin": "builtin",
     "profile": "platform",
     "prompt": "Run the daily GKE Fleet Compliance & RBAC Governance audit...",
     "parameters": {
       "stream": "compliance-audit",
       "schedule_mode": "scheduled"
     },
     "skills": ["fleet-audit", "gke-security-posture"],
     "maxTurns": 250,
     "createdByPrincipal": "",
     "createdBySubject": "",
     "delivery": {
       "mode": "chat",
       "silentWhenClean": true,
       "jobKey": "compliance-audit"
     }
   }
   ```
3. **Gateway verification and grant minting:** The active gateway replica subscribes to `a2a.trigger.*.submit`. On receiving a submission:
   - It takes `<source>` from the subject token (which NATS `authcallout` bound to the caller's KSA) and verifies that `triggerName` belongs to that `<source>`.
   - It checks the per-trigger rate cap ([`spec-profiles-dispatch.md`](spec-profiles-dispatch.md#scale-rules)). Because `spec-profiles-dispatch.md` states only that each trigger has its own rate cap without fixing numbers, Track 2 adds a token-bucket limiter in the gateway (default 6 submissions per hour per named schedule trigger and 12 per hour for `event-watcher`, `drift-detector`, `stall-watch`, and `repo-watcher`, configurable on `PlatformAgent`) and returns a `rate_limited` refusal when a trigger exceeds its window budget.
   - It verifies that the target `AgentProfile` exists and lists `unattended` in `spec.allowedTiers`.
   - For schedule triggers, it verifies `parameters.stream` against `origin`: built-in stream names are allowed only when `origin == "builtin"` and `triggerName == "schedule/" + stream`, and any `origin == "user"` submission carrying a built-in stream name (or any non-empty `stream` other than `user/<schedule-name>`) is refused.
   - It stamps `AuthorityRequester{Principal: ps.Hash("trigger:" + triggerName), Backend: "trigger", Subject: triggerName, VerifiedBy: "unattended-ksa"}` (and, on `origin: user` schedules carrying `createdByPrincipal`, records the creator attribution in the requester/audit metadata; see [§6](#6-user-created-schedules)).
   - It mints a root capability grant in `KV_cap` with `Tier: capability.TierUnattended`, `Profile: resolvedProfile`, and the install's resolved scope (plus `triggerName`, `origin`, the bound `stream` parameter, and, for `origin: user` schedules, the read-only `origin: user` constraint), records the delivery metadata on the task's session record, and publishes the submission to `a2a.submit.<profile>.unattended.top.<task>` on `PROFILE_SUBMITS`.
4. **Migrating `k8s-event-watcher` and `drift-detector`:** Under `spec.mode: next`, both components switch from `POST /sessions` and `/sessions/{id}/inject` on the Session KV daemon (and its shared `SESSION_KV_BEARER_TOKEN`) to publishing on `a2a.trigger.event-watcher.submit` and `a2a.trigger.drift-detector.submit` under their own Kubernetes ServiceAccounts.

## 4. Where an Unattended result goes

An `Unattended` top-level task has no asking chat conversation. When its worker publishes a terminal event on `a2a.tasks.<profile>.<task>.events`, the gateway's relay (`a2a/gateway/relay.go` and `notify.go`) routes the output according to the task's `delivery` spec:

1. **Silent-when-clean suppression:** When `delivery.mode` is `local`, or `delivery.silentWhenClean` is `true` and the terminal report is `[SILENT]` (or the structured `audit_report.py finish` artifact carries `silent: true`), the gateway records the completed run in the scheduled-reports store and posts nothing to chat.
2. **Structured fleet-audit report artifact (`kube-agents.report/v1`, new):** Under Hermes cron, a model finishing an audit turn after running `audit_report.py finish` returned its own conversational wrap-up as the job output unless the prompt repeated `finish`'s line (#3217). On profile workers, `audit_report.py finish` emits a `kube-agents.report/v1` task artifact containing both the `kube-agents.chat/v1` card payload and the plain-text fallback. When a task carries a `kube-agents.report/v1` artifact, the gateway delivers that artifact verbatim to chat rather than the model's trailing assistant message.
3. **One thread per schedule per UTC day and reply linkage (#4059):** For `delivery.mode: chat`, the gateway groups posts by `(delivery.jobKey, UTCDate)` using a JetStream KV bucket (`SCHEDULE_THREADS`, new, 48-hour TTL) so the thread key survives gateway Lease failovers ([`gateway-replicas.md`](gateway-replicas.md)). The first non-silent run of `jobKey` on a UTC day starts a new message in the configured home channel and stores the platform's returned `threadID`; later runs of the same `jobKey` on that UTC day post into `threadID`. On every post, the gateway writes `(platform, chatID, threadID) -> reportText` into the incident store so a user reply in that thread has `<untrusted_report>` prepended by the gateway/bridge (shipped in `a2a/hermes-bridge/incident.go`, #4059).
4. **Installs with no home channel (#4053, #3928):** Today `a2a/gateway/notify.go` rejects a home-channel post with `notifyNoHome` when no chat platform has a home channel configured. Under this design, the gateway writes every non-silent unattended report into the durable `SCHEDULED_REPORTS` (new) JetStream stream/KV store before attempting chat delivery. The web console's `#scheduled` channel (#3928) and the on-demand report retrieval tool read directly from `SCHEDULED_REPORTS`. If no chat home channel is configured, the gateway marks the report `stored_no_home` instead of failing the task, and `oobe` / onboarding can still enumerate unposted reports when a home channel is later set.

## 5. What happens to the `no_agent` scripts

Thirty entries ship across the two Hermes rosters on `main`: five in `agents/chat/defaults/cron/jobs.json` and twenty-five in `agents/platform/cron/jobs.json` (Cluster Agents have no `cron/jobs.json`). Eleven are model runs and nineteen are deterministic `no_agent` scripts.

Model runs become `AgentSchedule` resources. Deterministic scripts that still have work to do under `spec.mode: next` become operator-rendered Kubernetes `CronJob`s (or controller loops) running under a narrow ServiceAccount with a broker token scoped to the single capability they need (`chat.notify`, `forge`, or `fleet-read`) and mounting the `<agent>-resolved-scope` ConfigMap ([§8](#8-cluster-lists)).

### Complete inventory and destinations

The per-job inventory lives in [`scheduling-on-profiles-inventory.md`](scheduling-on-profiles-inventory.md): every roster entry, what it does today, and its destination (profile, trigger kind, report sink), plus the open questions where this design doesn't yet give a job a home. Any PR that adds, changes, or removes a roster entry updates its row there.

## 6. User-created schedules

Today the Platform Agent can call Hermes' built-in `cronjob` tool (`create`, `list`, `resnap`, `remove`, `update`, `run`/`run_now`/`trigger`, `pause`, `resume`) to create schedules in `/opt/data/profiles/platform/cron/jobs.json`, while the Chat Agent denies the tool (`agents/chat/config.yaml`).

Under `spec.mode: next`, a brokered MCP tool named `schedule` (new, with `cronjob` kept as an alias) replaces the Hermes tool:

- **Operations:** `list`, `get`, `create`, `update`, `pause`, `resume`, `delete`, `run`, and `history`.
- **Creator attribution and accountability:** On `create` and `update`, the broker stamps immutable annotations `kubeagents.x-k8s.io/created-by-principal` and `kubeagents.x-k8s.io/created-by-subject` on the `AgentSchedule` from the calling task's attested `AuthorityRequester` (never from tool arguments). When `AgentScheduleReconciler` submits an `origin: user` schedule, it forwards both values in the trigger request, and the gateway records `OnBehalfOf: createdByPrincipal` alongside `Principal: ps.Hash("trigger:schedule/" + name)` on the task's `AuthorityRequester` and audit log so every run of a user-created schedule remains attributable to the human who created or edited it.
- **Tier rules:**
  - `TierUnattended` tasks may call only `list`, `get`, and `history`. All mutating or triggering actions (`create`, `update`, `delete`, `pause`, `resume`, `run`) are refused to `TierUnattended` callers so an unattended run can never schedule or trigger further unattended work.
  - Interactive human tasks at `TierRead` (the default human tier in [`spec-profiles-dispatch.md`](spec-profiles-dispatch.md#tiers-and-identity)) may call `list`, `get`, `history`, and `run` on any schedule, and may call `create`, `update`, `pause`, `resume`, and `delete` on `origin: user` schedules. On `origin: builtin` schedules, `TierRead` callers may toggle `pause`/`resume` when `PlatformAgent` policy allows in-chat schedule overrides, while `create`, `update`, and `delete` on `origin: builtin` are always refused.
- **Broker and admission guardrails:**
  - Agent workers never receive Kubernetes RBAC to write `AgentSchedule` resources directly; only `k8s-operator` and the broker ServiceAccount can write them.
  - A Kubernetes `ValidatingAdmissionPolicy` bound to the broker's ServiceAccount requires `object.metadata.labels["kubeagents.x-k8s.io/origin"] == "user"` on `CREATE` and `UPDATE` and `oldObject.metadata.labels["kubeagents.x-k8s.io/origin"] == "user"` on `UPDATE` and `DELETE`, and forbids the broker from modifying `origin: builtin` resources even if the broker process is compromised.
  - User schedules are capped per install (`MAX_USER_SCHEDULES = 32`) and must have a minimum interval of at least 5 minutes.
  - On `create` and `update`, the `schedule` tool refuses any `parameters.stream` value that names a built-in audit stream (allowing only omission or `user/<schedule-name>`), so a user schedule can never claim a built-in audit's report stream ([§1](#1-where-schedules-live-and-who-owns-their-state)).
  - In Hermes cron today, `cron_risk_gate.py` stamps user-created jobs with `risk: "high"` (a fail-closed read-only command allowlist with no proposal allowance, whereas the ten shipped fleet audits run at `risk: "low"`). To preserve that boundary, the gateway stamps grants for `origin: user` schedules with an `origin: user` constraint that denies GitOps write/proposal verbs (`vcs.propose`) unless an operator promotes the CR in Kubernetes, while `origin: builtin` schedules keep proposal authority under `TierUnattended`.
- **One-time migration from `/opt/data`:** During upgrade to `spec.mode: next`, a migration job inspects `/opt/data/profiles/*/cron/jobs.json`, skips the built-in job IDs listed in [`scheduling-on-profiles-inventory.md`](scheduling-on-profiles-inventory.md), creates an `origin: user` `AgentSchedule` CR for each remaining entry (mapping `schedule.expr` -> `spec.schedule`, `enabled: false` -> `spec.suspend: true`, `prompt` -> `spec.prompt`, `skills` -> `spec.skills`, `deliver: "local"` -> `spec.delivery.mode: local`, and `"chat"`, `"all"`, or any platform/channel string -> `spec.delivery.mode: chat`, logging a notice when a specific channel target is normalized to the home channel), and writes `/opt/data/.cron_jobs_migrated_to_crd` so the import runs once.

## 7. "Run it now" for an operator or the agent

Two different callers ask to run a scheduled job immediately, and they expect different routing (#1876):

1. **Operator, `oobe`, or background trigger (`run-now` annotation):**
   - Setting annotation `kubeagents.x-k8s.io/run-now: "<token>"` on an `AgentSchedule` (via `kubectl annotate agentschedule compliance-audit kubeagents.x-k8s.io/run-now="$(date -u +%s)" --overwrite`, the `schedule(action="run")` MCP tool, or `oobe`) wakes `AgentScheduleReconciler`.
   - When `metadata.annotations["kubeagents.x-k8s.io/run-now"] != status.lastHandledRunNow`, the reconciler checks whether a run is already in flight (`status.activeTaskID != ""`, since `spec.concurrencyPolicy` is always `Forbid` in `v1alpha1`): if a live run is already in flight, it adopts the running `activeTaskID` (matching [`oobe.md`](oobe.md#43-firing)), sets `status.lastHandledRunNow = "<token>"`, and starts no duplicate task. Otherwise it claims `status.pendingSlot` with idempotency key `run-now/<namespace>/<name>/<token>`, submits the `TierUnattended` task to the gateway, records `status.lastHandledRunNow = "<token>"`, `status.activeTaskID = taskID`, and `status.activeTaskStartedAt = now`, and leaves `status.nextScheduleTime` unchanged.
   - `oobe`'s first-run audit chain ([`oobe.md`](oobe.md#43-firing)) uses this path instead of `cron.jobs.trigger_job`: it sets `run-now` on `fleet-wide-cost-analysis`, `compliance-audit`, `obtainability-audit`, `stockout-prevention`, and `upgrade-retrospective` one at a time, watching `status.activeTaskID` and `status.lastCompletionTime` on each `AgentSchedule` before triggering the next.
2. **Interactive user request in chat (#1876):**
   - When a user asks in chat to "run the compliance audit now", queueing a background cron tick and replying "queued, wait for the next tick" leaves the user with no answer in their thread and lets a clean run stay `[SILENT]`.
   - Instead, the chat/platform agent delegates a child task to the target profile at the user's `Read` tier without `--scheduled` (so `silent_ok` is `false` and the full report returns to the asking conversation through the gateway's normal child/top result relay). The scheduled cadence on `AgentSchedule` is untouched.

## 8. Cluster lists

All scheduled collectors and helper scripts that enumerate the managed fleet must read the scope publisher's resolved set (`<agent>-resolved-scope` ConfigMap, key `resolved-scope.json`, written every 15 minutes by `scope_publisher.py` / `platformagent_scope_publisher.go`, #4277 / #4119) paired with the rendered `scope.json` (`KUBEAGENTS_SCOPE_FILE`), rather than scanning `/opt/data/profiles/cluster-*` or `/opt/data/fleet_scope.json`:

- `agents/platform/scripts/fleet_scope_targets.py` (`declared_scope_targets`) becomes the single Python scope reader for helper scripts and the `fleet_scope` MCP tool. Under `spec.mode: next`, it reads the rendered `scope.json` (management project, explicit `projects`, and `exclude.clusters`) together with `resolved-scope.json` (mounted from the `<agent>-resolved-scope` ConfigMap) before falling back to `/opt/data/fleet_scope.json` on legacy mode, and never reads `/opt/data/profiles/cluster-*`.
- `scope_publisher.py` already probes each member project with `rec._list_project` (`gcloud container clusters list`); extending its row schema (or companion cluster list in `resolved-scope.json` within the 900 KiB ConfigMap budget, plus explicit-project probing) lets `stall_watch.py`, `upgrade_readiness_watch.py`, `sa_role_watch.py`, and `leeway_watch.py` (`roster_clusters_by_project`) read the resolved `(project, location, cluster)` targets from `fleet_scope_targets.py` and retire their `/opt/data/profiles/cluster-*` directory walks.
- The reader exposes a narrow target-lookup interface (`declared_scope_targets()` and `list_scope_clusters()`, new) so a future fleet read cache can replace the ConfigMap file read without changing any audit collector or watch script.

## Changes to companion specs

- **[`spec-profiles-dispatch.md`](spec-profiles-dispatch.md):** Links this spec for scheduled `Unattended` task submission, `AgentSchedule`, and the retirement of Hermes cron alongside kanban.
- **[`spec-chatops-gateway.md`](spec-chatops-gateway.md):** Specifies the `unattended` trigger verifier class on `a2a.trigger.<source>.submit` (attested via `authcallout` KSA identity) and gateway-native delivery of top-level `TierUnattended` results to `chat.notify` and `SCHEDULED_REPORTS`.
- **[`spec-nats-deployment.md`](spec-nats-deployment.md):** Adds the `a2a.trigger.<source>.submit` publish and `_INBOX.<trigger-identity>.>` subscribe permissions for trigger KSAs in `authcallout`, the `SCHEDULE_THREADS` KV bucket (48h TTL), and the `SCHEDULED_REPORTS` stream/KV store.
- **[`fleet-audit-report-store.md`](fleet-audit-report-store.md):** Adds the broker-mediated store endpoints (`/v1/audit-reports/...`) with per-`audit_id` and per-repository write authorization so ephemeral `Job` pods read and write `latest.json` and `runs/<ts>.json` without mounting `/opt/data`.
- **[`oobe.md`](oobe.md):** Updates §4.3 so first-run audits trigger via the `AgentSchedule` `run-now` annotation and observe completion through `AgentScheduleStatus` instead of `cron.jobs.trigger_job` and `executions.db`.
- **[`agent-communication.md`](agent-communication.md):** Corrects §1 and §2.7's stale claims that Cluster Agents run their own cron roster.

## Order of work

Each step is filed and landed as its own sub-issue under #4685:

1. **Design (#4685 Track 1):** Land this design document.
2. **Scheduler, `AgentSchedule` CRD, and unattended trigger ingress (#4685 Track 2):** Add `AgentSchedule` types, `AgentScheduleReconciler` in `k8s-operator`, the `platform` (or `fleet-audit`) `AgentProfile` CR with `allowedTiers: [read, unattended]`, the `a2a.trigger.<source>.submit` and `_INBOX.<trigger-identity>.>` grants in `a2a/authcallout` (extending `operator-controller` in `agentprofile_card.go` and adding contract tests refusing cross-identity inbox subscriptions), the gateway trigger subscriber and per-trigger token-bucket rate limiter, `run-now` annotation handling, unit/envtest/conformance tests, and an eval case that runs a no-op `Unattended` schedule end to end in Job mode. Seamless gateway failover during schedule submission depends on active-standby gateway Lease election (#4206).
3. **Unattended result delivery (#4685 Track 3):** Gateway terminal-event delivery to `chat.notify`, `SCHEDULE_THREADS` daily thread grouping, incident store write for threaded replies (#4059), `kube-agents.report/v1` artifact extraction for `fleet-audit`, and `SCHEDULED_REPORTS` persistence for no-home-channel installs (#4053, #3928).
4. **Fleet audits migration (#4685 Track 4):** Broker-backed `/v1/audit-reports` store with task-grant write scoping; migrate `compliance-audit` first as a canary under `spec.mode: next` and compare against Hermes runs, then migrate the remaining nine fleet audits and `upgrade-readiness-weekly-brief`.
5. **Helper jobs and cluster-list cutover (#4685 Track 5):** Switch `fleet_scope_targets.py`, `stall_watch.py`, `upgrade_readiness_watch.py`, `sa_role_watch.py`, and `leeway_watch.py` to `<agent>-resolved-scope`; add `unattended` to the `cluster` `AgentProfile`'s `allowedTiers` (`[read, unattended]`); render Kubernetes `CronJob`s for the surviving deterministic scripts (`stall-watch`, `github-repo-watcher`, `upgrade-readiness-watch`, `upgrade-readiness-weekly-review`, `sa-role-watch`, `leeway-watch`, `findings-morning-nudge`, `feedback-prompt`, `eod-event-watcher-daily-report`); migrate `k8s-event-watcher` and `drift-detector` from Session KV `/sessions/{id}/inject` to `a2a.trigger.<source>.submit`; replace `chat-delivery-watch` with gateway/scheduler metrics and health conditions.
6. **OOBE migration (#4685 Track 6):** Move `oobe` first-run audit chaining onto `AgentSchedule` `run-now` and `AgentScheduleStatus`.
7. **User schedules (#4685 Track 7):** Add the brokered `schedule` (`cronjob`) MCP tool with creator attribution annotations, the broker `origin: user` `ValidatingAdmissionPolicy`, and the one-time `/opt/data/profiles/*/cron/jobs.json` migration.
8. **Legacy Hermes cron retirement (#4685 Track 8):** Once `spec.mode: next` is the default and no job runs on Hermes cron, delete `agents/chat/defaults/cron/jobs.json`, `agents/platform/cron/jobs.json`, `profile_cron_tick.py`, `/v1/cron-reports`, `kanban-workspace-gc`, `kanban-board-health`, `session-retention`, `cluster-agent-reconcile`, and the Hermes cron patches in `deploy/docker/Dockerfile` and `deploy/docker/patches/*cron*`.
