# Cluster Autoscaler: Debugging & Performance

## Live Visibility Logs

- **Asset:** `assets/log-autoscaler-events.sh <cluster-name>` (Live tail).

## `messageId` Cheat Sheet
| ID | Meaning | Fix |
|----|---------|-----|
| `scale.up.error.out.of.resources` | GCE Stockout | Add zone/family fallback in ComputeClass. |
| `scale.up.error.quota.exceeded` | Project quota cap | Raise regional quota. |
| `scale.up.error.ip.space.exhausted` | Subnet full | Expand pod IP ranges. |
| `scale.up.no.scale.up` | No priority match | Check Pod requests vs ComputeClass bounds. |

## Pending Pod Checklist
1. `kubectl describe pod`: Check events for "insufficient cpu" or "taints".
2. **Hit `--max-nodes`?** Check pool limits.
3. **Selector Conflict?** Pod Pins `gke-spot=true` while ComputeClass is On-Demand.
4. **node pool auto-creation Enabled?** Check `nodePoolAutoCreation.enabled: true`.
5. **Visibility Logs:** Read `noDecisionStatus.noScaleUp` for exact rejection reason. No reason at all (Pods under `unhandledPodGroups`, no `rejectedMigs`, no `skippedMigs`) is a finding too: see **Pool labelled for a ComputeClass** below.
6. **EKS to GKE Selector Translation:** If migrating from EKS/Karpenter, ensure the user translates AWS-style or generic selectors (`machine-family`) to GKE-native ones (`cloud.google.com/machine-family`). A common cause of `scale.up.no.scale.up` is a Pod asking for `machine-family: c3` while GKE only recognizes `cloud.google.com/machine-family: c3`.
7. **Machine Series Support:** If node pool auto-creation fails to provision nodes for a specific `machineFamily` or `instance-type` (e.g., N4, C3A), verify the GKE version supports that series for node pool auto-creation / Autopilot. Old GKE versions will ignore unsupported series. Check GKE release notes or node pool auto-creation docs for version requirements.
8. **Brand-new reservation?** A reservation created in the last ~30 min may not be in Cluster Autoscaler's cache yet. Targeting it before the cache catches up makes Cluster Autoscaler back off that reservation and stall. Wait **≥30 min** after creating the reservation before driving scale-up against it (see `ca-optimization.md`).
9. **Pool labelled for a ComputeClass, Pod names no class?** Compare the Pod's `nodeSelector` / required `nodeAffinity` on `cloud.google.com/compute-class` against the node pools' `cloud.google.com/compute-class` labels (`kubectl get nodes -L cloud.google.com/compute-class`). A namespace default class (the label `cloud.google.com/default-compute-class` or `cloud.google.com/default-compute-class-non-daemonset`) counts only when it shows in the Pod spec as a `nodeSelector` that GKE added: a label set after the Pod was created does not reach it. Check also that no cluster default class is on. If every pool that could grow carries the label with no taint and the Pod names no class, the Pods can stay `Pending` with an **empty** `NotTriggerScaleUp` reason while the pool is below its maximum (`autoscaling.maxNodeCount` is for each zone: compare the pool's node count with `autoscaling.totalMaxNodeCount` when it is set, else with `autoscaling.maxNodeCount` times the number of zones in the pool's `locations`). No scale-up was tried, so this is **not** a stockout. Seen on GKE 1.35.8-gke.1796000, not on 1.36.4-gke.2377000; other versions were not tested. Sign, checks and fix: Symptom 13 in `skills/gke-compute-classes/references/compute-class-debug.md` (the `gke-compute-classes` skill). For workloads with generated names: **Workloads With Generated Names** below.

## Finding Scale-down Blockers

- **Asset:** `./assets/find-scale-down-blockers.sh` (Scan cluster for blockers).

### Common Causes
- **Bare Pods:** No controller (Deployment/Job); autoscaler won't evict.
- **Local Storage:** `emptyDir` on local SSD or `hostPath`.
- **Annotation:** `cluster-autoscaler.kubernetes.io/safe-to-evict: "false"`.
- **PDBs:** Currently allowing zero disruptions.
- **Floor:** `min-nodes` or `total-min-nodes` > 0.

## Performance & Sluggishness
- **Required Anti-affinity:** Explodes scheduler cost at scale. Use `preferred` or `topologySpreadConstraints`.
- **Pool Count:** Beyond ~200 pools, autoscaling slows down. Consolidate near-duplicate ComputeClasses.
- **Spot Grace Period:** Default is 30s. Extend to 120s (GKE 1.35+) via `shutdownGracePeriodSeconds` in `kubeletConfig`.

## Workloads With Generated Names
Symptom: a controller creates Deployments such as `<app>-<uuid>` through the Kubernetes API, and their Pods name no ComputeClass (checklist item **Pool labelled for a ComputeClass**). No manifest in Git describes them, so a pull request per Deployment cannot fix them.
**Fix:** Name the class once, on the namespace. The ComputeClass must exist and name the pool.
1. Propose the label `cloud.google.com/default-compute-class: <name>` on the `Namespace` manifest as a pull request (`cloud.google.com/default-compute-class-non-daemonset` also worked for these Pods).
2. GKE then adds the class `nodeSelector` and toleration to each **new** Pod in the namespace, and the pool grows for them. Pods that are already `Pending` get neither: their owner must recreate them (a rollout restart) after the label is set. Tested on GKE 1.35.8-gke.1796000 (one small zonal cluster, one try per case).
3. Optional, not tested here: for bursts that cannot wait for a new node, propose a `CapacityBuffer` whose Pod template names the class (`ca-capacity-buffers.md`).

## Segregating System Pods (Expert Pattern)
Symptom: `kube-system` pods (metrics-server, coredns) land on expensive nodes and pin them.
**Fix:** Segregate via namespace default ComputeClass.
1. Propose a "cheap" `system-pool` ComputeClass as a pull request.
2. In the same pull request, label the `kube-system` namespace:
   `cloud.google.com/default-compute-class-non-daemonset: system-pool`
