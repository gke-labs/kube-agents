# Cluster Autoscaler: Capacity Buffers (Pre-warm)

## `CapacityBuffer` (CRD)

- **Provisioning Strategy:** `buffer.x-k8s.io/active-capacity` (Placeholder pods).
- **Namespace-scoped:** Targets a specific `ComputeClass` via `nodeSelector` in the `podTemplateRef`.

## Sizing Modes
- **Fixed:** `replicas: 3`. Always keep N units warm.
- **Dynamic:** `percentage: 20` + `scalableRef: <Deployment>`. Headroom scales with workload.

## Why use Buffers?
- **Bursty Serving:** Pod-pending SLOs can't tolerate 60-120s node pool auto-creation delay.
- **HPA outpaces cluster autoscaler:** Workload scales faster than nodes can arrive.
- **Pre-warming:** Warm GPUs/TPUs before known traffic windows.

*Note:* Replaces the "dumb" floor of `--min-nodes` with shape-aware, class-targeted warm capacity.

## Reactive Triage & Readiness Inspection

- **CRD Resource:** `capacitybuffers.autoscaling.x-k8s.io` (`autoscaling.x-k8s.io/v1beta1`, namespaced, shortName `cb`; requires GKE 1.35.2+; not `autoscaling.gke.io`).
- **Why `kubectl get pods` misses broken buffers:** Under `buffer.x-k8s.io/active-capacity`, `CapacityBuffer` placeholder pods exist only inside Cluster Autoscaler memory (`kubectl get pods` shows `0` Pending pods even when a buffer cannot obtain nodes), holding warm spare capacity that real workloads schedule onto directly. `TriggeredScaleUp` and `NotTriggerScaleUp` events are emitted on the `CapacityBuffer` object itself rather than on a Pod.
- **Read-only inspection commands:**

```bash
kubectl get capacitybuffers.autoscaling.x-k8s.io -A
kubectl describe capacitybuffer.autoscaling.x-k8s.io <name> -n <namespace>
```

On clusters running GKE earlier than 1.35.2, the API server reports `the server doesn't have a resource type`; treat the CRD as absent rather than as a failed buffer.

### Condition Triage Table

Upstream `autoscaling.x-k8s.io/v1beta1` emits `ReadyForProvisioning`, `LimitedByQuotas`, and `Provisioning` only — healthy steady-state buffers remain `Provisioning=True` (`reason: FakePodsInjected`) with no `Ready` condition.

| Condition | Meaning | Next Step |
| --- | --- | --- |
| `ReadyForProvisioning=False` (`reason: error`) | Controller cannot translate or validate the buffer (invalid `podTemplateRef`, missing required fields, or incompatible pod-based billing model). Quote `condition.message` verbatim. | Fix the billing model or `PodTemplate` configuration (manual action or GitOps pull request). |
| `LimitedByQuotas=True` | Namespace `ResourceQuota` capped or zeroed the buffer's `status.replicas` (`status.replicas == 0` means zero warm headroom is provisioned; `status.replicas > 0` means partial cap). Quote `condition.message` verbatim. | Raise or adjust namespace `ResourceQuota` limits, or reduce buffer `spec.replicas` / `spec.limits` to fit quota. |
| `Provisioning=False` | Controller failed to construct or inject placeholder pods (for example `FailedToGetPodTemplate` or `FailedToMakeFakePods`), excluding benign `reason: BufferIsEmpty` when `spec.replicas == 0` or a dynamic `scalableRef` buffer resolves to `status.replicas == 0` without `LimitedByQuotas=True`. Quote `condition.message` verbatim. | Repair the referenced `PodTemplate` or buffer spec in GitOps so Cluster Autoscaler can synthesize placeholder pods. |
| `Provisioning=True` (`reason: FakePodsInjected`) | Normal steady state in `v1beta1` (no `Ready` condition is emitted). Scale-up starvation or regional quota exhaustion on the backing `ComputeClass` surfaces as `NotTriggerScaleUp` events on the `CapacityBuffer` object (or `Ready=False` / `status.readyReplicas < status.replicas` past 15 minutes if a controller emits them). | Run `kubectl describe capacitybuffer.autoscaling.x-k8s.io <name> -n <namespace>` to inspect `NotTriggerScaleUp` / `TriggeredScaleUp` events and regional quota; if starved, expand fallback priorities on the target `ComputeClass` via a GitOps pull request without changing buffer replicas or provisioning strategy. |
