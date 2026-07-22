---
title: Cron jobs
description: Full annotated agents/platform/cron/jobs.json — the autonomous watchdogs.
sidebar:
  order: 2
---

`agents/platform/cron/jobs.json` defines the autonomous watchdog jobs. For the story of what they achieve together, see [Proactive autonomy](/kube-agents/overview/proactive-autonomy/). For how the schedule/prompt loop works, see [Autonomous watchdogs](/kube-agents/concepts/autonomous-watchdogs/).

## The shipping jobs

| ID                              | Schedule       | Prompt (abbreviated)                                                                         |
| ------------------------------- | -------------- | -------------------------------------------------------------------------------------------- |
| `blueprint-sync`                | `0 9 * * *`    | Execute GKE blueprint alignment audit; read `blueprint_sync_sop.md`.                         |
| `compliance-audit`              | `0 9 * * 0`    | Execute fleet-wide security compliance audit; read `compliance_audit_sop.md`.                |
| `policy-propagation`            | `0 * * * *`    | Propagate updated operational policies; read `policy_propagation_sop.md`.                    |
| `global-capacity-orchestrator`  | `0 * * * *`    | Execute cross-cluster capacity optimization; read `global_capacity_orchestrator_sop.md`.     |
| `fleet-wide-cost-analysis`      | `0 10 * * *`   | Execute daily cost delta audit; read `fleet_wide_cost_analysis_sop.md`.                      |
| `security-patch-orchestrator`   | `0 11 * * *`   | Run vulnerability and patch compliance scan; read `security_patch_orchestrator_sop.md`.      |
| `lifecycle-deprecation-manager` | `0 9 1 * *`    | Execute monthly toolchain lifecycle audit; read `lifecycle_deprecation_manager_sop.md`.      |
| `standardization-validator`     | `0 10 * * 0`   | Run weekly structural GKE alignment audit; read `standardization_validator_sop.md`.          |
| `obtainability-audit`           | `0 12 * * *`   | Execute dynamic capacity pool alignment audit; read `obtainability_audit_sop.md`.            |
| `github-issue-resolver`         | `*/30 * * * *` | Run the `github-issue-resolver` skill to poll, triage, investigate, and resolve open issues. |

All are `"enabled": true` in the shipping config.

## Job schema

Each entry follows this shape:

```json
{
  "id": "blueprint-sync",
  "name": "Blueprint Sync",
  "schedule": {
    "kind": "cron",
    "expr": "0 9 * * *",
    "display": "0 9 * * *"
  },
  "prompt": "Execute GKE blueprint alignment audit. Read '/opt/defaults/governance/blueprint_sync_sop.md' and perform the daily GKE cluster compliance checks against the master blueprints.",
  "skills": [],
  "enabled": true
}
```

| Field                | Type            | Purpose                                                                                      |
| -------------------- | --------------- | -------------------------------------------------------------------------------------------- |
| `id`                 | string          | Stable identifier used in observability and enable/disable ops.                              |
| `name`               | string          | Human-readable name for logs and Chat replies.                                               |
| `schedule.kind`      | string          | Only `"cron"` is used today.                                                                 |
| `schedule.expr`      | string          | Standard 5-field cron expression, evaluated in the pod's time zone (UTC unless overridden).  |
| `schedule.display`   | string          | Display form (usually equal to `expr`).                                                      |
| `prompt`             | string          | The literal message sent to the agent when the schedule fires.                               |
| `skills`             | array of string | Optional: skills to preload. Most jobs leave empty (the SOP loads what it needs).            |
| `enabled`            | bool            | Set `false` to disable without deleting the entry.                                           |
| `deliver` (optional) | string          | Chat delivery mode. `"all"` on `github-issue-resolver` means every run reports back to Chat. |

## Change log

- **[PR #354](https://github.com/gke-labs/kube-agents/pull/354)** (merged 2026-07-20) — `github-issue-resolver` dropped from a higher frequency to `*/30 * * * *` to reduce Chat noise and inference cost.
- **[PR #356](https://github.com/gke-labs/kube-agents/pull/356)** (open) — proposes a `fleet-health-digest` job at 13:00 UTC daily.
- **[PR #347](https://github.com/gke-labs/kube-agents/pull/347)** (open) — proposes a 15-minute `gke-node-problem-detector` watchdog.

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
