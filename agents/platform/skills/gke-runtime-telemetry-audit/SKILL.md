---
name: gke-runtime-telemetry-audit
description: Audits GKE clusters for Linux cgroup CPU throttling, kernel conntrack saturation, missing ingress preStop drain hooks, and ephemeral storage runaway.
---

# Task

Audit live GKE clusters for runtime performance bottlenecks, Linux kernel conntrack table saturation, cgroup CFS CPU throttling, and missing ingress preStop hooks, emitting findings for the `fleet-audit` reporting harness.

# Workflow

## 1. Execute Telemetry Inspection

Run the profile-relative telemetry runner to sweep target GKE clusters:

```bash
./skills/gke-runtime-telemetry-audit/scripts/telemetry_audit.py --output /opt/data/scratch/telemetry_raw.json
```

## 2. Evaluate Findings Against SOP Checks

Filter and categorize collected metrics according to `governance/gke_runtime_telemetry_sop.md`:
- `cfs-quota-throttling`: Flag containers with > 20% CFS throttled periods.
- `conntrack-saturation`: Flag nodes with > 75% conntrack table saturation.
- `ingress-502-drain`: Flag edge ingress gateways lacking `preStop` sleep hooks.
- `ephemeral-growth-rate`: Flag unmetered local disk writes lacking ephemeral limits.
- `ulimit-exhaustion`: Flag proxies approaching maximum open file descriptor ceilings.

## 3. Hand Findings to Fleet Audit

Emit findings using the `fleet-audit` harness lifecycle (`start` ... `finish`).
