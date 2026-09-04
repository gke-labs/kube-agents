---
name: gce-compute-fleet-audit
description: Audits standalone GCE virtual machines, Managed Instance Groups (MIGs), serial console boot failures, and guest OS daemon health.
---

# Task

Audit standalone GCE virtual machines, Managed Instance Groups (MIGs), serial console boot failures, and guest OS daemon health, emitting findings for the `fleet-audit` reporting harness.

# Workflow

## 1. Execute Compute Inspection

Run the profile-relative compute fleet runner to sweep target projects:

```bash
./skills/gce-compute-fleet-audit/scripts/compute_fleet_audit.py --output /opt/data/scratch/compute_raw.json
```

## 2. Evaluate Findings Against SOP Checks

Filter and categorize collected metrics according to `governance/gce_compute_fleet_sop.md`:

- `gce-startup-script-status`: Flag serial console boot failures and startup script errors.
- `mig-autoscaler-flapping`: Flag continuous autohealing recreation cycles.
- `ops-agent-guest-health`: Flag instances with unmonitored or dead Ops Agent daemons.
- `sole-tenant-headroom`: Flag sole-tenant node groups approaching capacity limits.
- `orphaned-snapshots`: Flag Persistent Disk snapshots older than 90 days.

## 3. Hand Findings to Fleet Audit

Emit findings using the `fleet-audit` harness lifecycle (`start` ... `finish`).
