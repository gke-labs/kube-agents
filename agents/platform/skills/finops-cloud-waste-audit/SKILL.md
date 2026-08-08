---
name: finops-cloud-waste-audit
description: Audits gross cloud resource overrequest, orphaned retained PersistentVolumes, unattached static IPs, and unindexed Cloud Logging cost runaway.
---

# Task

Audit Google Cloud and GKE resources for financial waste, massive CPU/RAM overrequests, orphaned retained PersistentVolumes, idle static IP addresses, and unindexed Cloud Logging cost runaway, emitting findings for the `fleet-audit` reporting harness.

# Workflow

## 1. Execute FinOps Inspection

Run the profile-relative FinOps runner to sweep target projects:

```bash
./skills/finops-cloud-waste-audit/scripts/finops_waste_audit.py --output /opt/data/scratch/finops_raw.json
```

## 2. Evaluate Findings Against SOP Checks

Filter and categorize collected metrics according to `governance/finops_cloud_waste_sop.md`:

- `massive-overrequest`: Flag workloads requesting > 100 CPU cores or > 500Gi RAM with < 5% usage.
- `orphan-retained-pvs`: Flag released PersistentVolumes incurring unattached storage expense.
- `unattached-static-ips`: Flag reserved external static IP addresses lacking active attachments.
- `cloud-logging-cost-runaway`: Flag unindexed debug log streams exceeding 10GB/day.
- `idle-backend-services`: Flag Cloud Load Balancing backend services receiving 0 traffic over 14 days.

## 3. Hand Findings to Fleet Audit

Emit findings using the `fleet-audit` harness lifecycle (`start` ... `finish`).
