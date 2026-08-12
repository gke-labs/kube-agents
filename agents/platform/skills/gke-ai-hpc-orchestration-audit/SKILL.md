---
name: gke-ai-hpc-orchestration-audit
description: Audits GKE Dynamic Workload Scheduler (DWS) GPU queues, Kueue multi-tenancy, NCCL interconnect drops, and CUDA memory settings.
---

# Task

Audit GKE AI/ML accelerator node pools, Dynamic Workload Scheduler (DWS) GPU queues, Kueue multi-tenant cohort budgets, NCCL interconnect health, and CUDA memory settings, emitting findings for the `fleet-audit` reporting harness.

# Workflow

## 1. Execute AI/HPC Workload Inspection

Follow the authoritative checklist in `governance/gke_ai_hpc_orchestration_sop.md` across target accelerator clusters:
- `dws-queue-timeout`: Flag workloads where DWS flex-start queue timeout annotation (`cloud.google.com/gke-dws-queue-timeout-seconds`) exceeds 86400s / 24h.
- `kueue-cohort-starvation`: Flag Kueue ClusterQueues lacking borrowing limits in shared cohorts.
- `nccl-interconnect-drops`: Flag multi-node GPU workloads lacking `nccl-fastsocket-installer` DaemonSet or GPUDirect tuning.
- `cuda-memory-fragmentation`: Flag dedicated GPU workloads lacking MPS or time-slicing configuration.
- `tpu-slice-resilience`: Flag multi-slice TPU node pools lacking autoRepair or resilient sub-slice recovery.

Optional helper runner:
```bash
./skills/gke-ai-hpc-orchestration-audit/scripts/ai_hpc_audit.py --output /opt/data/scratch/ai_hpc_raw.json
```

## 2. Hand Findings to Fleet Audit

Emit findings using the `fleet-audit` harness lifecycle (`start` ... `finish`).
