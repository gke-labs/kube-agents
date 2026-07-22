---
title: Autonomous watchdogs
description: Cron-scheduled jobs that make the Platform Agent proactive rather than reactive.
sidebar:
  order: 5
---

`agents/platform/cron/jobs.json` defines the scheduled jobs. Each one fires a pre-authored prompt at the Platform Agent on a cron schedule. The prompts typically point at a [governance SOP](/kube-agents/concepts/governance-sops/); the agent reads the SOP, executes the procedure, and either files a PR (via `submit-suggestion`) or posts a proactive Chat alert.

Full JSON is annotated on [Reference → Cron jobs](/kube-agents/reference/cron-jobs/).

## The shipping jobs

| Job                             | Cron           | Cadence           | Invokes                                |
| ------------------------------- | -------------- | ----------------- | -------------------------------------- |
| `blueprint-sync`                | `0 9 * * *`    | Daily 09:00       | `blueprint_sync_sop.md`                |
| `policy-propagation`            | `0 * * * *`    | Hourly            | `policy_propagation_sop.md`            |
| `global-capacity-orchestrator`  | `0 * * * *`    | Hourly            | `global_capacity_orchestrator_sop.md`  |
| `fleet-wide-cost-analysis`      | `0 10 * * *`   | Daily 10:00       | `fleet_wide_cost_analysis_sop.md`      |
| `security-patch-orchestrator`   | `0 11 * * *`   | Daily 11:00       | `security_patch_orchestrator_sop.md`   |
| `obtainability-audit`           | `0 12 * * *`   | Daily 12:00       | `obtainability_audit_sop.md`           |
| `compliance-audit`              | `0 9 * * 0`    | Weekly Sun 09:00  | `compliance_audit_sop.md`              |
| `standardization-validator`     | `0 10 * * 0`   | Weekly Sun 10:00  | `standardization_validator_sop.md`     |
| `lifecycle-deprecation-manager` | `0 9 1 * *`    | Monthly 1st 09:00 | `lifecycle_deprecation_manager_sop.md` |
| `github-issue-resolver`         | `*/30 * * * *` | Every 30 minutes  | `github-issue-resolver` skill          |

All are `enabled: true` in the shipping config.

## Job shape

Each job in `jobs.json` follows this schema:

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

- **`id`** — stable identifier, referenced in observability and disable/enable ops.
- **`schedule.expr`** — standard 5-field cron in the pod's local time zone (UTC unless the pod's TZ is overridden).
- **`prompt`** — verbatim message sent to the agent when the schedule fires. Typically instructs the agent to load a specific SOP file from `/opt/defaults/governance/`.
- **`skills`** — optional array of skill names to preload. Most jobs leave this empty because the SOP directs skill loading itself; `github-issue-resolver` is the exception and loads its namesake skill directly.
- **`enabled`** — set to `false` to disable a job without deleting its entry.
- **`deliver`** (optional) — controls chat delivery. Set on `github-issue-resolver` to `"all"` meaning every run reports back.

## Recent changes

- [PR #354 — `fix(cron): reduce github issue resolver execution frequency`](https://github.com/gke-labs/kube-agents/pull/354) — reduced from a more aggressive schedule to every 30 minutes to lower LLM cost and Chat noise.
- [PR #356 — daily fleet health digest at 13:00 UTC](https://github.com/gke-labs/kube-agents/pull/356) — adds a new job that emits a daily digest of the day's findings.
- [PR #347 — `gke-node-problem-detector` skill + 15m watchdog](https://github.com/gke-labs/kube-agents/pull/347) — adds a 15-minute node-health watchdog.

Both PR #356 and PR #347 are unmerged as of this writing. When they land, the table above will need updating.

## Disabling a watchdog

Edit `cron/jobs.json`, flip `enabled` to `false`, and redeploy the workspace (`provision_08_deploy_platform_agent.sh` or `dev/dev_rebuild_agent.sh`). The change is picked up on the next agent restart.

## Adding a watchdog

1. Write a governance SOP in `agents/platform/governance/<your-sop>.md`.
2. Add a job entry to `cron/jobs.json` pointing at the SOP.
3. Redeploy.

Keep the schedule realistic — LLM inference on every tick has cost. Hourly or daily is the sweet spot for most SOPs; sub-15-minute cadences should have a clear justification.

## Where to go next

- [Reference → Cron jobs](/kube-agents/reference/cron-jobs/) — full annotated `jobs.json`.
- [Governance SOPs](/kube-agents/concepts/governance-sops/) — the playbooks these watchdogs execute.
- [Declarative workflow](/kube-agents/concepts/declarative-workflow/) — how findings become PRs.
