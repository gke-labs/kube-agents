---
name: gke-ai-hpc-orchestration-audit
description: Audits GKE Dynamic Workload Scheduler (DWS) GPU queues, Kueue multi-tenancy, NCCL interconnect drops, and CUDA memory settings.
---

# Task

Audit GKE AI/ML accelerator node pools, Dynamic Workload Scheduler (DWS) GPU queues, Kueue multi-tenant cohort budgets, NCCL interconnect health, and CUDA memory settings, emitting findings for the `fleet-audit` reporting harness.

# Workflow

## 1. Execute AI/HPC Workload Inspection

Run the profile-relative AI/HPC runner to sweep target accelerator clusters:

```bash
./skills/gke-ai-hpc-orchestration-audit/scripts/ai_hpc_audit.py --output /opt/data/scratch/ai_hpc_raw.json
```

## 2. Evaluate Findings Against SOP Checks

Filter and categorize collected metrics according to `governance/gke_ai_hpc_orchestration_sop.md`:
- `dws-queue-timeout`: Flag DWS batch jobs with < 300s timeout.
- `kueue-cohort-starvation`: Flag Kueue ClusterQueues lacking borrowing/lending limits.
- `nccl-interconnect-drops`: Flag distributed training jobs lacking RoCE / GPUDirect environment tuning.
- `cuda-memory-fragmentation`: Flag PyTorch workloads lacking `expandable_segments` memory allocation flags.
- `tpu-slice-resilience`: Flag Cloud TPU workloads lacking ICI auto-restart annotations.

## 3. Hand Findings to Fleet Audit

Emit findings using the `fleet-audit` harness lifecycle (`start` ... `finish`).
