# Proactive Utilization-Driven Sizing and Active-Standby Broker Rollout

> **STATUS — implemented.** Covers Phase B of `sizingProfile: auto`: bi-directional utilization-triggered tier transitions with anti-thrashing hysteresis, surge-aware quota and LimitRange admission checks, and operator-coordinated active-standby handoff for `credential-proxy`.

**Scope:** `k8s-operator` (`api/v1alpha1`, `internal/controller/sizing_profile.go`, `internal/controller/sizing_utilization.go`, `internal/controller/credential_proxy_control.go`, `internal/controller/credential_proxy_manifests.go`, `internal/controller/platformagent_controller.go`), `agents/platform/scripts/credential_proxy.py`, Helm quota preflight / footprint assets, and [`docs/designs/credential-proxy-single-pod.md`](credential-proxy-single-pod.md).

**Baseline telemetry & RBAC:**

- Operator ClusterRole grants `get`/`list`/`watch` on `pods.metrics.k8s.io` and `nodes.metrics.k8s.io` plus cluster-wide `get`/`list`/`watch` on `coordination.k8s.io/leases` and `secrets`; namespaced Roles grant writes on Leases and Secrets in the release and `-profiles` namespaces.
- `sizing_utilization.go` queries `PodMetrics` (`metrics.k8s.io/v1beta1`) and scrapes `credential-proxy` `/metrics` (`8766`) directly inside `PlatformAgentReconciler` on `sizingUtilizationPollInterval` (`100s`).
- `credential_proxy.py` exports `kubeagents_credential_proxy_slots_in_use`, `kubeagents_credential_proxy_slot_cap`, `kubeagents_credential_proxy_child_memory_reserved_bytes`, `kubeagents_credential_proxy_child_memory_budget_bytes`, `kubeagents_credential_proxy_cgroup_memory_bytes`, and `kubeagents_credential_proxy_resource_watermark_ratio`.

---

## 1. Why Cluster Count Alone Is Not Enough

Under the prior `sizingProfile: auto` implementation, tier resolution was an upward-only latch keyed exclusively on cluster count (`10` for `medium`, `25` for `fleet`):

1. A small-count fleet (say `4` very large clusters with tens of thousands of pods) could saturate `small` (`1Gi` request / `2Gi` limit on `credential-proxy`, `384Mi` / `2Gi` on `agent-api-auth`, `512Mi` / `2Gi` on `shell`) without ever crossing `10` clusters.
2. A bursty workload never released `medium` or `fleet` resources after load subsided, because `resolveSizingTier` never stepped down even when utilization dropped well below the lower tier's capacity.
3. Because `buildCredentialProxyDeploymentForPlugins` previously used `Strategy: Recreate`, every tier change stopped the running broker pod before starting its replacement—and if the new pod failed to schedule on node capacity, service dropped until intervention.

---

## 2. Utilization Signal Model & Bi-Directional Hysteresis

### 2.1 Metrics Sources

`PlatformAgentReconciler` polls live `PodMetrics` (`metrics.k8s.io/v1beta1`) and scrapes `credential-proxy` `/metrics` every `sizingUtilizationPollInterval` (`100s`), excluding `<agent>-credential-proxy-exec-tier-*` pods from stateful-broker signals:

| Component                  | Utilization Signals                                                                                                                                                                                              | Capacity Denominator at Tier `T`                                                                                                                                                                                                                                                             |
| -------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `credential-proxy`         | `PodMetrics` CPU & memory working set, `/metrics` `slots_in_use / slot_cap`, `child_memory_reserved_bytes / child_memory_budget_bytes`, `cgroup_memory_bytes / min(cgroup limit, memory cap)`, `watermark_ratio` | CPU: effective request (`500m` / `1` / `4` Standard; `2` / `2` / `4` Autopilot); Memory: `Requests[memory]` when Autopilot limits clamp (`clamped=true`), else `Limits[memory]` (`2Gi` / `4Gi` / `8Gi`); slots (`<= credentialProxyMaxConcurrentCommands`) & child memory budget at tier `T` |
| `agent-api-auth`           | `PodMetrics` memory working set                                                                                                                                                                                  | Effective memory denominator (`2Gi` / `4Gi` / `8Gi`, `Requests` when clamped)                                                                                                                                                                                                                |
| `shell` (`<name>-shell-0`) | `PodMetrics` memory working set                                                                                                                                                                                  | Effective memory denominator (`2Gi` / `2Gi` / `4Gi`, `Requests` when clamped)                                                                                                                                                                                                                |

A tier step is only proposed along a saturated dimension if the candidate higher tier strictly increases that dimension's capacity (for example, on Autopilot `small -> medium` raises `credential-proxy` memory `2Gi -> 4Gi` without changing CPU `2 -> 2`, so CPU saturation alone targets `fleet` `2 -> 4`, whereas memory saturation targets `medium` first). Similarly, `slots` does not trigger promotion when the running broker's `slot_cap` is already at `credentialProxyMaxConcurrentCommands` (`8`).

### 2.2 Step-Up and Step-Down Thresholds (Anti-Thrashing)

Package-level constants in `sizing_utilization.go` govern bi-directional transitions:

1. Cluster-count floor: `floor = sizingTierForClusterCount(observedClusterCount)` (`small` `< 10`, `medium` `10..24`, `fleet` `>= 25`). When `auto` is active, utilization can promote above `floor` (`T > floor`), and step-down stops at `floor` (`T >= floor`), never stepping below what the registered cluster count earns.
2. Step-up (`T -> T+1` or relieving tier): Triggered when any bottleneck dimension `d` where candidate capacity strictly exceeds `cap(T, d)` holds `usage(d) / cap(T, d) >= 75%` (`sizingStepUpThresholdPct`) continuously for `5m` (`sizingStepUpSoakDuration`, tracked via `status.sizing.scaleUpSustainedSince`). Targets the lowest tier relieving all currently saturated dimensions.
3. Step-down (`T -> T-1`, 1 tier at a time): Requires `T > floor` (respects cluster-count floor and manual explicit tier overrides), at least `30m` elapsed since `status.sizing.lastTransitionTime` (`sizingTransitionCooldown`), and every dimension whose capacity strictly shrinks in `T-1` having an observed reading and holding `usage(d) / cap(T-1, d) <= 55%` (`sizingStepDownThresholdPct`) continuously for `25m` (`sizingStepDownSoakDuration`, tracked via `status.sizing.scaleDownSustainedSince`).
4. Missing-metrics fail-safe: Missing `PodMetrics` or unreachable `/metrics` never triggers step-down (only observed dimensions confirm safe step-down), leaving step-up idle while keeping the cluster-count floor enforced. The `75%` step-up (`5m`) and `55%` step-down-of-`T-1` (`25m` + `30m` cooldown) thresholds leave a deadband across tier sizes (`2Gi -> 4Gi`: step-up at `1.5Gi`, step-down from `medium` at `1.1Gi`).

---

## 3. Active-Standby Rollout for `credential-proxy`

### 3.1 Why Single Active Pod Remained Invariant

As documented in [`docs/designs/credential-proxy-single-pod.md`](credential-proxy-single-pod.md), multiple **simultaneously active** broker pods break stateful single-pod consumers:

- Google Chat relay holds `receipt -> ack_id` in process memory.
- Slack Socket Mode opens a per-process socket and holds `receipt -> event` in process memory.
- `scope_policy.MemberWindow` records prior probe results in process memory.

Therefore, `credential-proxy` preserves **`Replicas: 1` active** at all times, while switching `Deployment.Spec.Strategy` from `Recreate` to `RollingUpdate` (`MaxSurge: 1, MaxUnavailable: 0`) under operator-coordinated active-standby cutover.

### 3.2 Control Plane Protocol (Zero Hub API Token on `credential-proxy`)

Because `credential-proxy` sets `AutomountServiceAccountToken: false` and holds zero hub Kubernetes API permissions, the operator acts as the external coordinator:

1. **Deployment strategy & control credential:** `credential-proxy` uses `RollingUpdate` (`MaxSurge: 1, MaxUnavailable: 0`) and mounts a read-only operator-owned Secret `<agent>-credential-proxy-control` (`CREDENTIAL_PROXY_CONTROL_FILE`) with `CREDENTIAL_PROXY_STANDBY=1` (both reserved in CR env merges and stripped from exec-tier pods, alongside secret volume name isolation in `credential-proxy-control`).
2. **Standby warm-up (`NewPod`):** When `CREDENTIAL_PROXY_STANDBY=1` and `role == broker` (`credential_proxy.py` / `BrokerLifecycle`), `NewPod` initializes Envoy, `CommandExecutor`, and `Policy` (`warm=true`), keeping Socket Mode intake and background proposal readings sweeps stopped until activated. `/healthz` returns `503 {"status": "standby"}` so Kubernetes keeps 100% of Service Endpoints traffic on the existing active pod.
3. **Failure safety before cutover:** If a surge pod stays `Pending` or fails before passing warm checks (`matchesProxyPodTemplate` matching `kubeagents.x-k8s.io/proxy-template-hash` and `kubectl.kubernetes.io/restartedAt`, immune to Pod-level `LimitRanger`/VPA resource mutations), the existing pod stays `Ready` and active; after `sizingSurgePendingTimeout` (`2m`), the operator rolls back one tier (`SizingCappedByQuota` / `SurgeUnschedulable`) without dropping the active pod.
4. **Operator Lease & graceful drain (`OldPod` -> `NewPod`):** Once `NewPod` responds `200 {"warm": true, "state": "standby"}` on `GET :8766/control/status` (using `operatorMetricsIngressRule` on port `8766`), `PlatformAgentReconciler.reconcileCredentialProxyHandoff` (excluding `draining`/`drained` and deleting pods from target candidates):
   - sends `POST :8766/control/drain` to any non-target active pod (`OldPod`), which refuses new exec/pull calls (`503 draining`) while keeping `/healthz` returning `200 ok` during `BROKER_STATE_DRAINING` so Service Endpoints remain live for `DRAIN_OPEN_ROUTES` / `DRAIN_OPEN_REPLY_ROUTES`, stops Slack Socket Mode intake, waits up to `settle_seconds` (`3s`) for unsettled Chat receipts and in-memory Slack/Teams events plus `HANDOFF_DRAIN_SECONDS` (`15s`) for in-flight requests, NACKs remaining Chat receipts to Pub/Sub, and flips `/healthz` to `503 {"status": "draining"}` upon reaching `BROKER_STATE_DRAINED`;
   - updates the operator-managed `coordination.k8s.io/v1` `Lease` (`<agent>-credential-proxy`, read/written via `APIReader`) to record `HolderIdentity = NewPod.Name`;
   - sends `POST :8766/control/activate` to `NewPod`, which opens Slack Socket Mode + proposal readings sweep and flips `/healthz` to `200 {"status": "ok"}`.
5. **Cutover, legacy retirement & termination:** With `PeriodSeconds: 2, FailureThreshold: 2`, kubelet marks `NewPod` `Ready`, `DeploymentController` satisfies `maxUnavailable: 0`, and `SIGTERM` reaches the already-drained `OldPod` (releasing any late-pulled Chat receipts before exiting). Note the short non-zero cutover window while `OldPod` reaches `BROKER_STATE_DRAINED` (`/healthz` `503`) before `NewPod` passes readiness probes: `execute`, `vcs_call`, and `ApiSession._send` retry `503 CREDENTIAL_PROXY_BUSY` (`bound="draining"`) via `exec_refusal_retryable`/`retry_wait`. During a one-time upgrade from a pre-handoff broker image lacking `/control/*` (`404`), the operator requests `Delete` on the legacy pod and holds activation of `NewPod` until the deleting legacy pod exits `PodRunning` / disappears.

### 3.3 Sibling Workloads (`agent-api-auth` & `shell`)

The `shell` workload (`StatefulSet <name>-shell`) keeps ordered single-pod replacement (`RollingUpdate` `StatefulSet`) with `RWO` PVC retention; only `fleet` transitions raise `shell` memory (`2Gi -> 4Gi`), so `medium` transitions do not restart `shell`. Meanwhile, the `agent-api-auth` sidecar (`2Gi`/`4Gi`/`8Gi` memory limits across `small`/`medium`/`fleet`) runs inside the single-replica `Recreate` `<name>-gateway` Deployment (`replicas: 1`), restarting `<name>-gateway` on every tier transition (clamped or unclamped) unless `agentAPIAuth.resources.limits.memory` is overridden.

---

## 4. Surge-Aware Quota, LimitRange & Capacity Gates

Before admitting any tier transition (promotion or step-down) `running -> candidate` under `auto`, `sizing_profile.go` checks both `LimitRange` bounds and `ResourceQuota` headroom, accounting for simultaneous `old + new` `credential-proxy` pods during active-standby surge (`maxSurge: 1`):

For `ResourceQuota`, unlike `Recreate` delta checks (`after - before`), `credential-proxy` requires `quotaFree >= after` (full replacement pod requests and limits plus `1` extra pod slot in `pods` and `count/pods`). Post-preflight `ReplicaFailure` quota refusals naming `pods`/`count/pods` or tier-step dimensions step back one tier (`tier - 1`) on step-up or revert to `activeTier` on step-down. For unschedulable surges, if a surge pod remains `Pending` past `sizingSurgePendingTimeout` (`2m`), `sizingSurgeStalled` (filtering by stripped tier resources with `sizingRunningLimitsClamped`, post-transition creation time, and `!heldBack` so a lingering higher-tier surge pod never triggers a second rollback) steps back one tier (`tier - 1`) on step-up or reverts to `activeTier` on step-down with `SizingCappedByQuota` (`SurgeUnschedulable`), starts `sizingStepBackHoldInterval` (`10m`), and leaves the active broker pod running. All checks preserve `sizingRunningLimitsClamped` so Autopilot keeps CPU/memory limits equal to requests (`medium` CPU stays `2`, memory moves `2Gi -> 4Gi`), and keep Standard node sizing warnings when `fleet` requests `4 vCPU`.

---

## 5. CRD Status & Observability Additions

`PlatformAgentStatus` carries an additive optional field `status.sizing` (`*SizingStatus`, handled cleanly on older CRDs via 5-minute pruned-field reprobe `sizingStatusPruned`), exposing `activeTier` / `desiredTier` / `trigger` (`cluster-count` | `utilization` | `both` | `manual`), `scaleUpSustainedSince` / `scaleDownSustainedSince` / `lastTransitionTime`, `lastObservedPeakPct` / `lastObservedDimension` / `lastObservedWorkload`, `SizingCappedByQuota` Conditions (`ResourceQuotaShort`, `LimitRangeShort`, `SurgeUnschedulable`), and `Normal` (`SizingTierChanged`) and `Warning` (`SizingCappedByQuota`, `SizingCappedByLimitRange`, `SurgeUnschedulable`) Events.
