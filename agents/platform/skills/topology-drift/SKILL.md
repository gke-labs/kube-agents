---
name: topology-drift
description: Fleet-wide zonal placement skew from one `leeway report` sweep across several clusters — which multi-replica workloads have their running replicas concentrated in one zone, why (a conflicting nodeAffinity under a soft spread constraint, no spread constraint at all, zonal PersistentVolumes, a zone out of CPU, a zone cordoned or NotReady, a node pool that exists in one zone), the share a zone failure takes, and which remediation category each cause takes; a GitOps pull request only where the fix is a manifest. A question about one managed cluster is that cluster's Cluster Agent's, delegated by kanban card.
---

# Topology Drift (Platform Agent)

Use this skill for the fleet question: which clusters have workloads that would not survive a zone failure, and why. `leeway report` reads every target cluster in one run and does the counting and the first pass at attribution; this skill is about reading what it printed, confirming it, and naming the cause and the remediation category without proposing the wrong fix. Four of the causes below take no manifest change at all, and an agent that answers every skew with a `topologySpreadConstraints` pull request sends the operator to edit a manifest that is correct.

## Delegate or run

Per `AGENTS.md` ("Delegation") single-cluster runtime debugging is the Cluster Agent's. Call `get_cluster_profile_name(project, cluster, location)` for the cluster the request names:

- `exists: true` — delegate: `kanban_create(assignee="<profile-name>", title="Topology skew in <namespace or cluster>", body="<the question, the namespace, and the report format the requester asked for>. Read-only.")`. The Cluster Agent runs its own copy of this skill and completes the card with the per-workload attribution. If the request asked for a fix and the card's `metadata.proposed_patch` is a manifest change (rows 1 and 3 below), submit it via `submit-suggestion`; otherwise the RCA and the recommendation go back in your reply.
- `exists: false`, or the request spans several clusters — run the sweep yourself, below.

## Running the sweep

Call the `platform_control` `fleet_scope` tool first: when it lists `excluded_clusters`, drop each one from the target triples (excluded clusters are never read, listed or reported on) and name each one as excluded and not checked.

```bash
leeway report --topology-tier-c-signals --clusters <project>/<location>/<cluster>,<project>/<location>/<cluster>
```

Name every target as a `project/location/cluster` triple (resolve them with `list_cluster_profiles`, or from the request, less any `excluded_clusters`); add `--namespace <ns>` to scope every cluster to one namespace. Target clusters with `--clusters` only: inside the sandbox each triple becomes a `kubectl --context=gke_<project>_<location>_<cluster>` read through the credential proxy, and an exported `KUBECONFIG` would make the shim answer every name from that one file. `--topology-tier-c-signals` adds the Tier C rows — workloads with no declared or assumed spread intent that are nonetheless skewed — which is where a StatefulSet on zonal disks shows up.

The output is one JSON document (`leeway.report/v1alpha1`) on stdout with one `clusters[]` entry per target, in the order given, and a `summary` with `clusters_total`, `clusters_succeeded`, `clusters_failed`, `topology_findings`, `by_severity` and `by_tier`. A real one, from a four-cluster sweep, is at `references/report-example.json`. If `leeway` is not on `PATH`, the sandbox image predates it: say so and stop rather than substituting a hand survey.

<!-- shared:reading-start -->

## Reading the report

Read in this order. Each step can end the analysis early, and skipping ahead to the workload findings is how a cordoned zone gets misreported as six misconfigured Deployments.

1. **`clusters[].status`.** `"error"` carries an `error` string and nothing else: that cluster was not read, and it is reported as unscanned, never as clean.
2. **`clusters[].domains["topology.kubernetes.io/zone"]`** — the zone census, one entry per zone with `nodes`, `ready`, `usable` (Ready and schedulable) and allocatable CPU and memory. One zone: nothing can spread, say so and stop. A zone with `usable: 0` or `ready: 0`: a Domain finding (next step) explains every workload stacked in the other zones.
3. **Findings whose `subject.kind` is `"Domain"`** (`kind: "leeway.domain_unavailable"`). A zone that is cordoned or down is the cause of the skew around it; the workloads in the remaining zones are its consequence and get no finding of their own.
4. **Workload findings**, each read in this order: `suspectedCause` → `remediation.kind` → `intent.source` and `intent.evidence[]` → `contributingFactors[]` → `domains[]`.

What the fields are:

- `kind` — `leeway.contract_violated` (a declared hard constraint is broken), `leeway.placement_drift` (a soft, assumed or absent constraint, drifted), `leeway.baseline_breach` (daemon mode only), `leeway.domain_unavailable`.
- `tier` and `severity` — `A`/`critical`: the workload declared a hard contract (`whenUnsatisfiable: DoNotSchedule`, required pod anti-affinity). `B`/`warning`: a soft preference (`ScheduleAnyway`, preferred anti-affinity) or the assumed cluster default. `C`: no constraint at all, surfaced with `--topology-tier-c-signals` or when leeway could attribute a cause. Severity records where the expectation came from, not how exposed the workload is: a Tier B finding with every replica in one zone is the larger outage risk.
- `score.maxDomainShare` — the share of running replicas in the fullest domain (`1` is all of them). `observedSkew`, `minAchievableSkew`, `excessSkew`, `relocationDistance` are the arithmetic behind the verdict.
- `intent` — where the expectation came from. `source` is one of `topology-spread-constraint`, `pod-anti-affinity-required`, `pod-anti-affinity-preferred`, `pod-affinity-required`, `pod-affinity-preferred`, `workload-annotation`, `policy-crd`, `cluster-default-declared`, `cluster-default-assumed`, `learned-baseline`; `confidence` is `declared`, `inferred`, `assumed` or `learned`; `evidence[]` quotes the constraint verbatim and anything that contradicts it. A finding with no `intent` key is a Tier C row: the workload declared nothing on this axis.
- `domains[]` — one row per eligible zone: `actual` running replicas, `expected`, `delta`, `readyNodes`, and a `note` when the zone lost nodes. Pending replicas are not in this table.
- `suspectedCause` — one of `constraint_ignored`, `volume_pinning`, `domain_capacity_shortfall`, `taint_exclusion`, `domain_outage`, `consolidation`, `rollout_bias`, `unknown`. `contributingFactors[]` is the evidence for and against, including the Pending count and the scheduler's own `FailedScheduling` message. `untestedCauses[]` names what a one-shot report cannot test — always `rollout_bias`, which needs history; check the rollout yourself when it matters.
- `remediation` — `kind` is one of `manifest_patch`, `rollout_restart`, `cluster_capacity`, `cluster_nodes`, `storage_topology`; `summary` says what to do; `command` (a `kubectl rollout restart`) and `yaml` (a `topologySpreadConstraints` block) are present only for the first two kinds. The `yaml` is the spread-constraint half of a fix and nothing else — see row 1 below.

Top-level and cluster keys are snake_case (`topology_findings`, `rank_findings`, `by_severity`); finding fields are camelCase.

## Attributing the cause

For each workload finding on `topology.kubernetes.io/zone`, and for each zone the census marks unusable, find the row that matches what leeway printed, run its confirming read, and report the cause and remediation category the row gives. The category words are the ones the report line format uses: `scheduling`, `volume`, `capacity`, `node-pool`.

| #   | What leeway printed                                                                                                                                                                   | Cause                                                                                                                                       | Confirm with                                                                                                                          | Remediation category                                                                                                                                                                                                      |
| --- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | `suspectedCause: constraint_ignored`, `intent.evidence[]` carries `conflicting nodeAffinity/nodeSelector on topology.kubernetes.io/zone …`, `remediation.kind: manifest_patch`        | **scheduling** — a required `nodeAffinity` or `nodeSelector` pins the pods to one zone while a `ScheduleAnyway` spread constraint only asks | the pod template's `affinity.nodeAffinity` and `nodeSelector` (read 1)                                                                | manifest: remove or widen the affinity. The `remediation.yaml` re-renders the spread constraint only; applying it alone changes nothing. Never a capacity action.                                                         |
| 2   | `suspectedCause: constraint_ignored` without that evidence line; `remediation.kind: rollout_restart`                                                                                  | **scheduling** — the spread is `ScheduleAnyway`, every zone is eligible, and the scheduler placed anyway (a rollout or an earlier squeeze)  | pods per zone (read 2) and `kubectl rollout history`                                                                                  | a rolling restart rebalances (`remediation.command`, recommend only); `DoNotSchedule` if the spread is a requirement                                                                                                      |
| 3   | `suspectedCause: unknown`, `intent.source: cluster-default-assumed`, `remediation.kind: manifest_patch`, summary begins `Add a topologySpreadConstraint`                              | **scheduling** — no spread constraint declared; the assumed scheduler default (`maxSkew: 5`) is the only expectation                        | the pod template has no `topologySpreadConstraints` (read 1); the census shows another zone with `usable > 0`; no zonal PVCs (read 3) | manifest: add the constraint — here `remediation.yaml` is the whole fix                                                                                                                                                   |
| 4   | `suspectedCause: volume_pinning`, `contributingFactors[]` has `N pods pinned by zonal volumes`, `remediation.kind: storage_topology`                                                  | **volume** — PersistentVolumes with node affinity to one zone hold the pods there                                                           | the PVCs, their PVs' `spec.nodeAffinity`, and the StorageClass (read 3)                                                               | storage: regional StorageClass or volume migration. A `DoNotSchedule` spread on top of zonal volumes leaves new pods `Pending` (`VolumeNodeAffinityConflict`).                                                            |
| 5   | `suspectedCause: domain_capacity_shortfall`, `contributingFactors[]` has `N pods Pending with FailedScheduling: … Insufficient cpu` (or memory), `remediation.kind: cluster_capacity` | **capacity** — the under-filled zone has no room for the requests                                                                           | the Pending pods and their events (read 4); the zone's `allocatable_cpu_milli` in the census                                          | capacity: node-pool size or autoscaler limits in that zone, or smaller requests. The spread constraint is doing its job; do not edit it.                                                                                  |
| 6   | `kind: leeway.domain_unavailable`, `suspectedCause: taint_exclusion`, census `usable: 0`, note `… none schedulable`                                                                   | **capacity** — every node in the zone is cordoned or carries an untolerated taint                                                           | node taints and `SchedulingDisabled` per zone (read 5)                                                                                | nodes: uncordon, finish the drain, or a toleration if the taint is permanent. Workloads stacked in the other zones are a consequence; no spread edit for them.                                                            |
| 7   | `kind: leeway.domain_unavailable`, `suspectedCause: domain_outage`, census `ready: 0`, note `… none Ready`                                                                            | **capacity** — the zone's nodes are present and NotReady                                                                                    | node conditions per zone (read 5)                                                                                                     | nodes: node health or a zone incident; same consequence rule as row 6                                                                                                                                                     |
| 8   | **nothing** — the workload has no finding, yet its pods are all in one zone and it carries a `nodeSelector` or required `nodeAffinity` onto a node pool                               | **node-pool** — the pool exists in one zone; leeway honours the selector as intent and stays silent                                         | nodes per pool per zone (read 5) against the workload's selector (read 1)                                                             | node pool: add zones to the pool (`nodeLocations`) or move the workload to a multi-zone pool. Never widen the affinity onto zones where the pool has no nodes, and never a spread constraint — both leave pods `Pending`. |

Row 8 is the one to look for by hand on every analysis: list the multi-replica workloads in scope, and any with `maxDomainShare` of `1` in the report's `subjects[]` (with `--all`) or with all pods on one zone in read 2 and no finding is a candidate. `consolidation` and `rollout_bias` cannot fire in a one-shot report (both need history); the region axis has one domain on every regional cluster, so a `maxDomainShare` of `1` there means nothing.

The confirming reads:

```bash
# 1. the template's spread, affinity and selector
kubectl get deployment <name> -n <namespace> -o jsonpath='{.spec.template.spec.topologySpreadConstraints}{"\n"}{.spec.template.spec.affinity}{"\n"}{.spec.template.spec.nodeSelector}{"\n"}'
# 2. pods per zone (NODE column, then the node's zone label)
kubectl get pods -n <namespace> -l <selector> -o wide
# 3. volumes: claims, the PV's node affinity, the class's topology
kubectl get pvc -n <namespace>
kubectl get pv <pv-name> -o jsonpath='{.spec.nodeAffinity}{"\n"}{.spec.storageClassName}{"\n"}'
kubectl get storageclass <class> -o jsonpath='{.allowedTopologies}{"\n"}{.parameters}{"\n"}'
# 4. Pending pods and the scheduler's reason
kubectl get pods -n <namespace> --field-selector=status.phase=Pending -o wide
kubectl describe pod <pending-pod> -n <namespace>
# 5. nodes by zone and pool, with readiness and taints
kubectl get nodes -L topology.kubernetes.io/zone,cloud.google.com/gke-nodepool
kubectl get nodes -o custom-columns='NAME:.metadata.name,ZONE:.metadata.labels.topology\.kubernetes\.io/zone,UNSCHEDULABLE:.spec.unschedulable,TAINTS:.spec.taints[*].key'
```

Use `statefulset` in read 1 for a StatefulSet; its volumes come from `spec.volumeClaimTemplates`.

## Blast radius

For each workload, from the zone-axis finding's `domains[]`: running replicas per zone, and the share in the fullest zone (`score.maxDomainShare`, which the rows' `actual` values reproduce). Pending replicas come from the `contributingFactors[]` line, not from the table. State it as `<n>/<m> running in <zone>` and as a percentage; only where the census shows at least two zones with `usable > 0` does the number describe a zone failure, so on a single-zone cluster report the census instead.

## Worked example

`references/report-example.json` is a four-cluster run. Reading its first cluster, `prod-central`, in the order above: `status` is `ok`; the zone census has `us-central1-a` and `us-central1-b`, both `usable: 1`; there is no Domain finding; three workload findings follow.

- `Deployment/shop/checkout-api` — `placement_drift`, Tier B. `suspectedCause: constraint_ignored`; `intent.evidence[]` quotes the `ScheduleAnyway` constraint and then `conflicting nodeAffinity/nodeSelector on topology.kubernetes.io/zone restricts placement to 1 domain(s)`; `remediation.kind: manifest_patch`; `domains[]` has `actual: 2` in `us-central1-a`, `0` in `us-central1-b` with a ready node. Row 1: **scheduling**, 2/2 running in `us-central1-a` (100%); the fix is the affinity, and the `yaml` block is not it.
- `StatefulSet/shop/orders-db` — `placement_drift`, Tier C, no `intent` key. `suspectedCause: volume_pinning`; `contributingFactors[]`: `2 pods pinned by zonal volumes (subject claims zone-bound volumes)`; `remediation.kind: storage_topology`. Row 4: **volume**, 2/2 in `us-central1-a`; read 3 names the class.
- `Deployment/shop/batch-worker` — `contract_violated`, Tier A, `DoNotSchedule`. `suspectedCause: domain_capacity_shortfall`; `contributingFactors[]`: `3 pods Pending with FailedScheduling: 0/2 nodes are available: 1 Insufficient cpu, 1 node(s) didn't match pod topology spread constraints.`; `remediation.kind: cluster_capacity`; `domains[]` has `actual: 1` in `us-central1-b`, `0` in `us-central1-a`. Row 5: **capacity**, 1/1 running in `us-central1-b` plus 3 Pending; the constraint is correct and the zone is full.

The other clusters show the remaining rows: `prod-east`'s `Deployment/web/frontend-web` is row 3 (`cluster-default-assumed`, `manifest_patch`, `Add a topologySpreadConstraint …`), `prod-west` has a Domain finding for `us-west1-c` (`taint_exclusion`, census `usable: 0`), and `staging` is `status: "error"`. `prod-east` also runs `Deployment/finance/ledger-sync` on a pool that exists in one zone; it has no finding, which is row 8.

<!-- shared:reading-end -->

## Acting on what the sweep found

The confirming reads above run against a swept cluster as `kubectl --context=gke_<project>_<location>_<cluster> …`. Then, by row:

- **Rows 1 and 3** are the only manifest fixes. When the request asked for the fix, open one pull request per workload via `submit-suggestion`: for row 3 the change is `remediation.yaml`; for row 1 it is the `affinity.nodeAffinity` / `nodeSelector` edit, with the spread constraint left as it is or tightened to `DoNotSchedule` if the owner wants a guarantee. When the request asked for a diagnosis, the patch goes back in your reply as a recommendation (`SOUL.md` §3, item 3).
- **Row 2** is a `kubectl rollout restart` the cluster's owner runs; quote `remediation.command` and never run it.
- **Rows 4–8** are storage, capacity, node and node-pool changes. Report the cause and `remediation.summary`; a node-pool or StorageClass change the requester wants declared goes through `gcp-config-connector` or `gcp-terraform`, never as a pod-template edit.

Never open a pull request against a workload whose finding is row 4, 5, 6 or 7, and never widen a node-pool-pinned workload's affinity (row 8).

## Reporting a sweep

One table per cluster that was read — workload, zone distribution, blast radius, cause row and category, remediation category — then the clusters the report could not read (`status: "error"`, with the `error` string), then the single-zone clusters the census ruled out. `summary.clusters_failed` above zero is a partial sweep and is said so in the first line. When the requester gave a line format for the per-workload summary, emit those lines verbatim from the table.
