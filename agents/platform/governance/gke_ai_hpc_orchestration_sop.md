# SOP: GKE AI/ML & HPC Orchestration Audit (Daily Governance)

**Purpose:** Sweep all managed GKE GPU/TPU clusters for Dynamic Workload Scheduler (DWS) capacity queue timeouts, Kueue multi-tenant quota starvation, NCCL all-reduce interconnect packet drops, and CUDA memory fragmentation. The question this audit answers for a platform admin is: _which distributed training jobs will abort during capacity queuing, which teams are starved of accelerator quota, and where is GPU memory fragmentation causing spurious CUDA OOMs?_ Output is this stream's single GitHub ledger issue, rewritten in place on every run, plus narrow remediation Pull Requests carrying manifest fixes for the findings that get promoted.

**Cron:** id `gke-ai-hpc-orchestration-audit`, schedule `15 8 * * *` (daily 08:15 UTC).

**Data sources:** GKE DWS event logs, Kueue `ClusterQueue` status conditions, DCGM / NVML GPU metrics, and JobSet failure conditions.

---

## Execution Checklist

### 0. Open the audit run

```bash
./skills/fleet-audit/scripts/audit_report.py start --audit gke-ai-hpc-orchestration-audit
```

Returns `{"issue": <int|null>, "repo":"org/repo", "workspace":"...", "findings_path":"/opt/data/scratch/findings_gke-ai-hpc-orchestration-audit.json"}`.

### 1. Enumerate the target fleet

```bash
kubectl get computeclasses,clusterqueues,jobsets -A -o json
```

Record all accelerator clusters into `scope.clusters` with mandatory `checks_run` objects.

### 2. Execute AI/HPC workload inspection

```bash
./skills/gke-ai-hpc-orchestration-audit/scripts/ai_hpc_audit.py --output /opt/data/scratch/ai_hpc_raw.json
```

---

## Section 3: Diagnostic Checks Roster

#### `dws-queue-timeout`

- **Severity**: `critical`
- **Command**: `kubectl --context=$CLUSTER get jobs,jobsets -A -o json`
- **Condition**: GKE batch `Job` or `JobSet` using Dynamic Workload Scheduler specifies `dws.gke.io/wait-timeout < 300s`, causing premature job aborts during GPU block assembly.
- **Remediation**: Update `dws.gke.io/wait-timeout` to `>= 1800s` in batch manifest.

#### `kueue-cohort-starvation`

- **Severity**: `major`
- **Command**: `kubectl --context=$CLUSTER get clusterqueues,resourceflavors -o json`
- **Condition**: Kueue `ClusterQueue` quota configuration permits single tenant cohort to consume 100% of cluster GPU capacity without preemption borrowing limits.
- **Remediation**: Configure `borrowingLimit` and `lendingLimit` on Kueue `ClusterQueue` resources in manifest.

#### `nccl-interconnect-drops`

- **Severity**: `major`
- **Command**: `kubectl --context=$CLUSTER get daemonsets -n kube-system -l k8s-app=nvidia-driver-installer -o json`
- **Condition**: Distributed multi-node GPU training jobs lack RoCE / GPUDirect tuning parameters, causing all-reduce gradient synchronization stalls.
- **Remediation**: Inject `NCCL_DEBUG=INFO`, `NCCL_BUFFSIZE`, and `NCCL_CROSS_NIC` into training container environment.

#### `cuda-memory-fragmentation`

- **Severity**: `major`
- **Command**: `kubectl --context=$CLUSTER get deployments,statefulsets -A -o json`
- **Condition**: Deep learning workloads suffer CUDA out-of-memory errors despite > 25% GPU VRAM showing available in metrics.
- **Remediation**: Set `PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True` in workload manifest.

#### `tpu-slice-resilience`

- **Severity**: `major`
- **Command**: `kubectl --context=$CLUSTER get pods -l cloud.google.com/gke-tpu-topology -o json`
- **Condition**: Multi-host Cloud TPU workloads lack automatic restart and failure recovery annotations for Inter-Chip Interconnect (ICI) link drops.
- **Remediation**: Inject TPU resilience annotations and JobSet restart policies in manifest.

---

### 4. Emit findings.json

Write the schema exactly as validated by the helper to `findings_path`:

```json
{
  "audit": "gke-ai-hpc-orchestration-audit",
  "scope": {
    "clusters": [
      {
        "name": "gke-ai-cluster",
        "location": "us-central1",
        "project": "proj-1",
        "checks_run": [
          {
            "check": "dws-queue-timeout",
            "command": "kubectl --context=gke-ai-cluster get jobs,jobsets -A -o json"
          }
        ]
      }
    ],
    "skipped": []
  },
  "findings": []
}
```

### 5. Close the audit run

```bash
./skills/fleet-audit/scripts/audit_report.py finish --audit gke-ai-hpc-orchestration-audit \
  --findings-file /opt/data/scratch/findings_gke-ai-hpc-orchestration-audit.json
```

---

## Red Lines

- **Never abort or delete running GPU/TPU training jobs directly.**
- **Never emit a manifest that deletes a JobSet, RayCluster, or Kueue ClusterQueue.**
- **Never export training data excerpts, model weights, or private S3/GCS credentials.**
