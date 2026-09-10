---
name: gcp-recommender-ingest
description: Ingests GCP Cloud Recommender insights and GKE notifications into fleet audit findings.
---

# Task

Ingest GCP Cloud Recommender recommendations and GKE pre-upgrade notifications across fleet projects and structure them into deterministic finding objects for the `fleet-audit` reporting harness.

# Workflow

## 1. Execute Ingestion Script

Run the profile-relative ingestion runner to sweep all fleet projects:

```bash
./skills/gcp-recommender-ingest/scripts/recommender_ingest.py --output /opt/data/scratch/recommender_raw.json
```

## 2. Evaluate Findings Against SOP Checks

Filter and categorize collected recommendations according to `governance/gcp_recommender_sop.md`:

- `gke-webhook-readiness`: Flag admission webhooks blocking upgrade eligibility.
- `gke-security-posture-cve`: Flag GKE container image vulnerabilities and security posture insights.

## 3. Hand Findings to Fleet Audit

Emit findings using the `fleet-audit` harness lifecycle (`start` ... `finish`).
