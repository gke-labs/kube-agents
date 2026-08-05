---
title: Cron jobs
description: Full annotated agents/platform/cron/jobs.json — the autonomous watchdogs.
sidebar:
  order: 2
---

`agents/platform/cron/jobs.json` defines the autonomous watchdog jobs. For the story of what they achieve together, see [Proactive autonomy](/kube-agents/overview/proactive-autonomy/). For how the schedule/prompt loop works, see [Autonomous watchdogs](/kube-agents/concepts/autonomous-watchdogs/).

The Chat Agent profile carries a second, separate job file — [`agents/chat/defaults/cron/jobs.json`](https://github.com/gke-labs/kube-agents/blob/main/agents/chat/defaults/cron/jobs.json) — with three `no_agent` **script** jobs (a script runs as a plain subprocess instead of prompting the model): the hourly `cluster-agent-reconcile` sweep that keeps [Cluster Agent](/kube-agents/concepts/cluster-agents/) profiles aligned with the live fleet, and the two [first-run onboarding](/kube-agents/concepts/chatops/#first-run-onboarding) jobs, `bootstrap-inventory-scan` and `bootstrap-inventory-delivery`. Those are not in the generated table below, which sources the Platform Agent's file only.

## The shipping jobs

Generated from [`agents/platform/cron/jobs.json`](https://github.com/gke-labs/kube-agents/blob/main/agents/platform/cron/jobs.json).

<!-- BEGIN GENERATED: cron-jobs -->
<!-- Regenerate with: make docs-generate -- do not edit by hand. -->
<!-- prettier-ignore-start -->

| ID | Schedule | Cadence | Enabled | Prompt |
| -- | -------- | ------- | :-----: | ------ |
| `compliance-audit` | `20 6 * * *` | Daily 06:20 | yes | Run the daily fleet security and RBAC posture audit. Read the SOP at 'governance/compliance_audit_sop.md' i... |
| `obtainability-audit` | `50 6 * * *` | Daily 06:50 | yes | Run the daily workload reliability audit. Read the SOP at 'governance/obtainability_audit_sop.md' in your p... |
| `security-patch-orchestrator` | `20 7 * * 1` | Weekly, Monday 07:20 | yes | Run the weekly GKE upgrade and patch readiness audit. Read the SOP at 'governance/security_patch_orchestrat... |
| `fleet-wide-cost-analysis` | `50 7 * * 1` | Weekly, Monday 07:50 | yes | Run the weekly fleet waste audit. Read the SOP at 'governance/fleet_wide_cost_analysis_sop.md' in your prof... |
| `fleet-consistency-drift` | `20 8 * * 1` | Weekly, Monday 08:20 | yes | Run the weekly fleet consistency drift audit. Read the SOP at 'governance/fleet_consistency_drift_sop.md' i... |
| `github-issue-resolver` | `*/30 * * * *` | Every 30 minutes | yes | Run the github-issue-resolver skill to poll, triage, investigate, and resolve unaddressed open issues on ou... |
| `blueprint-sync` | `0 9 * * *` | Daily 09:00 | no | Execute GKE blueprint alignment audit. Read 'governance/blueprint_sync_sop.md' in your profile home and per... |
| `policy-propagation` | `0 * * * *` | Hourly | no | Propagate updated operational policies. Read 'governance/policy_propagation_sop.md' in your profile home an... |
| `global-capacity-orchestrator` | `0 * * * *` | Hourly | no | Execute cross-cluster capacity optimization. Read 'governance/global_capacity_orchestrator_sop.md' in your... |
| `standardization-validator` | `0 10 * * 0` | Weekly, Sunday 10:00 | no | Run weekly structural GKE alignment audit. Read 'governance/standardization_validator_sop.md' in your profi... |
| `lifecycle-deprecation-manager` | `0 9 1 * *` | Monthly, 1st 09:00 | no | Execute monthly toolchain lifecycle audit. Read 'governance/lifecycle_deprecation_manager_sop.md' in your p... |

<!-- prettier-ignore-end -->
<!-- END GENERATED: cron-jobs -->

## Job schema

Each entry follows this shape:

```json
{
  "id": "compliance-audit",
  "name": "Security & RBAC Posture Audit",
  "schedule": {
    "kind": "cron",
    "expr": "20 6 * * *",
    "display": "20 6 * * *"
  },
  "prompt": "Run the daily fleet security and RBAC posture audit. Read the SOP at 'governance/compliance_audit_sop.md' in your profile home — all 348 lines of it, before you run anything. Its eleven checks are section 2, lines 56-270, so a read that stops early skips almost the entire audit and reports a clean fleet it never looked at. Then execute it exactly, using the fleet-audit skill to open and close the audit run.",
  "skills": ["fleet-audit"],
  "enabled": true,
  "deliver": "all"
}
```

| Field                | Type            | Purpose                                                                                                                                                          |
| -------------------- | --------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `id`                 | string          | Stable identifier used in observability and enable/disable ops. It survives renames — `obtainability-audit` is now the Workload Reliability Audit.               |
| `name`               | string          | Human-readable name for logs and Chat replies. For the five audits it is also the ledger issue title, via the `AUDITS` map in `fleet-audit`'s `audit_report.py`. |
| `schedule.kind`      | string          | Only `"cron"` is used today.                                                                                                                                     |
| `schedule.expr`      | string          | Standard 5-field cron expression, evaluated in the pod's time zone (UTC unless overridden).                                                                      |
| `schedule.display`   | string          | Display form (usually equal to `expr`).                                                                                                                          |
| `prompt`             | string          | The literal message sent to the agent when the schedule fires. Governance jobs name their SOP **relative to the profile home** — `governance/<sop>.md`.          |
| `skills`             | array of string | Optional: skills to preload. The five audits preload `fleet-audit`; the disabled governance jobs leave it empty (the SOP loads what it needs).                   |
| `enabled`            | bool            | Set `false` to disable without deleting the entry.                                                                                                               |
| `deliver` (optional) | string          | Chat delivery mode. `"all"` is set on all six enabled jobs, which is safe because each returns exactly `[SILENT]` when it has nothing to report.                 |

## Editing

Edit `jobs.json`, then redeploy the workspace:

```bash
cd k8s-operator/scripts
./provision_08_deploy_platform_agent.sh
```

Or during development:

```bash
cd k8s-operator
make dev-rebuild-agent ARGS="platform"
```

The change is picked up on the next pod restart.
