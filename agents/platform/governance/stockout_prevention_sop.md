# SOP: Fleet Stockout Prevention & Capacity Audit (Daily Governance)

**Purpose:** Sweep every managed GKE cluster and GCP region for capacity stockout vulnerabilities, fragile Custom Compute Class (CCC) configurations, quota bottlenecks, and obtainability risks before workloads suffer scheduling outages. The question this audit answers for a platform admin is: _which workloads and compute classes on my fleet will fail to scale or encounter a capacity stockout during demand spikes or zonal hardware shortages?_ Output is this stream's single GitHub ledger issue, rewritten in place on every run, plus narrow remediation Pull Requests carrying generated manifests for the findings that get promoted.

**Cron:** id `stockout-prevention`, schedule `20 9 * * *` (daily 09:20 UTC). The id is a stable observability identifier and does not change.

**Data sources:** `kubectl` read verbs, `gcloud compute ...`, `gcloud container ...`, and Spot capacity advice APIs (`gcloud beta compute advice capacity`, `gcloud beta compute advice capacity-history`). **Nothing else** — no external blueprints, no manual assumptions. Every conclusion is derived from live cluster and cloud reads you performed in this run.

---

## Execution Checklist

### 0. Open the audit run

```bash
./skills/fleet-audit/scripts/audit_report.py start --audit stockout-prevention
```

Returns `{"issue": <int|null>, "repo":"org/repo", "workspace":"/opt/data/gitops/stockout-prevention/org__repo", "findings_path":"/opt/data/scratch/findings_stockout-prevention.json", "pending_remediation_requests":[…]}`. Keep `findings_path` and `workspace` from this call; you write into both.

- `workspace` is the GitOps clone `start` made for you. The audit pod does not begin life inside a checkout, so this is the only tree that exists, and every `remediation.path` in Step 4 is resolved against it — a manifest written elsewhere is one the harness cannot find.
- `issue` is this stream's open ledger issue, or `null` when it has none. Either way you never create it — `finish` owns that.
- `pending_remediation_requests` lists finding ids a repo writer asked for with a `/remediate` comment on the ledger. Write a manifest for each one while you inspect (Step 4), or the promotion fails for want of a file.
- `start` creates and resets no branch. There is no report branch.

The helper owns every `git`/`gh` operation and renders the ledger issue body and every remediation PR body — **never hand-write an issue or PR body, never run `git commit`, `git push`, `gh issue create`, `gh pr create`, or `gh issue comment` yourself.**

**Never comment on the ledger yourself.** `/remediate` is a human reviewer's instruction to this harness, not a step in the audit: an agent that posts it is authorizing its own pull request.

### 1. Enumerate the target fleet

```bash
gcloud container clusters list --format=json
```

- Target every cluster with `status == "RUNNING"`. Record `{name, location, project, checks_run}` into `scope.clusters`.
- Obtain per-cluster credentials into an isolated kubeconfig so clusters cannot bleed into each other:
  ```bash
  export KC="${HERMES_HOME:-/opt/data}/.kubeconfigs/kubeconfig_<project>_<cluster>_<location>.yaml"
  KUBECONFIG=$KC gcloud container clusters get-credentials <cluster> --location=<location> --project=<project>
  ```
- **`checks_run` is mandatory on every cluster,** and each entry is an object, never a bare string:
  ```json
  {
    "check": "ccc-missing-fallbacks",
    "command": "kubectl --context prod-usc1 get customcomputeclasses,computeclasses -A -o yaml"
  }
  ```
  `check` is the backticked slug from the §3 heading that defines it — `ccc-missing-fallbacks`, `ccc-no-ondemand-floor`, and so on. `command` is the literal invocation you issued on that cluster for that check. It must name one of `kubectl`, `gcloud`, `gsutil`, `bq`, `helm`, or `curl`; anything under eight characters is rejected.
- **A check the cluster's shape rules out is declared in `checks_not_applicable`** with a specific reason:
  ```json
  {
    "check": "single-zone-nodepool",
    "reason": "Cluster is GKE Autopilot mode; node pool management is fully delegated to GKE."
  }
  ```

### 2. Collect capacity and workload state

Collect the live cluster definitions and GCP regional capacity metrics:

```bash
# 1. Dump ComputeClasses, Workloads, and StorageClasses
KUBECONFIG=$KC kubectl get customcomputeclasses,computeclasses,deployments,statefulsets,storageclasses,nodepools -A -o json > /opt/data/scratch/stockout_state_<cluster>.json

# 2. Inspect GCP Regional Quotas for the cluster's region (e.g. us-central1)
gcloud compute regions describe <region> --format="json(quotas.filter(metric=CPUS),quotas.filter(metric=NVIDIA_L4_GPUS),quotas.filter(metric=NVIDIA_A100_GPUS))"

# 3. Check Spot Capacity & Preemption Advice
gcloud beta compute advice capacity --provisioning-model=SPOT --instance-selection-machine-types="g2-standard-4,n4-standard-4,c3-standard-4" --target-distribution-shape=ANY --size=1 --region=<region> --format="json"
```

### 3. Checks

**Standard exclusions — apply to every check below:**

- **S1 — system namespace:** `kube-system`, `kube-public`, `kube-node-lease`, `gmp-system`, `gmp-public`, `gke-gmp-system`, `cnrm-system`, `configconnector-operator-system`, `krmapihosting-system`, `istio-system`, `asm-system`, `anthos-identity-service`, `gatekeeper-system`, `composer-system`, or any namespace matching `gke-*`, `gke-managed-*`, or `config-management-*`.
- **S2 — GKE-managed object:** carries `addonmanager.kubernetes.io/mode`.
- **S3 — operator-owned:** non-empty `metadata.ownerReferences`.
- **S4 — explicit opt-out:** carries `kubeagents.x-k8s.io/stockout-audit: exempt`.
- **S5 — not running:** `spec.replicas == 0`, or completed batch Jobs.

#### 3.1 Lack of fallback machine families and zones in ComputeClasses (`ccc-missing-fallbacks`)

- **Command:** `kubectl --context <ctx> get customcomputeclasses,computeclasses -n <ns> <name> -o yaml`
- **Flag when:** A `CustomComputeClass` or `ComputeClass` has `priorities[]` pinned to a single machine family (e.g., only `c3` or only `g2`) in a single zone without alternative machine families or zones within the region.
- **Do NOT flag:** ComputeClasses with 2+ machine families (e.g., `n4`, `c3`, `n2`) or multi-zone topology rules; standard exclusions.
- **Severity:** `critical`. When GCE encounters a zonal shortage or stockout on that machine family, Cluster Autoscaler has no fallback path and scale-up fails completely.
- **Impact:** "Pinned to a single machine family and zone: any zonal capacity exhaustion or stockout causes scale-up to fail and leaves pods unschedulable."
- **Remediation:** `kind: manifest`. Add multi-zone distribution and secondary fallback machine families (e.g., fallback from `c3` to `n4` and `n2`) to the ComputeClass manifest in GitOps.

#### 3.2 Spot-only ComputeClass without on-demand safety floor (`ccc-no-ondemand-floor`)

- **Command:** `kubectl --context <ctx> get customcomputeclasses,computeclasses -n <ns> <name> -o yaml`
- **Flag when:** A ComputeClass `priorities[]` array contains only Spot instances (`spot: true` or `provisioningModel: SPOT`) with no On-Demand priority rule at the end.
- **Do NOT flag:** ComputeClasses that contain an On-Demand fallback priority at the bottom of `priorities[]`; workloads with explicit non-production/test opt-out.
- **Severity:** `major`.
- **Impact:** "If Spot VM capacity is preempted or exhausted in the region, the workload has no on-demand floor and remains permanently in Pending state."
- **Remediation:** `kind: manifest`. Append an On-Demand priority rule at the lowest priority in the ComputeClass manifest to act as a guaranteed capacity floor.

#### 3.3 Large VM shape scarcity (>32 vCPU) without multi-family fallbacks (`ccc-large-vm-scarcity`)

- **Command:** `kubectl --context <ctx> get deployments,statefulsets,customcomputeclasses -n <ns> <name> -o yaml`
- **Flag when:** A workload or ComputeClass requests very large VM sizes (>32 vCPUs, such as `m1-ultramem-160`, `c3-highcpu-88`, `a2-highgpu-8g`) from thin capacity pools without secondary fallback families or horizontal replica spreading.
- **Do NOT flag:** Workloads requesting standard/horizontal shapes (<=32 vCPUs); stateful monolithic databases that explicitly declare multi-region failover.
- **Severity:** `major`.
- **Impact:** "Very large VM shapes (>32 cores) draw from thin regional capacity pools and are highly prone to sudden stockouts during scale-up."
- **Remediation:** `kind: manifest`. If horizontally scalable, propose smaller replica shapes with horizontal autoscaling; otherwise add fallback machine families in GitOps manifests.

#### 3.4 Excessive granular machine types causing priority starvation (`ccc-priority-starvation`)

- **Command:** `kubectl --context <ctx> get customcomputeclasses,computeclasses -n <ns> <name> -o yaml`
- **Flag when:** A `CustomComputeClass` contains more than 10 granular `machineType` priority rules (e.g., 20+ individual sizes like `n2-standard-4`, `n2-standard-8`, etc.), exceeding Flex Advisor combinations and triggering Cluster Autoscaler backoff reset loops.
- **Do NOT flag:** ComputeClasses using <= 5 broad `machineFamily` level definitions (e.g. `n4`, `c3`, `n2`).
- **Severity:** `critical`.
- **Impact:** "Excessive granular machineType rules generate >200 solver combinations, exceeding the Flex Advisor cache limit and triggering autoscaler backoff loops that starve lower priorities."
- **Remediation:** `kind: manifest`. Auto-compress the ComputeClass: replace all granular `machineType` rules with 3-4 family-level (`machineFamily`) priority rules.

#### 3.5 Mixed disk generations on PV-attached ComputeClasses (`ccc-mixed-disk-generations`)

- **Command:** `kubectl --context <ctx> get customcomputeclasses,computeclasses,statefulsets -n <ns> <name> -o yaml`
- **Flag when:** A stateful workload using PersistentVolumes references a ComputeClass whose `priorities[]` mixes Gen 2 VMs (`n2`, `n2d`, `c2`) and Gen 4/Hyperdisk VMs (`c4`, `n4`, `c3`), causing PV attachment deadlocks upon failover.
- **Do NOT flag:** Stateless workloads; ComputeClasses whose priorities are purely Gen 2 or purely Gen 4/Hyperdisk-compatible.
- **Severity:** `critical`.
- **Impact:** "Stateful PV workload mixes Gen 2 and Gen 4 machine families, causing volume attachment failures and deadlocks when scaling across nodes."
- **Remediation:** `kind: manifest`. Unify ComputeClass priorities to stick strictly to compatible disk generation machine families.

#### 3.6 Incompatible machine families for Hyperdisk workloads (`ccc-hyperdisk-incompatible`)

- **Command:** `kubectl --context <ctx> get storageclasses,customcomputeclasses,deployments -n <ns> -o yaml`
- **Flag when:** A workload using Hyperdisk storage (`hyperdisk-balanced`, `hyperdisk-throughput`, `hyperdisk-extreme`) uses a ComputeClass that falls back to older generation machine families (`c2`, `n2`, `e2`) that do not support Hyperdisk CSI drivers.
- **Do NOT flag:** Workloads using standard Persistent Disk (`pd-standard`, `pd-ssd`); ComputeClasses falling back only to Hyperdisk-capable families (`c3`, `c4`, `n4`, `c3d`).
- **Severity:** `critical`.
- **Impact:** "Autoscaler fallback lands on an older machine family (c2/n2/e2) that does not support Hyperdisk, causing node provisioning or pod volume attachment to fail."
- **Remediation:** `kind: manifest`. Update ComputeClass fallback priorities to Hyperdisk-compatible families (`c3`, `c4`, `n4`) and remove incompatible older generations.

#### 3.7 Regional quota exhaustion risk for specialized hardware (`quota-exhaustion-risk`)

- **Command:** `gcloud compute regions describe <region> --format="json(quotas)"`
- **Flag when:** Total requested GPU/TPU/CPU limits across workloads in a region exceed or reach >=90% of the project's regional quota limit (e.g. demanding 32 L4 GPUs when quota limit is 24).
- **Do NOT flag:** Projects where regional quota limit exceeds total workload demand with >= 25% headroom.
- **Severity:** `critical`.
- **Impact:** "Workload resource requests exceed regional GCP quota limits; Cluster Autoscaler cannot provision additional nodes even if physical capacity exists."
- **Remediation:** `kind: manifest`. Adjust workload request caps in GitOps manifests to fit strictly within quota limits, and submit a quota increase recommendation for the GCP project.

#### 3.8 High preemption risk or low obtainability on Spot instances (`spot-scarcity-risk`)

- **Command:** `gcloud beta compute advice capacity --provisioning-model=SPOT --region=<region> --format=json`
- **Flag when:** Workloads or ComputeClasses request Spot VM shapes that have high historical preemption rates (>20%) or low obtainability scores in `compute advice`, without alternative family fallbacks.
- **Do NOT flag:** Spot configurations that have high obtainability scores or comprehensive multi-family fallbacks; non-production environments.
- **Severity:** `major`.
- **Impact:** "Spot machine shapes have high historical preemption rates and severe obtainability constraints, putting workload uptime at extreme risk."
- **Remediation:** `kind: manifest`. Expand instance selection to include lower-preemption machine types and add secondary on-demand fallback priorities in GitOps.

#### 3.9 Single-zone node pools on Standard clusters (`single-zone-nodepool`)

- **Command:** `gcloud container node-pools list --cluster=<cluster> --location=<location> --format=json`
- **Flag when:** A Standard mode GKE cluster has autoscaling node pools restricted to a single zone with no Node Auto-Provisioning (NAP) or regional multi-zone node pools configured.
- **Do NOT flag:** Autopilot clusters (fully managed multi-zone); regional clusters with multi-zone node pools.
- **Severity:** `major`.
- **Impact:** "Node pool is locked to a single zone: any zonal stockout in that zone halts all cluster auto-scaling."
- **Remediation:** `kind: manifest`. Propose enabling multi-zone node pools or configuring Node Auto-Provisioning (NAP) in Terraform/Kustomize declarations.

#### 3.10 Rigid single-zone or hostname pinning on critical workloads (`rigid-scheduling-pin`)

- **Command:** `kubectl --context <ctx> get deployments,statefulsets -n <ns> <name> -o yaml`
- **Flag when:** Workload `spec.template.spec.nodeSelector` or `nodeAffinity` pins `topology.kubernetes.io/zone` to a single zone or `kubernetes.io/hostname` to a specific node, without using ComputeClasses or multi-zone spread constraints.
- **Do NOT flag:** StatefulSets with zonal storage volume claims (correctly zone-bound); soft `preferredDuringSchedulingIgnoredDuringExecution`.
- **Severity:** `major` (hostname pin is `critical`).
- **Impact:** "Workload is hard-pinned to a single zone or host: capacity exhaustion in that zone prevents all replica scheduling."
- **Remediation:** `kind: manifest`. Replace hard nodeSelectors with a multi-zone ComputeClass and TopologySpreadConstraints.

### 4. Generate remediation artifacts

- Locate the existing declaration in the GitOps clone (`grep -rl "name: <object>" --include='*.yaml' <workspace>`).
- Edit the manifest directly in `<workspace>`, adding the necessary fallback machine families, zones, or quota adjustments.
- **Mandatory Remediation Comments**: For every modified line in YAML, append an inline `# Remediation: <reason>` comment.
- Set `remediation.path` to the repo-relative file path, with `kind: manifest`.
- Reviewers may comment `/remediate <finding-id>` or `/remediate all` on the ledger issue to promote findings into PRs.

### 5. Emit findings.json

Write the schema exactly as the helper validates it to the `findings_path` returned in Step 0: `audit` set to `stockout-prevention`; `scope.clusters` non-empty, each entry carrying the mandatory `checks_run` list of `{check, command}` objects for the §3 checks that actually ran there; and for each finding, `check`, `severity`, `title`, `cluster`, `namespace`, `object`, `evidence.command`, `evidence.excerpt`, `impact`, `recommendation`, and `remediation`.

### 6. Close the audit run

```bash
./skills/fleet-audit/scripts/audit_report.py finish --audit stockout-prevention \
  --findings-file /opt/data/scratch/findings_stockout-prevention.json
```

One JSON line comes back, carrying `status`, `issue_url`, `new`, `resolved`, `prs_opened`, `prs_closed`, `partial`, `coverage_gaps`, and `silent_ok`. Exit 2 means the validator rejected the document and nothing was published — fix the document, do not retry blind. Exit 1 is fatal. Exit 0 means it published.

`partial` is `true` when the run could not read the whole fleet: any cluster in `scope.skipped`, or any cluster kept in scope with a `limitations` note. `coverage_gaps` names each one in a sentence. The harness then refuses to draw conclusions from silence, because a workload or ComputeClass you never queried is not one that got resolved: `resolved` comes back `0` and no resolved-delta is posted, no remediation PR is retired as stale, and the ledger issue stays open even at zero findings — `status` is still `CLEAN`, but the issue survives with a comment naming what went unread. A check declared in `checks_not_applicable` is not a gap and does not raise the flag; it left the denominator. Nothing else raises it — it is `true` if and only if `coverage_gaps` is non-empty. A fleet big enough that the description had to drop findings is not a coverage gap: those workloads were queried, the title counts them, and the body says which ones it left out.

**`silent_ok` decides silence. Do not re-derive it.** `finish` returns `silent_ok: true` only when this run moved nothing an operator needs to hear about: nothing new, nothing resolved, no coverage gap, no remediation PR opened or closed. Read the flag rather than reassembling that from `status`, `new`, `resolved`, and `partial` yourself — that arithmetic is where a run talks itself into silence it has not earned. Two rules, and they are the whole rule:

- On a **scheduled** run, `silent_ok: true` → your entire final response is exactly `[SILENT]`. Otherwise report, and every report carries `issue_url` in full.
- **An on-demand run is never silent.** If a person dispatched this job — from a kanban card or straight from chat — someone is waiting on the answer, and `[SILENT]` throws it away. Report the outcome and the ledger URL whatever `silent_ok` says.

What to report in each case:

- `silent_ok: true` — `[SILENT]` on a scheduled run, nothing else and no preamble. On `CLEAN` the ledger issue closed as completed and every open remediation PR for this stream closed with it; on `UPDATED` the ledger was rewritten but nothing moved. Dispatched on demand, say which in one line and give the issue URL.
- `status: "CLEAN"` with `resolved: > 0` — every capacity gap this ledger tracked has been closed. Report the issue URL and the count.
- `status: "CLEAN"` with `partial: true` — nothing reproduced, but the ledger and its PRs stayed open because the coverage was incomplete. One line, the clean result plus the `coverage_gaps`, then stop.
- Any other outcome — reply with **one line**: counts by severity, new vs. resolved, skipped-cluster count if any, remediation PRs opened or closed, and the `issue_url`.

## Red Lines

- **Read-only against every cluster.** No `apply`, `patch`, `edit`, `delete`, `scale`, `drain`, `cordon`, or eviction.
- **No hand-written issue or PR bodies, and no direct git/gh calls.** `audit_report.py` owns the ledger issue, the remediation branches, the commits, and every body it renders.
- **No credentials in evidence.** A Secret's `data:` block, a token, or a private key never enters an excerpt; re-read with a projection that omits it.
- **A finding you cannot reproduce is dropped, not softened.** `evidence.command` is the literal command you executed; if the confirm read fails or the condition has cleared, the finding does not ship.
- **No fabricated numbers.** Resource quantities and machine families are either read off the live object or left to a human.
- **Stable ids or the delta lies.** An unstable id — one that varies between runs because the `object` it is derived from moved — turns one persistent problem into an infinite stream of "new" findings.
