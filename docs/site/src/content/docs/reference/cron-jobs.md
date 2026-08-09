---
title: Cron jobs
description: The two shipped cron rosters — the Chat Agent's plumbing and the Platform Agent's governance watchdogs.
sidebar:
  order: 2
---

Two files define the scheduled jobs, one per profile, and which one an entry belongs in follows from what runs it. For the story of what these jobs achieve together, see [Proactive autonomy](/kube-agents/overview/proactive-autonomy/); for the mechanism, [What fires the schedule](/kube-agents/concepts/autonomous-watchdogs/#what-fires-the-schedule).

`agents/chat/defaults/cron/jobs.json` is the Chat Agent's roster, and the only store the gateway's own ticker advances. Every entry on it is a `no_agent` **script** job — a plain subprocess, no model prompted — because that profile's toolsets are stripped to `mcp-router`, `kanban` and `memory` and it could not run an audit if asked. Four jobs ship: `profile-cron-tick`, the every-minute dispatcher that ticks every named profile with work due; the hourly `cluster-agent-reconcile` sweep that keeps [Cluster Agent](/kube-agents/concepts/cluster-agents/) profiles aligned with the live fleet; and the two [first-run onboarding](/kube-agents/concepts/chatops/#first-run-onboarding) jobs, `bootstrap-inventory-scan` and `bootstrap-inventory-delivery`.

`agents/platform/cron/jobs.json` is the Platform Agent's roster, and carries the seven governance watchdogs. `profile-cron-tick` is what makes it live: each is a real cron run in its own process, with that profile's persona, toolsets, `skills`, `model` and `max_turns`. No id may appear on both rosters — two rosters both carrying one is that audit running twice per schedule, concurrently with itself.

## The shipping jobs

Generated from [`agents/chat/defaults/cron/jobs.json`](https://github.com/gke-labs/kube-agents/blob/main/agents/chat/defaults/cron/jobs.json) and [`agents/platform/cron/jobs.json`](https://github.com/gke-labs/kube-agents/blob/main/agents/platform/cron/jobs.json). Retired ids are omitted: an id on its way out ships switched off for a release before it is deleted, and a disabled entry on the Platform Agent's roster is left out of this table rather than listed as a job an operator could reach. See [The retired jobs](/kube-agents/concepts/autonomous-watchdogs/#the-retired-jobs).

<!-- BEGIN GENERATED: cron-jobs -->
<!-- Regenerate with: make docs-generate -- do not edit by hand. -->
<!-- prettier-ignore-start -->

| ID | Profile | Schedule | Cadence | Enabled | Runs |
| -- | ------- | -------- | ------- | :-----: | ---- |
| `profile-cron-tick` | Chat Agent | `* * * * *` | — | yes | `profile_cron_tick.py` |
| `cluster-agent-reconcile` | Chat Agent | `11 * * * *` | Hourly at :11 | yes | `cluster_agent_reconcile.py` |
| `bootstrap-inventory-scan` | Chat Agent | `* * * * *` | — | yes | `bootstrap_scan_gate.py` |
| `bootstrap-inventory-delivery` | Chat Agent | `* * * * *` | — | yes | `bootstrap_delivery.py` |
| `compliance-audit` | Platform Agent | `20 6 * * *` | Daily 06:20 | yes | Run the daily fleet security and RBAC posture audit. Read the SOP at 'governance/compliance_audit_sop.md' i... |
| `obtainability-audit` | Platform Agent | `50 6 * * *` | Daily 06:50 | yes | Run the daily workload reliability audit. Read the SOP at 'governance/obtainability_audit_sop.md' in your p... |
| `security-patch-orchestrator` | Platform Agent | `20 7 * * 1` | Weekly, Monday 07:20 | yes | Run the weekly GKE upgrade and patch readiness audit. Read the SOP at 'governance/security_patch_orchestrat... |
| `fleet-wide-cost-analysis` | Platform Agent | `50 7 * * 1` | Weekly, Monday 07:50 | yes | Run the weekly fleet waste audit. Read the SOP at 'governance/fleet_wide_cost_analysis_sop.md' in your prof... |
| `fleet-consistency-drift` | Platform Agent | `20 8 * * 1` | Weekly, Monday 08:20 | yes | Run the weekly fleet consistency drift audit. Read the SOP at 'governance/fleet_consistency_drift_sop.md' i... |
| `ai-security-audit` | Platform Agent | `50 8 * * *` | Daily 08:50 | yes | Run the daily AI workload security audit. Read the SOP at 'governance/ai_security_audit_sop.md' in your pro... |
| `github-issue-resolver` | Platform Agent | `*/30 * * * *` | Every 30 minutes | yes | Run the github-issue-resolver skill to poll, triage, investigate, and resolve unaddressed open issues on ou... |

<!-- prettier-ignore-end -->
<!-- END GENERATED: cron-jobs -->

## Job schema

Both rosters use one schema. A governance watchdog:

```json
{
  "id": "compliance-audit",
  "name": "Security & RBAC Posture Audit",
  "schedule": {
    "kind": "cron",
    "expr": "20 6 * * *",
    "display": "20 6 * * *"
  },
  "prompt": "Run the daily fleet security and RBAC posture audit. Read the SOP at 'governance/compliance_audit_sop.md' in your profile home — all 406 lines of it, before you run anything. Its eleven checks are section 2, lines 102-314, so a read that stops early skips almost the entire audit and reports a clean fleet it never looked at. Then execute it exactly, using the fleet-audit skill to open and close the audit run.",
  "skills": ["fleet-audit"],
  "enabled": true,
  "deliver": "all"
}
```

| Field              | Type            | Purpose                                                                                                                                                                                                                                                                                                   |
| ------------------ | --------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `id`               | string          | Stable identifier used in observability and enable/disable ops. It survives renames — `obtainability-audit` is now the Workload Reliability Audit.                                                                                                                                                        |
| `name`             | string          | Human-readable name for logs. For the audits it is also the ledger issue title, via the `AUDITS` map in `fleet-audit`'s `audit_report.py`.                                                                                                                                                                |
| `schedule.kind`    | string          | `"cron"` on every entry. `"interval"` is supported but unused: Hermes re-anchors an interval job to when the last run _finished_, and the gateway ticker sleeps a fixed 60 seconds after each tick returns, so a 1-minute interval fires every two.                                                       |
| `schedule.expr`    | string          | Standard 5-field cron expression, evaluated in the pod's time zone (UTC unless overridden).                                                                                                                                                                                                               |
| `schedule.display` | string          | Display form (usually equal to `expr`).                                                                                                                                                                                                                                                                   |
| `prompt`           | string          | What the run is asked to do, copied verbatim into the turn. Governance jobs name their SOP **relative to the profile home** — `governance/<sop>.md`. It lives here and nowhere else.                                                                                                                      |
| `skills`           | array of string | The skills the run needs. The scheduler force-loads each one's text ahead of the first turn, rather than leaving the load to the model's discretion. The six audits use `fleet-audit`; `github-issue-resolver` uses its namesake skill. A `no_agent` job prompts no model, so the field is ignored there. |
| `no_agent`         | bool            | Set on the Chat Agent's four plumbing jobs: the tick is a subprocess, not an LLM turn. The governance watchdogs omit it.                                                                                                                                                                                  |
| `script`           | string          | For a `no_agent` job, the script to run, resolved in that profile's `scripts/`. The scheduler runs it with no arguments.                                                                                                                                                                                  |
| `enabled`          | bool            | Set `false` to disable without deleting the entry. See [Disabling a watchdog](/kube-agents/concepts/autonomous-watchdogs/#disabling-a-watchdog) — a deleted entry is not removed from a cluster that already has it.                                                                                      |
| `deliver`          | string          | Where the run's outcome goes. `"all"` sends it to the configured target; `"local"` resolves to no target at all and drops it. The seven watchdogs all use `"all"`, so a watchdog that has stopped working is visible rather than indistinguishable from a quiet fleet.                                    |

## Editing

Adding or editing a job is a one-file change — see [Adding a watchdog](/kube-agents/concepts/autonomous-watchdogs/#adding-a-watchdog).

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
