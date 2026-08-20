---
name: gke-runtime-telemetry-audit
description: Audits GKE clusters for Linux cgroup CPU throttling, kernel conntrack saturation, missing ingress preStop drain hooks, and ephemeral storage runaway.
---

# Task

Audit live GKE clusters for runtime performance bottlenecks, Linux kernel conntrack table saturation, cgroup CFS CPU throttling, and missing ingress preStop hooks, emitting findings for the `fleet-audit` reporting harness.

# Workflow

## 1. Execute Telemetry Inspection

Follow the authoritative checklist in `governance/gke_runtime_telemetry_sop.md` across target GKE clusters:
- `cfs-quota-throttling`: Flag containers with restrictive fractional CPU limits without burst support.
- `conntrack-saturation`: Flag cluster nodes configuring low `nf_conntrack_max` sysctl limits.
- `ingress-502-drain`: Flag Service-exposed workloads lacking `preStop` graceful shutdown sleep hooks.
- `ephemeral-growth-rate`: Flag workloads lacking explicit `resources.limits.ephemeral-storage`.
- `ulimit-exhaustion`: Flag high-concurrency workloads running with default low file descriptor limits.

Optional helper runner for `ingress-502-drain`:
```bash
./skills/gke-runtime-telemetry-audit/scripts/telemetry_audit.py --output /opt/data/scratch/telemetry_raw.json
```

## 2. Hand Findings to Fleet Audit

Emit findings using the `fleet-audit` harness lifecycle (`start` ... `finish`).
