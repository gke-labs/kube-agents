---
name: finops-cloud-waste-audit
description: Audits unfiltered Cloud Logging cost runaway and idle load balancer backend services.
---

# Task

Audit Google Cloud resources for financial waste, unfiltered high-throughput Cloud Logging export sinks, and idle Load Balancing backend services, emitting findings for the `fleet-audit` reporting harness.

# Workflow

## 1. Execute FinOps Inspection

Follow the authoritative checklist in `governance/finops_cloud_waste_sop.md` across target GCP projects:

- `cloud-logging-cost-runaway`: Flag unfiltered high-throughput Cloud Logging export sinks.
- `idle-backend-services`: Flag Cloud Load Balancing backend services lacking active backends.

Optional helper runner for backend services:

```bash
./skills/finops-cloud-waste-audit/scripts/finops_waste_audit.py --output /opt/data/scratch/finops_raw.json
```

## 2. Hand Findings to Fleet Audit

Emit findings using the `fleet-audit` harness lifecycle (`start` ... `finish`).
