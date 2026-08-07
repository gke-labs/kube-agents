# SOP: Fleet Waste Audit (Weekly Governance)

**Purpose:** Weekly sweep of the GKE fleet for resources that cost money and do nothing — orphaned storage, idle network reservations, under-allocated node pools, grossly over-requested workloads, and the scale-down blockers that pin them. The output is one ledger issue of reproducible findings, not a dashboard.

**Cron:** id `fleet-wide-cost-analysis`, schedule `50 7 * * 1` (Mondays, 07:50 UTC).

**Data sources:** `kubectl` read verbs (including `kubectl top`), `gcloud compute` / `gcloud container`, and the `gke` MCP server. **Nothing else.** This environment has no GCP Billing BigQuery export, no GKE Cost Allocation data, no Recommender API, and no Prometheus — so this audit observes _physical_ waste directly and reports it in **resource units** (GiB, vCPU, disk count, node count, object count). **Never state a dollar amount, a monthly saving, or a percentage saving — you have no pricing data.** Where a price would help the reviewer decide, say so in the remediation note and point at the GCP console; never invent the number. (`list_cc_pods` / `get_cc_pod_diagnostics` are scoped to `krmapihosting-system` on the Config Controller management cluster and are not used by this audit.)

---

## Execution Checklist

### 0. Open the audit run

Run `./skills/fleet-audit/scripts/audit_report.py start --audit fleet-wide-cost-analysis`. It prints one JSON line:

```json
{
  "issue": 128,
  "repo": "acme/fleet",
  "workspace": "/opt/data/gitops/fleet-wide-cost-analysis/acme__fleet",
  "findings_path": "/opt/data/scratch/findings_fleet-wide-cost-analysis.json",
  "pending_remediation_requests": [
    "overrequest--prod-us-east--deployment-payments-api"
  ]
}
```

Keep the returned `findings_path`; write findings there and nowhere else. Keep `workspace` too: it is the GitOps clone `start` created, because this pod does not begin inside a checkout, and it is the root every `remediation.path` in §4 is resolved against. `issue` is this stream's open ledger issue, or `null` when it has none — either way `finish` opens or rewrites it. `pending_remediation_requests` names findings a repo writer asked for with a `/remediate` comment on the ledger: write the manifest for each of those during inspection, **unless** the finding's remediation is `gcloud` or `manual` — those are not promotable, the helper refuses the request with its own comment, and you never manufacture a manifest (least of all a deletion) to satisfy one. Each `/remediate` receives exactly one answer on the ledger — an acknowledgement listing every target and its outcome, or one refusal naming the reason (no write access, an unknown id, a non-`manifest` target, or a command not written at the start of its own line and so never parsed as one) — recorded behind a hidden marker so the same request is not answered twice.

There is no report branch. Do not create branches, commit, push, or call `gh` yourself, and never hand-write the ledger issue body — the helper renders it. **Never comment on the ledger yourself:** `/remediate` is a human reviewer's instruction to this harness, not a step in the audit, and an agent that posts it — including when someone asks for a fix in chat — is authorizing its own pull request. `finish` ignores a `/remediate` from a machine account, so posting one achieves nothing but noise on the issue.

### 1. Enumerate the target fleet

1. `gcloud config get-value project`, then `gcloud container clusters list --project=<p> --format="json(name,location,status,autopilot.enabled,currentNodeCount)"` for every project the agent can see.
2. `scope.clusters` = every cluster with `status: RUNNING` that you could read. Record every cluster you could **not** read in `scope.skipped` with its status or the stderr excerpt as the reason (`"cluster STOPPING"`, `"no kubeconfig: <stderr excerpt>"`).

   **The one-question scope rule.** A cluster appears in exactly one scope list. Could you read it? Yes → `scope.clusters`; if some checks did not run there, name them in that cluster's `limitations`. No → `scope.skipped`. Nothing goes in both, and nothing in `scope.skipped` may appear in a finding. The validator enforces all three.

   **Each `scope.clusters` entry is `{name, location, project, checks_run}`, and `checks_run` is mandatory.** Each entry in it is an object, never a bare string:

   ```json
   {
     "check": "orphan-pv",
     "command": "kubectl --context prod-usc1 get pv -o custom-columns=NAME:.metadata.name,PHASE:.status.phase,CLAIM:.spec.claimRef.name --no-headers"
   }
   ```

   `check` is the backticked slug from the §3 heading that defines it — `overrequest`, `orphan-pv`, and so on — never the section number and never prose. (`start` prints the full roster of ten; the SOP still says what each check _is_.) `command` is the literal invocation you issued on that cluster for that check, with its `--context`/`--project` and the namespace or resource it targeted. It must name one of `kubectl`, `gcloud`, `gsutil`, `bq`, `helm`, or `curl`; `echo`, `cat`, `python3 -c`, and a call back into `audit_report.py` are all rejected, as is anything under eight characters.

   The validator rejects an unknown slug, a duplicate, a missing or unusable command, the field being absent, and an empty list unless that cluster's `limitations` says why nothing ran: a cluster you could read but ran nothing against is not a clean cluster, it is an audit that did not happen. Anything short of the checks that apply to that cluster makes the run **partial** exactly as a `limitations` note does, so the ledger stays open and nothing is announced as resolved. Append the entry when its check completes, not when you intend to run it, and paste the command rather than reconstructing it — every one is published verbatim in the ledger under _How this run checked the fleet_.

   **A check the cluster's shape rules out is not a gap — declare it.** Alongside `checks_run`, a cluster may carry `checks_not_applicable` as a list of `{check, reason}`:

   ```json
   {
     "check": "idle-nodepool",
     "reason": "GKE Autopilot: Google owns the node pools; there is no autoscaler floor to lower."
   }
   ```

   Same slugs as `checks_run`, and the `reason` must say why the check _cannot_ apply here — "N/A" and "not applicable" are rejected; name the property of the cluster that rules it out. Those checks leave the denominator instead of counting as missing, so an Autopilot cluster (where 3.7 has no user-managed pool to act on — 3.8 still does, see §2) reads as complete at nine of nine rather than forever-incomplete at nine of ten. Without it, every run on an Autopilot fleet is partial forever, `resolved` is pinned at `0`, and the ledger can never close. Use it only for checks the cluster's shape rules out. A check you could have run and did not is a `limitations` note and a real gap, and the validator rejects a slug in both lists, a duplicate, an unknown slug, and a reason under sixteen characters.

3. Get credentials per cluster into an **isolated kubeconfig**, so two clusters cannot bleed into each other through a shared current-context:

   ```bash
   export KC="${HERMES_HOME:-/opt/data}/.kubeconfigs/kubeconfig_<project>_<cluster>_<location>.yaml"
   KUBECONFIG=$KC gcloud container clusters get-credentials <cluster> --location=<location> --project=<project>
   ```

   Every `kubectl` in Step 2 runs with that cluster's `KUBECONFIG=$KC`. On failure, skip that cluster with the stderr excerpt as the reason and continue — one unreachable cluster never aborts the run.

4. **If zero clusters are RUNNING and reachable, do not call `finish`** — the helper hard-fails on an empty `scope.clusters`. A fleet you could not read is not a clean fleet, so this is **not** a `[SILENT]` run: report the enumeration failure as your one-line summary and stop.

### 2. Collect state

Collect once per cluster into `/opt/data/scratch/`, then run all checks against the dumps — never re-query per finding, and quote from these dumps as `evidence.excerpt`.

- Per cluster: `KUBECONFIG=$KC kubectl get nodes,pods,pvc,pv,svc,jobs,cronjobs,pdb,ns,resourcequota -A -o json`, plus `gcloud container node-pools list --cluster=<c> --location=<l> --format=json`.
- Per project: `gcloud compute disks list`, `addresses list`, `forwarding-rules list`, `target-pools list`, `backend-services list` (all `--format=json`).
- **Usage samples:** take three `KUBECONFIG=$KC kubectl top pods -A --no-headers` + `KUBECONFIG=$KC kubectl top nodes --no-headers` samples, ~5 minutes apart (`sleep 300` between). Save each as `top-<cluster>-<n>.txt`.
- **Metrics degradation:** if `kubectl top nodes` exits non-zero or `metrics.k8s.io` is absent from `KUBECONFIG=$KC kubectl api-resources`, the cluster still belongs in `scope.clusters` — you read it. Set `"limitations": "metrics-server unavailable: <stderr excerpt>; usage check 3.1 did not run."` on that cluster's `scope.clusters` entry and skip **only** 3.1 there. Every other check reads requests and object state, not usage, and still runs.
- **Autopilot:** where `autopilot.enabled` is true, there are no user-managed node pools — skip 3.7 and add `{"check": "idle-nodepool", "reason": "GKE Autopilot: Google owns the node pools; there is no autoscaler floor to lower."}` to that cluster's `checks_not_applicable`. **Not `limitations`:** a non-empty `limitations` string is what makes a run `partial`, and a cluster does not stop being Autopilot, so parking the mode there would pin the weekly cost stream at `partial: true` for good — `resolved: 0`, ledger never closing. An Autopilot cluster is never a skipped cluster either; you read it fine. 3.8 still applies (Autopilot scales down and unevictable pods still block it), and 3.1 matters _more_ (Autopilot bills on requests), so **bump 3.1 severity one level on Autopilot clusters**.

**Shared definitions used by every check below.** `SYSTEM_NS` = `kube-system`, `kube-public`, `kube-node-lease`, `gmp-system`, `gmp-public`, `gke-gmp-system`, `cnrm-system`, `configconnector-operator-system`, `krmapihosting-system`, `istio-system`, `asm-system`, `anthos-identity-service`, `gatekeeper-system`, `composer-system`, plus any namespace matching `gke-*`, `gke-managed-*`, or `config-management-*` — the same fleet-wide set the Security & RBAC Posture Audit calls `$SYS` and the Workload Reliability Audit calls S1. Note it is `anthos-identity-service` and not `anthos-*`: a glob there would silently hide waste in any namespace a team happened to prefix `anthos-`. `kubeagents-system` is deliberately **not** suppressed; the harness audits itself. `AGE(x)` = now minus `creationTimestamp`. "All samples" = all three usage samples from this run.

### 3. Checks

**Severity discipline.** Waste is not an incident. Default to `minor`; use `major` only for magnitude (roughly a node's worth of capacity, ≥500 GiB of storage, or a systemic leak). **`critical` is reserved for two conditions only:** (a) a condition that blocks node drain, which blocks both scale-down _and_ security patching; (b) a waste finding whose obvious remediation is deletion of an object holding the only copy of data — verified by `gcloud compute snapshots list --filter="sourceDisk~<disk-name>"` returning nothing. Rule (b) is a **warning to the reviewer**, not an urgency claim; say so in the impact line. Nothing else reaches `critical` — in particular the one-level Autopilot bump in 3.1 stops at `major`, because a `critical` finding whose remediation is a manifest opens a merge-ready remediation pull request automatically (§4), and a right-size patch derived from a ten-minute sample is never that urgent.

**Finding identity.** **Do not write an `id`.** The harness derives it from `check`, `cluster`, `namespace` and `object`, and ignores any `id` in the file. `check` is the backticked slug in the check's heading below; a slug outside this SOP's roster is rejected.

Identity is only as stable as those four fields, so **never** let a timestamp, a counter, a sample value, an age, or a usage figure into `object` — a waste finding re-measured tomorrow is the same finding, and one whose identity moves is announced as fixed and re-announced as new. Two findings agreeing on all four fields are one finding, and the harness refuses the document rather than collapsing them silently.

**Project-scoped GCP objects.** The harness requires a non-empty `cluster` on every finding. For a disk, address, or forwarding rule that belongs to a project rather than to one cluster, set `cluster` to the literal string `project/<project-id>` and leave `namespace` empty. Attribute to a real cluster name only when the labels or the name prefix prove it.

#### 3.1 Gross over-request vs. observed usage (`overrequest`)

**This check owns the request _value_.** The Workload Reliability audit (`obtainability-audit` check 3.1, `no-requests`) owns the request's _absence_: it flags a container that declares no request at all and deliberately proposes no number, deferring the sizing to here, where the usage samples are. A container with no request is that audit's finding; a container whose declared request dwarfs its usage is this one's. Neither audit ever restates the other's half — two streams sizing one container in opposite directions makes each run flag the state the other just created.

- **Command:** `kubectl top pods -A --no-headers` (×3, per Step 2) joined against `kubectl get pods -A -o json | jq '[.items[]|{ns:.metadata.namespace,pod:.metadata.name,owner:(.metadata.ownerReferences[0]//{}),start:.status.startTime,req:[.spec.containers[].resources.requests],lim:[.spec.containers[].resources.limits]}]'`, aggregated to the owning controller.
- **Flag when:** for a controller, **every** sample shows aggregate usage ≤ 20% of aggregate requests (use the per-container **maximum across samples**, never the mean), **and** the reclaimable delta is material: ≥ 2 vCPU **or** ≥ 4 GiB summed across its pods. _Justification:_ sane practice requests at p50–p95 with 1.3–2× headroom; a 5× gap is not headroom, it is a copy-pasted default. The absolute floor stops the audit from flagging a 10m sidecar where the percentage is arithmetically true and operationally meaningless.
- **Sampling honesty — state this in the impact line:** `kubectl top` is an instantaneous sample, not a percentile. Three samples over ~10 minutes on one Monday morning **cannot see a nightly batch peak or a weekday traffic curve**. That is why this check requires all three samples to agree, uses the peak rather than the mean, carries an absolute floor, and — see remediation — never proposes a request below 2× the observed peak. The impact line must name the sample window and date so the reviewer knows the basis.
- **Do NOT flag:** `SYSTEM_NS`; DaemonSets (their requests are per-node overhead, not a sizing choice); pods with `AGE < 1h` (cold caches, JIT warmup); anything owned by a `Job` or `CronJob` (periodic by design, sampled at the wrong moment); workloads with an HPA whose `minReplicas` floor is the real lever; `BestEffort` pods (no requests to shrink); pods in `Pending`/`Terminating`; workloads whose request equals the namespace `LimitRange` default (fix the LimitRange, not the workload — note it once per namespace instead).
- **Severity:** `major` if the reclaimable delta is ≥ 8 vCPU or ≥ 32 GiB across the controller (a node's worth); else `minor`. One level higher on Autopilot.
- **Impact:** "Deployment `ns/name` requests 12 vCPU / 48 GiB; peak observed across 3 samples on 2026-08-03 07:50–08:00 UTC was 0.9 vCPU / 3 GiB — ~11 vCPU and ~45 GiB reserved and unschedulable by anything else."
- **Remediation:** `kind: manifest`, subject to §4's declaration rule — the controller's **complete** desired manifest, taken from its declaration in the GitOps repo, with `requests` set to `ceil(peak × 2)` (floor `50m` / `64Mi`) and limits untouched. **Skip the manifest and emit `kind: manual` for `Guaranteed`-QoS pods** (requests == limits): lowering requests there silently changes the QoS class and the eviction ordering. Note: "confirm against a longer observation window before merging; this is a 10-minute sample."

#### 3.2 Orphaned PersistentVolumes (`orphan-pv`)

- **Command:** `kubectl get pv -o json | jq '[.items[]|select(.spec.persistentVolumeReclaimPolicy=="Retain")|{name:.metadata.name,phase:.status.phase,since:.status.lastPhaseTransitionTime,created:.metadata.creationTimestamp,cap:.spec.capacity.storage,sc:.spec.storageClassName,claim:.spec.claimRef,handle:(.spec.csi.volumeHandle//.spec.gcePersistentDisk.pdName)}]'`
- **Flag when:** phase is `Released` or `Failed` and `status.lastPhaseTransitionTime` is ≥ 7 days old; or phase is `Available` with a null `claimRef` and `AGE ≥ 30d` and no `Pending` PVC in the fleet matches its storage class. If `lastPhaseTransitionTime` is absent (pre-1.31 control plane), fall back to `AGE ≥ 30d` and say so in the evidence excerpt. _Justification:_ 7 days outlives any deploy/rollback cycle and any weekly batch job; a `Retain` PV that has sat unclaimed for a week was abandoned, not paused. 30 days for `Available` because statically provisioned PVs are legitimately pre-staged.
- **Do NOT flag:** `Delete`-policy PVs (the disk is already gone); PVs whose `claimRef` names a PVC that still exists; PVs whose claim name matches `^<vct>-<sts>-[0-9]+$` for a StatefulSet that still exists in that namespace — **a StatefulSet scaled to zero is a deliberate state, not waste**; PVs annotated by a backup system (`velero.io/*`, `gke.io/backup-*`) or carrying a retention marker; PVs provisioned for GKE-managed addons; anything younger than 7 days in the terminal phase (deletion churn).
- **Severity:** `major` if capacity ≥ 100 GiB or the storage class is SSD/extreme; else `minor`. `critical` under severity rule (b).
- **Impact:** "PV `pvc-abc123` (500 GiB, `premium-rwo`) has been `Released` with `Retain` since 2026-06-12 — the backing disk still exists and no claim can bind it."
- **Remediation:** `kind: manual`. Steps, in order: identify the backing disk from `spec.csi.volumeHandle`; `gcloud compute disks describe` it; **snapshot it** (`gcloud compute snapshots create <disk>-preaudit --source-disk=<disk> --source-disk-zone=<z>`); confirm the owning team no longer needs the data; only then delete. **Never emit a manifest that deletes a PV or PVC.**

#### 3.3 Bound PVCs with no consuming pod (`unconsumed-pvc`)

- **Command:** `kubectl get pvc -A -o json` cross-referenced against `kubectl get pods -A -o json | jq -r '.items[]|.metadata.namespace+"/"+(.spec.volumes[]?|.persistentVolumeClaim.claimName//empty)'`.
- **Flag when:** the PVC is `Bound`, no pod in **any** phase in that namespace references it across all samples, and `AGE ≥ 14d`. _Justification:_ 14 days spans a full deploy/rollback cycle and two runs of this weekly audit, so a flagged PVC has been observed unconsumed at least twice.
- **Do NOT flag:** `SYSTEM_NS`; PVCs whose name matches a live StatefulSet's `volumeClaimTemplate` pattern (scaled-to-zero StatefulSets are deliberate); PVCs referenced by a suspended CronJob's job template or a not-yet-started Job; PVCs referenced by a `VolumeSnapshot` in progress; PVCs with a `WaitForFirstConsumer` storage class that are still `Pending`; PVCs in a namespace under active GitOps sync where the workload is temporarily scaled down (`configsync.gke.io/*` annotations present).
- **Severity:** `major` if ≥ 100 GiB or an SSD class; else `minor`.
- **Impact:** "PVC `ns/data-old` (200 GiB, `standard-rwo`) is Bound with no pod mounting it since at least 2026-07-18 — 200 GiB provisioned and unread."
- **Remediation:** `kind: manual` — confirm with the namespace owner, snapshot the volume, then delete the PVC (which deletes the PV under `Delete`). Never a manifest.

#### 3.4 Unattached Compute Engine persistent disks (`unattached-disk`)

- **Command:** `gcloud compute disks list --project=<p> --filter="-users:* AND creationTimestamp<-P30D" --format="json(name,sizeGb,type,zone,region,creationTimestamp,labels,description)"`
- **Flag when:** the disk has no `users` and `AGE ≥ 30d`. _Justification:_ PD-CSI detaches and reattaches during node upgrades, pod rescheduling, and maintenance windows; 30 days outlives a full monthly GKE maintenance cycle, so this is not churn.
- **Attribution decides what the finding says, not whether it is emitted.** Try the `goog-k8s-cluster-name` / `goog-k8s-cluster-location` labels, a `gke-<cluster>-` name prefix, and a `kubernetes.io-created-for/pv-name` entry in the description, in that order. If one of them names a cluster you audited, set `cluster` to that cluster's real name. If none does, set `cluster` to the literal `project/<project-id>` per §3's project-scoped rule, with `object: Disk/<name>`, and say in the impact line that ownership could not be determined.
- **Do NOT flag:** disks whose name or handle matches a live PV's `spec.csi.volumeHandle` (claimed, just not currently attached); node boot disks for a pool that is mid-upgrade or mid-scale (`gcloud container operations list` shows an in-flight operation); disks with labels from a managed service (`goog-composer-*`, `goog-dataproc-*`, `goog-gke-node` on a live pool); disks in a project where no cluster reached `scope.clusters`.
- **Severity:** `major` if `sizeGb ≥ 500` or the type contains `ssd`/`extreme`; else `minor`. `critical` under severity rule (b), which reaches only the attributable case — rule (b) is about a deletion recommendation, and an unattributable disk never carries one.
- **Impact:** attributable — "3 unattached `pd-ssd` disks in `us-central1` totalling 1,500 GiB, unattached since 2026-05-30, all labelled for cluster `prod-usc1`." Unattributable — "Unattached 500 GiB `pd-ssd` disk `data-2024` in `us-central1`, unattached since 2026-05-30; no label, name prefix, or description ties it to any cluster this audit read, so its owner is unknown."
- **Remediation — attributable disk:** `kind: gcloud`, printed for a human to run, never executed here: `gcloud compute disks describe` → `gcloud compute snapshots list --filter="sourceDisk~<name>"` → **snapshot if none exists** → `gcloud compute disks delete`. Note verbatim: "Verify before deleting — an unattached disk can be a deliberate cold archive. Snapshot first. Price the disk in the console if you need the figure."
- **Remediation — unattributable disk:** `kind: manual`, and **never a deletion**. This audit knows nothing about who owns the disk, so it does not get to recommend destroying it. `recommendation.action` is to identify the owner — read `gcloud compute disks describe <name> --zone=<z>` for labels, description, and creation metadata, and check the project's audit log for the principal that created it — and only then decide. Naming a delete command for a disk whose owner you could not determine is the single most dangerous thing this audit can do.

#### 3.5 Reserved static IP addresses not in use (`idle-address`)

- **Command:** `gcloud compute addresses list --project=<p> --filter="status!=IN_USE" --format="json(name,address,addressType,purpose,region,status,creationTimestamp,users,description)"`
- **Flag when:** `addressType == EXTERNAL` **and** `status == RESERVED` **and** `AGE ≥ 14d`. All three, every time. _Justification:_ an IP reserved ahead of a cutover is normal for a sprint; two weeks exceeds a typical cutover window and means this audit has already seen it twice. A reserved-but-unused **external** IP bills continuously — an internal one is free, so it is not waste and flagging it is a false positive whose remediation is an irreversible release. `addressType` is projected by the command above precisely so you can test it; the API returns `EXTERNAL` or `INTERNAL`.
- **Do NOT flag:** addresses whose `purpose` is `GCE_ENDPOINT`, `VPC_PEERING`, `PRIVATE_SERVICE_CONNECT`, `NAT_AUTO`, `SHARED_LOADBALANCER_VIP`, or `IPSEC_INTERCONNECT` (legitimately "reserved" while serving) — `GCE_ENDPOINT` is the purpose GCP assigns a reserved internal ILB VIP and a VM alias-IP range, so leaving it off this list turns every internal load balancer in the fleet into a waste finding; addresses named in any fleet annotation — `kubernetes.io/ingress.global-static-ip-name`, `networking.gke.io/load-balancer-ip`, `cloud.google.com/load-balancer-ip`, `networking.gke.io/addresses` — grep the Step 2 dumps for the address name and value before flagging; addresses whose description mentions DR, failover, or a planned migration.
- **Severity:** `minor` per address. If a single project holds ≥ 10 idle addresses, emit one roll-up finding at `major` instead of ten `minor` ones — that is a leak, not a leftover.
- **Impact:** "4 external addresses in `us-central1` have been `RESERVED` and unattached for 40+ days."
- **Remediation:** `kind: gcloud` — `gcloud compute addresses describe <n> --region=<r>` to confirm nothing references it, then `gcloud compute addresses delete`. Caution: "Releasing an external IP is irreversible — the address returns to the pool and cannot be reclaimed. Confirm no DNS record points at it."

#### 3.6 Orphaned load-balancer resources (`orphan-lb`)

- **Command:** `gcloud compute forwarding-rules list --project=<p> --format="json(name,IPAddress,region,target,description,creationTimestamp)"`, plus `target-pools list` and `backend-services list --format="json(name,backends,creationTimestamp)"`.
- **Flag when:** a forwarding rule's description carries a `kubernetes.io/service-name` whose `namespace/name` no longer exists anywhere in the fleet dumps and `AGE ≥ 7d`; or a target pool has zero instances; or a backend service has zero backends. _Justification:_ the GKE service controller tears LB resources down within minutes of Service deletion; a week is three orders of magnitude past normal reconcile latency and past any controller outage.
- **Do NOT flag — this is the highest-risk cross-check in the audit:** **run it only if every cluster in that project was successfully enumerated in Step 1.** If any cluster in the project sits in `scope.skipped`, skip the check for that whole project — the Service may live in the cluster you could not read — and declare the omission as `"limitations": "3.6 not run: cluster <skipped> in project <p> could not be enumerated."` on **every** cluster of that project that is in `scope.clusters`. Never add a second `scope.skipped` row for a cluster that is already in `scope.skipped`; a duplicate fails validation and publishes nothing. Also exclude: rules with no `kubernetes.io/*` description (Terraform- or human-managed); rules backing a `MultiClusterService`/`MultiClusterIngress` (the Service lives at fleet level); PSC service attachments; internal rules whose target is a live backend service.
- **Severity:** `major` — an orphaned forwarding rule keeps a load balancer and usually an IP alive.
- **Impact:** "Forwarding rule `a1b2c3` still targets deleted Service `staging/checkout-lb`; its backend pool has been empty since 2026-06-20."
- **Remediation:** `kind: gcloud` — `gcloud compute forwarding-rules describe` to confirm the target, then delete the rule, then its target pool / backend service, then release the address. Caution: "Confirm no DNS record or external client still resolves to this IP."

#### 3.7 Under-allocated node pools and non-zero autoscaler floors (`idle-nodepool`)

- **Command:** `gcloud container node-pools list --cluster=<c> --location=<l> --format="json(name,initialNodeCount,config.machineType,config.taints,autoscaling,management)"` joined against per-node request sums from `kubectl get pods -A --field-selector=status.phase=Running -o json` versus `.status.allocatable` in `kubectl get nodes -o json`.
- **Flag when:** every node in the pool shows non-DaemonSet CPU requests ≤ 15% **and** memory requests ≤ 15% of allocatable across all samples, and the pool has ≥ 1 node, and either autoscaling is disabled or `autoscaling.minNodeCount ≥ 1`. _Justification:_ measure **requests**, not usage — requests are what the cluster autoscaler acts on, so they are what determines whether the pool can shrink. 15% is below the point where DaemonSet and system overhead (typically 10–25% of a small node) dominates: a pool at ≤ 15% non-DaemonSet allocation is not carrying a workload. A `minNodeCount ≥ 1` floor on such a pool is capacity nobody asked for.
- **Do NOT flag:** Autopilot clusters (this check does not apply there and is declared in their `checks_not_applicable` per Step 2); pools created < 7 days ago; pools already at `minNodeCount: 0` with zero nodes (nothing to reclaim); pools whose nodes are `SchedulingDisabled` or mid-upgrade; the cluster's only node pool (system pods need somewhere to land); pools where DaemonSets alone account for the allocation _and_ the pool is a deliberate burst pool with `minNodeCount: 0`. GPU/TPU pools are **not** suppressed — an idle accelerator pool with a non-zero floor is the single largest reclaimable item this audit can find.
- **Severity:** `major` if the pool has ≥ 3 nodes, a machine type of ≥ 8 vCPU, or attached accelerators; else `minor`.
- **Impact:** "Node pool `gpu-burst` (2 × `a2-highgpu-1g`, `minNodeCount: 1`) has held ≤ 4% non-DaemonSet CPU allocation across all samples — 2 accelerator nodes reserved for nothing."
- **Remediation:** `kind: gcloud` — `gcloud container node-pools update <pool> --cluster=<c> --location=<l> --enable-autoscaling --min-nodes=0`, or delete the pool if it has been empty for consecutive audits. Caution: "Setting `min-nodes=0` lets the autoscaler drain these nodes; confirm no workload depends on warm capacity. Deleting a pool evicts everything on it."

#### 3.8 Scale-down blockers pinning under-allocated nodes (`scaledown-blocked`)

**PodDisruptionBudgets are out of scope here.** The Workload Reliability audit (`obtainability-audit`, checks 3.3 and 3.4) owns whether a PDB exists and whether its shape blocks drains, and it emits the rewrite. It reads every PDB in the fleet daily and knows each one's target replica count; this audit sees only the PDBs sitting on the handful of nodes 3.7 flagged, weekly. Two ledgers proposing two edits to one PDB is how a workload owner gets contradictory pull requests. So: where a flagged node's only blocker is a PDB, emit **no** finding — it is already reported there.

- **Command:** `kubectl get pods -A -o json` filtered to pods on the nodes 3.7 already found under-allocated, read for `metadata.ownerReferences`, the `cluster-autoscaler.kubernetes.io/safe-to-evict` annotation, and `emptyDir`/`hostPath` entries in `spec.volumes`.
- **Flag when:** an under-allocated node hosts at least one pod the autoscaler cannot evict for a reason that is **not** a PDB — the annotation `cluster-autoscaler.kubernetes.io/safe-to-evict: "false"`, a bare pod with no `ownerReferences`, or a pod with local storage (`emptyDir`/`hostPath`) and no `safe-to-evict: "true"`. Evaluate only nodes 3.7 flagged — that keeps the check cheap and keeps it a waste finding rather than a policy opinion.
- **Do NOT flag:** `SYSTEM_NS`; any node whose only unevictable pod is held by a PDB (see above); nodes already cordoned or mid-upgrade; pods being deliberately pinned by a documented anti-eviction annotation with an owner label.
- **Severity:** `critical` when nothing will ever reschedule the pod — a bare pod with local storage, or `safe-to-evict: "false"` on a pod with no controller — because that blocks node drain permanently, which blocks autoscaler scale-down _and_ security patching and node upgrades. `major` otherwise.
- **Impact:** "Node `gke-prod-default-a1b2` sits at 8% CPU allocation but cannot be drained: bare pod `ci/debug-shell` has no `ownerReferences` and mounts an `emptyDir`, so the autoscaler will hold this node indefinitely."
- **Remediation:** `kind: manual` — move the workload under a controller so it can be rescheduled, or have its owner clear the `safe-to-evict: "false"` annotation once the local state it protects is understood. Caution: "A pod pinned with local storage may hold state that does not survive rescheduling — confirm with the owner what is in the volume before it moves."

#### 3.9 Terminal pods and TTL-less Jobs (`terminal-pods`)

- **Command:** `kubectl get pods -A --field-selector=status.phase==Succeeded -o json` and `...==Failed -o json`, plus `kubectl get jobs -A -o json | jq '[.items[]|{ns:.metadata.namespace,name:.metadata.name,owner:(.metadata.ownerReferences[0].kind//""),ttl:.spec.ttlSecondsAfterFinished,done:.status.completionTime}]'`.
- **Flag when:** a namespace holds ≥ 50 terminal pods, or any terminal pod is older than 7 days; or a standalone Job (no CronJob owner) has been `Complete`/`Failed` for ≥ 7 days with `ttlSecondsAfterFinished` unset. _Justification:_ 50 is well above the handful a normal rollout leaves behind and well below the point where a single namespace hurts anyone; the 7-day floor keeps a debugging engineer's failed pods intact through a working week.
- **Do NOT flag:** `SYSTEM_NS`; pods younger than 24h (someone may still be reading them); Jobs owned by a CronJob (`successfulJobsHistoryLimit` already bounds them, unless it is set above 10 — then flag the CronJob, not the Jobs); Jobs that already set `ttlSecondsAfterFinished`; resources owned by a controller with its own GC (`workflows.argoproj.io/*`, `tekton.dev/*`, `fluxcd.io/*` labels).
- **Severity:** `minor`. `major` when one namespace exceeds 500 terminal pods or the cluster total exceeds 2,000 — at that point it is etcd object growth and API-server list latency, not just untidiness.
- **Impact:** "Namespace `ci` holds 1,840 `Succeeded` pods, the oldest from 2026-03-11 — 1,840 etcd objects listed on every full API-server sweep."
- **Remediation:** `kind: manifest`, subject to §4's declaration rule — the one safe additive change in this SOP: set `ttlSecondsAfterFinished: 86400` on the Job spec or the CronJob's `jobTemplate.spec` in the object's GitOps declaration, rewriting that file complete. Change the declaration, never a live completed Job, and if the object has no declaration you can find, the finding is `kind: manual`. Note that the existing backlog still needs a one-off `kubectl delete pod --field-selector=status.phase==Succeeded -n <ns>` run by a human.

#### 3.10 Idle namespaces holding billable objects (`idle-namespace`)

- **Command:** `kubectl get ns -o json` cross-referenced against the pod, PVC, Service, and ResourceQuota dumps from Step 2.
- **Flag when:** a non-`SYSTEM_NS` namespace has zero `Running`/`Pending` pods in **all** samples, `AGE ≥ 30d`, and still holds at least one billable or quota-holding object: a PVC, a `type: LoadBalancer` Service, or a ResourceQuota reserving capacity. _Justification:_ a namespace with no workload but a bound volume or a live load balancer is paying for nothing; 30 days covers a monthly release cycle and a pre-provisioned environment awaiting its first deploy.
- **Do NOT flag:** `SYSTEM_NS`; namespaces holding only Secrets, ConfigMaps, RBAC, or CRs (free); namespaces under an active GitOps sync (`configsync.gke.io/*`, `kustomize.toolkit.fluxcd.io/*` annotations) — the controller owns their lifecycle; namespaces that are the target of a suspended or infrequent CronJob (idle by design); namespaces carrying an explicit ownership or retention annotation; namespaces in `Terminating`.
- **Severity:** `major` if it holds a `LoadBalancer` Service or ≥ 100 GiB of PVCs; else `minor`.
- **Impact:** "Namespace `demo-q1` has run no pods for 30+ days but still holds a `LoadBalancer` Service and 250 GiB of PVCs."
- **Remediation:** `kind: manual` — confirm the owner, then remove the LB Service and snapshot-then-delete the PVCs before deleting the namespace. Caution: "Deleting a namespace deletes everything in it, including PVCs whose PVs use `Delete`. Snapshot first."

### 4. Generate remediation artifacts

Only two manifest kinds are permitted, both purely additive: the `ttlSecondsAfterFinished` change (3.9) and the request right-size change (3.1). **Both target an object that already exists**, so both go to that object's **existing declaration in the GitOps repo**: locate it in the `workspace` clone (`grep -rl "name: <object>" --include='*.yaml' <workspace>`), give that file's path relative to the clone root as `remediation.path`, and rewrite it as the object's complete desired manifest. **Open the hits before you trust them.** `grep` is kind-blind and unanchored: `name: <object>` also matches every `app.kubernetes.io/name:` label line and every object whose name merely starts with the one you flagged, and it will happily return a file under another cluster's directory. A hit is not a declaration until you have opened it and confirmed it declares the object you observed, on the cluster you observed it on. **If the hits land in more than one directory, or none can be tied to the target cluster, the finding is `kind: manual`.** Never emit a patch fragment — a file carrying `metadata.name` and a partial `spec` is not valid `kubectl apply` input, and a second file claiming an object the repo already declares is a duplicate resource id that both Config Sync and Argo reject. **When you cannot find a declaration for the object, the finding is `kind: manual`**, with the intended change in `recommendation.action` and no file written; do not invent a path for it. This audit has no create case at all — both permitted kinds target an object that already exists. Should one ever be added, the new file belongs in the directory that already holds the applied declarations for that cluster and namespace, discovered from a sibling; **never a new top-level directory and never a parent directory that is not already in the clone**, because the repository is reconciled by a tool that applies a fixed set of paths and a file outside them is applied by nothing.

Write every manifest into the `workspace` clone **before** calling `finish` — the helper stages the path and rejects the whole document if it is absolute, contains `..`, carries a glob metacharacter (`* ? [ ]`), or starts with `:`. A path that is merely not on disk is treated differently: it costs you the fix, not the run. That finding degrades to `kind: manual` with its evidence and recommendation intact, the ledger records that the audit named the file and never wrote it, and everything else publishes. A right-size that arrives as prose instead of a diff is a right-size nobody applies, so write the file. Head each file with a comment naming the target object and the apply command. Everything else is `kind: gcloud` (a command printed for a human) or `kind: manual`, and those **must omit `remediation.path` entirely** — the helper rejects a path on a non-manifest remediation. **No manifest may delete a PV, PVC, namespace, disk, snapshot, or address.**

**A manifest is the only kind that can become a pull request.** A `critical` manifest finding opens a narrow remediation PR automatically, branched from `main` and linked to the ledger with `Part of #<issue>` — capped at five per run, with the withheld ones named in the ledger as awaiting `/remediate`, and only where the branch carries no **live** pull request (one the harness closed itself is labelled `audit:stale-closed` and may be promoted again; one a human closed and one that merged stay closed). That combination should never arise here: the §3 severity ceiling keeps both manifest checks at `major` or below. Every other manifest finding waits for a repo writer to comment `/remediate <finding-id>` or `/remediate all` on the ledger. Findings whose `remediation.path` values overlap are promoted together in one PR, so keep one file per finding unless two findings genuinely patch the same object. A comment naming ids arrives as `pending_remediation_requests` on the next run's `start`, so you know which manifests to write while inspecting; `all` does not, because it names no particular file — it is expanded at `finish` against that run's manifest findings. `gcloud` and `manual` findings are never promotable: this audit is mostly `gcloud`, so most of what it produces stays prose in the ledger issue and never becomes a PR. That is correct — releasing an IP or deleting a disk is a command a human runs after checking, not a file to merge.

A `kind: gcloud` `note` is rendered into the ledger issue **inside a bash fence**, so write it as a shell-pasteable block: the ordered commands on their own lines, and every caution — verify-before-deleting, snapshot-first, the irreversibility warnings this SOP requires — as a `#` comment line above the command it guards. Prose in a `gcloud` note renders as broken shell. A `kind: manual` note is rendered as prose and should be written as prose.

### 5. Emit findings.json

Write the file at the `findings_path` returned by `start`, matching the harness schema exactly.

- `evidence.command` is **mandatory and must be the literal command you ran** — the same string, with the real cluster, project, and object names substituted, so a reviewer can paste it and see what you saw. **A finding you cannot reproduce is dropped, not softened.** Never reconstruct a plausible command after the fact.
- `evidence.excerpt` is trimmed real output from the Step 2 dumps — never a paraphrase, never invented JSON. The helper clips at 40 lines / 2,000 chars, so trim to the lines that prove the finding yourself rather than pasting a whole dump and letting it be cut mid-object.
- **Credential hygiene:** never paste a Secret's `data:` block, a token, a kubeconfig, or a private key into `evidence.excerpt`. If the output that proves the finding carries one, re-run the read with a field selector or an `-o jsonpath` that omits it and quote that instead. The harness redacts high-confidence credential shapes as a backstop, not as the primary control — the control is not reading the value in the first place.
- Every dollar figure, savings estimate, and percentage-saved claim is forbidden. Resource units only.
- **Noise control:** if one check produces more than 20 findings of the same kind in one cluster or project, collapse them into a single roll-up finding listing the objects in `evidence.excerpt`, and raise its severity one level. Give the roll-up the scope it covers as its `object` — `Cluster/<name>` for a whole cluster, `Namespace/<name>` for one namespace — so its identity is that scope rather than any one workload in it; the individual findings it replaces are not also emitted, so nothing collides. This bounds the fleet-wide noise and it keeps the ledger readable: the harness caps the rendered issue body at 60,000 characters and says so in the body when it has to drop findings, but the counts in the title stay the true totals either way.
- Re-verify the scope before publishing: `scope.clusters` is non-empty; every entry's `checks_run` names the §3 checks that actually ran there, and nothing more, each with the command that ran it; every cluster you could not read is in `scope.skipped` with a specific reason — "unavailable" is not a reason, the stderr excerpt is; every check that did not run on a cluster you _could_ read is named in that cluster's `limitations`; no cluster appears in both lists; no finding names a cluster from `scope.skipped`. The validator rejects the document on any of those.
- Re-read your `checks_run` lists one last time. Padding one to ten because the SOP lists ten checks is the one entry in this document that converts a partial audit back into a false all-clear: the harness has no way to see the check you did not run, so it believes you. The `command` on each entry is what makes that padding falsifiable rather than free — it is published verbatim, so an invented command is a false statement in a public issue. `checks_not_applicable` is the same lie wearing a different field: it removes checks from the denominator, so a slug parked there because you ran out of turns is a coverage gap the ledger will never show. It is published too — every exclusion and its reason render under _Not applicable_, where a reviewer who knows the cluster can call it. An honest eight-of-ten costs you nothing but an open ledger, and an honest eight-of-eight on a cluster where the other two cannot apply closes it.

**Every finding carries a `recommendation`.** Three sub-fields, all required, all non-empty, on **every** finding — not only the two checks that can produce a manifest. Write it while the evidence is in front of you; deferring it until someone asks for the fix means writing it from memory.

- `action` — what to do, imperative, one or two sentences.
- `rationale` — why this fix and not the obvious alternative. Name the alternative you considered and say why you rejected it.
- `risk` — what breaks on apply, and the read-only check to run first. **On this audit the `risk` field is what stops a "waste" finding from becoming an outage.** Every claim here rests on a ten-minute Monday-morning window; the risk line is where you say what that window cannot see.

Worked example, for a 3.7 finding on an idle batch pool — a `gcloud` remediation that will never become a pull request, and still carries the full recommendation:

```json
"recommendation": {
  "action": "Scale the batch node pool to zero, or delete it if the batch workload has moved.",
  "rationale": "Scaling to zero is reversible and keeps the pool's config; deleting is cheaper to reason about but discards the taint/label set the batch jobs depend on, which nobody has confirmed is obsolete.",
  "risk": "If a CronJob targets this pool with a nodeSelector it will go Pending on the next fire. Check for selectors referencing the pool before scaling."
}
```

### 6. Close the audit run

Run `./skills/fleet-audit/scripts/audit_report.py finish --audit fleet-wide-cost-analysis --findings-file <findings_path>`. Exit 0 means published; exit 2 means the validator rejected the document and nothing was published — fix the document and re-run, never delete the finding that tripped it; exit 1 is fatal. The helper prints one JSON line, carrying `status`, `issue_url`, `new`, `resolved`, `prs_opened`, `prs_closed`, `partial`, `coverage_gaps`, and `silent_ok`.

`partial` and `coverage_gaps` describe how much of the fleet the run can actually speak for: `partial` is `true` if any cluster is in `scope.skipped` or any audited cluster carries a `limitations` note, and `coverage_gaps` states each gap in a sentence. This matters most on a cost audit, where the conclusion is usually drawn from an absence — an idle disk that stops appearing has not necessarily been released, it may sit in a project nobody could list this week. So over a partial run the helper reports `resolved: 0` and posts no resolved-delta, retires no remediation PR as stale, and keeps the ledger open even with an empty findings array: `status` is still `CLEAN`, but the issue stays and gains a comment naming the gaps. A check declared in `checks_not_applicable` is not a gap and does not raise the flag; it left the denominator. Coverage is the only thing the flag tracks, so `partial` is `true` if and only if `coverage_gaps` is non-empty: the §5 body budget dropping findings from the description does not set it, because the audit still saw them and says as much in the body.

**`silent_ok` decides silence. Do not re-derive it.** `finish` returns `silent_ok: true` only when this run moved nothing an operator needs to hear about: nothing new, nothing resolved, no coverage gap, no remediation PR opened or closed. Read the flag rather than reassembling that from `status`, `new`, `resolved`, and `partial` yourself — that arithmetic is where a run talks itself into silence it has not earned. Two rules, and they are the whole rule:

- On a **scheduled** run, `silent_ok: true` → the final response is **exactly** `[SILENT]`. Otherwise report, and every report carries `issue_url` in full.
- **An on-demand run is never silent.** If a person dispatched this job — from a kanban card or straight from chat — someone is waiting on the answer, and `[SILENT]` throws it away. Report the outcome and the ledger URL whatever `silent_ok` says.

What to report in each case:

- `silent_ok: true` → `[SILENT]` on a scheduled run, nothing else and no preamble. On `CLEAN` the ledger issue closed as completed and every open remediation PR for this stream closed with it; on `UPDATED` nothing moved since last week. Dispatched on demand, say which in one line and give the issue URL.
- `CLEAN` with `resolved: > 0` → the waste this audit was tracking is gone. Report the issue URL and the count that closed with it. Reclaimed capacity is the whole point of the stream and the one result worth interrupting someone for.
- `CLEAN` with `partial: true` → nothing was found, but nothing was closed either. One line giving the clean result, the `coverage_gaps`, and the issue URL.
- `OPENED`, or `UPDATED` with a non-zero `new` or `resolved` → one line: counts of new and resolved findings by severity, the ledger issue URL, and anything in `prs_opened` / `prs_closed`. The helper wrote the issue body; do not restate it, do not edit it.

## Red Lines

- **Read-only, always.** Never delete, cordon, drain, patch, scale, or apply anything. Every command in this SOP is a read verb. This is the most dangerous of the five audits to get wrong: **a false positive here can lead a human to delete a disk holding real data.**
- Never emit a manifest that deletes a PV, PVC, namespace, disk, snapshot, or address. Deletion remediations are `kind: manual` or `kind: gcloud` only, always with an explicit verify-before-deleting caution and a snapshot-first step wherever data is involved. A manifest is now one merge away from `main` — a `critical` one opens its own pull request without a human asking — so this line matters more than it did, not less.
- Never state a dollar amount, a monthly saving, a percentage saving, or a price. You have no pricing data, and a fabricated figure is worse than no figure.
- Never emit a finding without a literal, reproducible `evidence.command`. Drop it instead.
- Never infer "unused" from a single observation. Every usage-based claim rests on all three samples.
- Never flag an object in a cluster recorded in `scope.skipped` — the validator rejects the whole document for it — and never flag an object for a check you declared as not run in that cluster's `limitations`. An incomplete read is not evidence of absence.
- **Never print raw credentials.** A Secret's `data:` block, a ServiceAccount token, a kubeconfig, or a private key never appears in `evidence.excerpt` — record the object reference, or re-run the read with a field selector or an `-o jsonpath` that omits the value. The harness's redaction is a backstop, not permission.
- **Never write an unstable `object`.** The id is derived from `check`, `cluster`, `namespace`, and `object`, so an `object` that moves between runs destabilises it: a pod name carrying its ReplicaSet suffix, a generated resource name, or a roll-up named after one of the workloads it covers rather than the scope it covers (§5). Name the durable object the check judged. A finding whose identity moves is reported as fixed one week and re-reported as new the next, on a ledger people trust to tell them what is still wasted.
- Never query BigQuery, the Billing export, the Recommender API, or Prometheus; never delegate any part of this audit to a Cluster Agent or a kanban card. Collection is done inline, here.
- Never write outside the GitOps declaration of the object a remediation targets and the helper's `findings_path`. Never hand-write or edit the ledger issue body or a remediation PR body, and never call `gh issue create` or `gh pr create` — this stream has exactly one ledger issue and `finish` owns it.
