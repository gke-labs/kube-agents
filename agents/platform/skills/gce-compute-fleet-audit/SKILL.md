---
name: gce-compute-fleet-audit
description: Audits standalone GCE virtual machines, Managed Instance Groups (MIGs), serial console boot failures, sole-tenant node group headroom, and orphaned disk snapshots.
---

# Task

Audit standalone GCE virtual machines, Managed Instance Groups (MIGs), serial console boot failures, sole-tenant node group headroom, and orphaned disk snapshots, emitting findings for the `fleet-audit` reporting harness.

# Workflow

## 1. Open the Run, Then Execute Compute Inspection

Open the run first with the `fleet-audit` harness's `start` (SOP §0). `finish` refuses a manifest that finished before `start` did, as an earlier run's. Then call the platform_control `fleet_scope` tool (SOP §1) and run the profile-relative compute fleet collector with its `collector_args`, which hand it the install's declared scope; an install that declares none passes nothing and the collector sweeps every project the identity can list. It writes a collector manifest to stdout:

```bash
python3 ./skills/gce-compute-fleet-audit/scripts/compute_fleet_audit.py <collector_args from the fleet_scope tool, verbatim, or nothing on an install that declares no scope> > /opt/data/scratch/manifest_gce-compute-fleet-audit.json
```

## 2. Evaluate Findings Against SOP Checks

Read the manifest and follow `governance/gce_compute_fleet_sop.md` §2, which owns the copy rules for `commands`, `checks_not_applicable`, `checks_unevaluated`, `limitations` and `candidates`:

All four roster checks are collector-verified; none is yours to hand-run.

- `gce-startup-script-status`: serial console boot failures and startup script errors.
- `mig-convergence-stalled`: MIGs creating and deleting at once (a resize loop).
- `sole-tenant-headroom`: node group reservations at capacity with no failover host spare.
- `orphaned-snapshots`: Persistent Disk snapshots of deleted disks older than 90 days.

`ops-agent-guest-health` is not on the roster — SOP §2.3 says why, and `finish` rejects a `checks_run` or a `finding.check` naming it.

## 3. Hand Findings to Fleet Audit

Finish the run you opened in step 1 with the `fleet-audit` harness's `finish`, passing `--manifest-file` as the SOP's §5 directs, so it cross-checks `checks_run` against what the collector ran. This stream's `finish` requires that flag, or `--no-collector-manifest` with a reason.
