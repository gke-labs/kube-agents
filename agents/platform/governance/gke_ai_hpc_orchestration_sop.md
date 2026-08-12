# SOP: GKE AI/ML and HPC Workload Orchestration Audit (Daily Governance)

**Purpose:** Sweep all managed GKE accelerator clusters for Dynamic Workload Scheduler (DWS) flex-start provisioning timeouts, Kueue cohort starvation, NCCL cross-node interconnect packet drops, CUDA memory fragmentation, and TPU multi-slice topology misconfigurations. The question this audit answers for a platform admin is: _which distributed training jobs are failing DWS provisioning deadlines, where is Kueue queue borrowing causing workload starvation, and which GPU/TPU nodes have interconnect drops causing training step degradation?_ Output is this stream's single GitHub ledger issue, rewritten in place on every run, plus narrow remediation Pull Requests carrying manifest fixes for the findings that get promoted.

**Cron:** id `gke-ai-hpc-orchestration-audit`, schedule `15 8 * * *` (daily 08:15 UTC).

**Data sources:** `kubectl` read verbs and `gcloud container clusters ...` across all target accelerator clusters (`GCP_PROJECT_ID` and `MONITORED_PROJECT_IDS`).

---

## Execution Checklist

### 0. Open the audit run

```bash
./skills/fleet-audit/scripts/audit_report.py start --audit gke-ai-hpc-orchestration-audit
```

Returns `{"issue": <int|null>, "repo":"org/repo", "workspace":"/opt/data/gitops/gke-ai-hpc-orchestration-audit/org__repo", "findings_path":"/opt/data/scratch/findings_gke-ai-hpc-orchestration-audit.json", "pending_remediation_requests": [...]}`. If `pending_remediation_requests` is non-empty, write a `kind: manifest` file for every requested finding during §2 and §3, whether or not this SOP would have promoted it on its own.

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
- **`checks_run` is mandatory on every cluster:** Each entry is an object `{"check": "<slug>", "command": "<literal command>"}` naming the exact inspection command executed on that cluster.
- A cluster you cannot reach goes in `scope.skipped` with a reason string. If a cluster is partially readable, record the refusal in its `limitations` string. Declare structurally inapplicable checks in `checks_not_applicable`.

### 2. Diagnostic checks roster

- **S1 — system namespace:** `kube-system`, `kube-public`, `kube-node-lease`, `gmp-system`, `gmp-public`, `gke-gmp-system`, `cnrm-system`, `configconnector-operator-system`, `krmapihosting-system`, `istio-system`, `asm-system`, `anthos-identity-service`, `gatekeeper-system`, `composer-system`, or any namespace matching `gke-*`, `gke-managed-*`, or `config-management-*`.
- **S2 — managed addon:** Exclude resources annotated with `addonmanager.kubernetes.io/mode`.
- **S3 — controller-managed:** Exclude child Pods carrying `ownerReferences`. Target parent Deployments, StatefulSets, JobSets, or RayClusters.
- **S4 — scaled-to-zero:** Exclude workloads where `spec.replicas == 0`.
- **Cluster-scoped objects:** `namespace` is `""` for cluster-scoped findings (`ComputeClass`, `ClusterQueue`, `ResourceFlavor`).

#### 2.1 Dynamic Workload Scheduler (DWS) flex-start queue timeouts (`dws-queue-timeout`)

- **Severity**: `critical`
- **Command**: `kubectl --context=$CLUSTER get computeclasses,clusterqueues,jobsets -A -o json`
- **Condition**: Workload DWS flex-start queued provisioning queue timeout exceeds maximum allowed duration (workload annotation `cloud.google.com/gke-dws-queue-timeout-seconds` > 86400 / 24h) or pending workloads encounter admission deadline timeouts.
- **Do NOT flag**: Non-DWS batch jobs or workloads with standard provisioning models.
- **Remediation**: (kind: manifest) Adjust workload annotation `cloud.google.com/gke-dws-queue-timeout-seconds` or configure multi-zone fallback capacity in workload spec.

#### 2.2 Kueue cluster queue borrowing and cohort starvation (`kueue-cohort-starvation`)

- **Severity**: `major`
- **Command**: `kubectl --context=$CLUSTER get clusterqueues,resourceflavors -o json`
- **Condition**: High-priority training queue is starved of quota due to unbounded cohort borrowing limits in shared Kueue cohorts.
- **Do NOT flag**: Standalone ClusterQueues without shared cohorts or development queues with explicit borrowing caps.
- **Remediation**: (kind: manifest) Configure explicit `cohort` borrowing limits and nominal quota ceilings in ClusterQueue manifests.

#### 2.3 NCCL cross-node interconnect packet drops (`nccl-interconnect-drops`)

- **Severity**: `major`
- **Command**: `kubectl --context=$CLUSTER get daemonsets -n kube-system -l k8s-app=nccl-fastsocket-installer -o json`
- **Condition**: Multi-node GPU training workloads lack FastSocket / NCCL GPUDirect optimization DaemonSets on GPU accelerator clusters.
- **Do NOT flag**: Single-node GPU inference workloads or clusters without GPU nodes.
- **Remediation**: (kind: manifest) Enable `--enable-fast-socket` on GPU node pools or deploy `nccl-fastsocket-installer` DaemonSet and configure `NCCL_CROSS_NIC=1` in workload environment.

#### 2.4 GPU container unallocated CUDA memory fragmentation (`cuda-memory-fragmentation`)

- **Severity**: `minor`
- **Command**: `kubectl --context=$CLUSTER get pods,deployments -A -o json`
- **Condition**: Workload requests whole GPU resources (`nvidia.com/gpu`) without MPS (Multi-Process Service) or time-slicing configuration (`cloud.google.com/gke-gpu-sharing-strategy`).
- **Do NOT flag**: Dedicated multi-node distributed training jobs saturating GPU VRAM or workloads with explicit GPU sharing/time-slicing configured.
- **Remediation**: (kind: gcloud) Configure GPU time-slicing or sharing in GKE NodePool GPU driver configuration.

#### 2.5 TPU multi-slice topology and health check resilience (`tpu-slice-resilience`)

- **Severity**: `minor`
- **Command**: `gcloud container node-pools list --cluster=$CLUSTER --location=$LOCATION --project=$PROJECT --format=json`
- **Condition**: Multi-slice TPU v4/v5e node pool lacks automated node repair or resilient sub-topology fault tolerance settings.
- **Do NOT flag**: Single-host TPU v4-8 / v5e-1x1 development instances.
- **Remediation**: (kind: gcloud) Enable auto-repair and resilient sub-slice recovery in TPU NodePool configuration.

### 3. Generate remediation artifacts

For promoted findings requiring `kind: manifest` remediation, write the updated manifest to `remediation.path` resolved within the `workspace` GitOps repository:

- Discover the target manifest file from existing repository paths (e.g., `clusters/<cluster>/kueue/clusterqueue.yaml`).
- Never invent phantom paths or write manifests to directories outside the reconciled GitOps hierarchy.

### 4. Emit findings.json

Write the whole document to `findings_path` in one shot, with `audit: "gke-ai-hpc-orchestration-audit"`, `scope.clusters` listing every cluster you queried — each carrying the `checks_run` list §1 required and, where §1 recorded them, that cluster's `checks_not_applicable` entries and `limitations` string — and `scope.skipped` listing only the targets you could not read.

`command` in `checks_run` is the literal inspection command executed, and anything under eight characters is rejected.

Every finding must conform to the full findings schema:

```json
{
  "audit": "gke-ai-hpc-orchestration-audit",
  "scope": {
    "clusters": [
      {
        "name": "ai-cluster-1",
        "location": "us-central1-a",
        "project": "proj-1",
        "checks_run": [
          {
            "check": "dws-queue-timeout",
            "command": "kubectl --context=ai-cluster-1 get computeclasses,clusterqueues -A -o json"
          }
        ]
      }
    ],
    "skipped": []
  },
  "findings": [
    {
      "check": "dws-queue-timeout",
      "severity": "critical",
      "title": "Workload dws-training-run DWS queue timeout exceeds SLA",
      "cluster": "ai-cluster-1",
      "namespace": "ml-platform",
      "object": "JobSet/dws-training-run",
      "impact": "Queued training workloads time out waiting for DWS dynamic reservation windows.",
      "evidence": {
        "command": "kubectl --context=ai-cluster-1 get jobset dws-training-run -n ml-platform -o json",
        "excerpt": "cloud.google.com/gke-dws-queue-timeout-seconds: \"172800\""
      },
      "recommendation": {
        "action": "Adjust cloud.google.com/gke-dws-queue-timeout-seconds annotation to 86400s or lower.",
        "rationale": "Prevents stalled job queueing and aligns with the platform's 24h queue timeout limit.",
        "risk": "Reduces max queue wait time."
      },
      "remediation": {
        "kind": "manifest",
        "path": "clusters/ai-cluster-1/ml-platform/jobset.yaml"
      }
    }
  ]
}
```

### 5. Close the audit run

```bash
./skills/fleet-audit/scripts/audit_report.py finish --audit gke-ai-hpc-orchestration-audit   --findings-file /opt/data/scratch/findings_gke-ai-hpc-orchestration-audit.json
# -> {"status":"CLEAN"|"OPENED"|"UPDATED","issue_url":...,"new":n,"resolved":m,
#     "prs_opened":[...],"prs_closed":[...],"partial":false,"coverage_gaps":[],
#     "silent_ok":true}
```

- On a **scheduled** run, `silent_ok: true` -> your final response is exactly `[SILENT]`.
- **An on-demand run is never silent.** If a person dispatched this job, report the outcome and the ledger URL whatever `silent_ok` says.
- Repo writers can trigger remediation by commenting `/remediate <finding-id>` or `/remediate all` on the ledger issue.

---

## Red Lines

- **Read-only audit.** Never terminate running training jobs, abort TPU slice reservations, or mutate cluster queues directly.
- **No hand-written issues or PRs.** `audit_report.py` owns the entire git/GitHub write path.
- **Never print raw credentials.** Secret tokens, certificates, and private keys must never reach an excerpt.
- **No unstable finding identity.** Name the durable resource identifier (`ComputeClass/<name>`, `ClusterQueue/<name>`), never an ephemeral job pod name.
- **Never emit a manifest that directly deletes a workload or queue.** Deletion remediations are `kind: manual` or `kind: gcloud` only.
