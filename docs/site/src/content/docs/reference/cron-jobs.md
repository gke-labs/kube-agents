---
title: Cron jobs
description: The two shipped cron rosters — the Planning Agent's plumbing and the Platform Agent's governance watchdogs.
sidebar:
  order: 2
---

Two files define the scheduled jobs, one per profile, and which one an entry belongs in follows from what runs it. For the story of what these jobs achieve together, see [Proactive autonomy](/kube-agents/overview/proactive-autonomy/); for the mechanism, [What fires the schedule](/kube-agents/concepts/autonomous-watchdogs/#what-fires-the-schedule).

`agents/chat/defaults/cron/jobs.json` is the Planning Agent's roster, and the only store the gateway's own ticker advances — unless the experimental [`platformFrontDoor`](/kube-agents/operator/platformagent-crd/#platformfrontdoor) flag has re-homed the gateway, which moves the ticked store to the Platform Agent's roster and stops this one. Every entry on it is a `no_agent` **script** job — a plain subprocess, no model prompted — because that profile's toolsets are stripped to `mcp-router`, `kanban` and `memory` and it could not run an audit if asked. Four jobs ship: `profile-cron-tick`, the every-minute dispatcher that ticks every named profile with work due; the hourly `cluster-agent-reconcile` sweep that keeps [Cluster Agent](/kube-agents/concepts/cluster-agents/) profiles aligned with the live fleet; and the two [first-run onboarding](/kube-agents/concepts/chatops/#first-run-onboarding) jobs, `bootstrap-inventory-scan` and `bootstrap-inventory-delivery`.

`agents/platform/cron/jobs.json` is the Platform Agent's roster, and carries the seven governance watchdogs plus `github-repo-watcher`, a `no_agent` poller that runs no model. `profile-cron-tick` is what makes it live: each watchdog is a real cron run in its own process, with that profile's persona, toolsets, `skills`, `model` and `max_turns`. No id may appear on both rosters — two rosters both carrying one is that audit running twice per schedule, concurrently with itself.

## The shipping jobs

Generated from [`agents/chat/defaults/cron/jobs.json`](https://github.com/gke-labs/kube-agents/blob/main/agents/chat/defaults/cron/jobs.json) and [`agents/platform/cron/jobs.json`](https://github.com/gke-labs/kube-agents/blob/main/agents/platform/cron/jobs.json). Retired ids are omitted: an id on its way out ships switched off for a release before it is deleted, and a disabled entry on the Platform Agent's roster is left out of this table rather than listed as a job an operator could reach. See [The retired jobs](/kube-agents/concepts/autonomous-watchdogs/#the-retired-jobs).

<!-- BEGIN GENERATED: cron-jobs -->
<!-- Regenerate with: make docs-generate -- do not edit by hand. -->
<!-- prettier-ignore-start -->

| ID | Profile | Schedule | Cadence | Enabled | Runs |
| -- | ------- | -------- | ------- | :-----: | ---- |
| `profile-cron-tick` | Planning Agent | `* * * * *` | — | yes | `profile_cron_tick.py` |
| `cluster-agent-reconcile` | Planning Agent | `11 * * * *` | Hourly at :11 | yes | `cluster_agent_reconcile.py` |
| `bootstrap-inventory-scan` | Planning Agent | `* * * * *` | — | yes | `bootstrap_scan_gate.py` |
| `bootstrap-inventory-delivery` | Planning Agent | `* * * * *` | — | yes | `bootstrap_delivery.py` |
| `compliance-audit` | Platform Agent | `20 6 * * *` | Daily 06:20 | yes | Run the daily fleet security and RBAC posture audit. Read the SOP at 'governance/compliance_audit_sop.md' i... |
| `obtainability-audit` | Platform Agent | `50 6 * * *` | Daily 06:50 | yes | Run the daily workload reliability audit. Read the SOP at 'governance/obtainability_audit_sop.md' in your p... |
| `security-patch-orchestrator` | Platform Agent | `20 7 * * 1` | Weekly, Monday 07:20 | yes | Run the weekly GKE upgrade and patch readiness audit. Read the SOP at 'governance/security_patch_orchestrat... |
| `fleet-wide-cost-analysis` | Platform Agent | `50 7 * * 1` | Weekly, Monday 07:50 | yes | Run the weekly fleet waste audit. Read the SOP at 'governance/fleet_wide_cost_analysis_sop.md' in your prof... |
| `fleet-consistency-drift` | Platform Agent | `20 8 * * 1` | Weekly, Monday 08:20 | yes | Run the weekly fleet consistency drift audit. Read the SOP at 'governance/fleet_consistency_drift_sop.md' i... |
| `ai-security-audit` | Platform Agent | `50 8 * * *` | Daily 08:50 | yes | Run the daily AI workload security audit. Read the SOP at 'governance/ai_security_audit_sop.md' in your pro... |
| `stockout-prevention` | Platform Agent | `20 9 * * *` | Daily 09:20 | yes | Run the daily fleet stockout prevention and capacity audit. Read the SOP at 'governance/stockout_prevention... |
| `github-repo-watcher` | Platform Agent | `*/10 * * * *` | Every 10 minutes | yes | `github_scan_gate.py` |

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
  "deliver": "chat"
}
```

| Field              | Type            | Purpose                                                                                                                                                                                                                                                                                                                                                                                                                                |
| ------------------ | --------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `id`               | string          | Stable identifier used in observability and enable/disable ops. It survives renames — `obtainability-audit` is now the Workload Reliability Audit.                                                                                                                                                                                                                                                                                     |
| `name`             | string          | Human-readable name for logs. For the audits it is also the ledger issue title, via the `AUDITS` map in `fleet-audit`'s `audit_report.py`.                                                                                                                                                                                                                                                                                             |
| `schedule.kind`    | string          | `"cron"` on every entry. `"interval"` is supported but unused: Hermes re-anchors an interval job to when the last run _finished_, and the gateway ticker sleeps a fixed 60 seconds after each tick returns, so a 1-minute interval fires every two.                                                                                                                                                                                    |
| `schedule.expr`    | string          | Standard 5-field cron expression, evaluated in the pod's time zone (UTC unless overridden).                                                                                                                                                                                                                                                                                                                                            |
| `schedule.display` | string          | Display form (usually equal to `expr`).                                                                                                                                                                                                                                                                                                                                                                                                |
| `prompt`           | string          | What the run is asked to do, copied verbatim into the turn. Governance jobs name their SOP **relative to the profile home** — `governance/<sop>.md`. It lives here and nowhere else.                                                                                                                                                                                                                                                   |
| `skills`           | array of string | The skills the run needs. The scheduler force-loads each one's text ahead of the first turn, rather than leaving the load to the model's discretion. The seven audits use `fleet-audit`. A `no_agent` job prompts no model, so the field is ignored there.                                                                                                                                                                             |
| `no_agent`         | bool            | Set on the Planning Agent's four plumbing jobs and on `github-repo-watcher` on the Platform Agent's: the tick is a subprocess, not an LLM turn. The governance watchdogs omit it.                                                                                                                                                                                                                                                      |
| `script`           | string          | For a `no_agent` job, the script to run, resolved in that profile's `scripts/`. The scheduler runs it with no arguments.                                                                                                                                                                                                                                                                                                               |
| `enabled`          | bool            | Set `false` to disable without deleting the entry. See [Disabling a watchdog](/kube-agents/concepts/autonomous-watchdogs/#disabling-a-watchdog) — a deleted entry is not removed from a cluster that already has it.                                                                                                                                                                                                                   |
| `deliver`          | string          | Where the run's outcome goes. `"all"` sends it to the configured target; `"chat"` hands the report to the Planning Agent, who posts it and can answer a follow-up about it; `"local"` resolves to no target at all and drops it. Every enabled job on the Platform Agent's roster — the seven watchdogs and the poller — uses `"chat"`, so a job that has stopped working is visible rather than indistinguishable from a quiet fleet. |

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

### How an edit reaches an existing pod

This part is about the **Planning Agent's** file, [`agents/chat/defaults/cron/jobs.json`](https://github.com/gke-labs/kube-agents/blob/main/agents/chat/defaults/cron/jobs.json) — the one the start-up reconcile below acts on. The Platform Agent's roster, which the rest of this page documents, reaches its profile by a separate path: `profile_scaffold.py` scaffolds it, and `merge_cron_store` merges it under the same per-key rule.

`$HERMES_HOME/cron/jobs.json` lives on the agent's persistent volume, and the scheduler writes `last_run` back into it on every tick — so the volume's copy is always newer than the image's, and the entrypoint's update-only defaults copy never overwrites it. Simply overwriting the file is not an option either: it would reset every `last_run` (making all jobs look due at once), discard the chat binding [first-run onboarding](/kube-agents/concepts/chatops/#first-run-onboarding) writes, and reinstate the two onboarding jobs that finishing onboarding deliberately removes.

So [`docker-entrypoint.sh`](https://github.com/gke-labs/kube-agents/blob/main/deploy/shared/docker-entrypoint.sh) runs [`cron_jobs_sync.py`](https://github.com/gke-labs/kube-agents/blob/main/agents/chat/scripts/cron_jobs_sync.py) before the scheduler starts, merging the image's declarations into the volume's file **by job id**. The merge is per key, not per field-list: **the image wins every key it ships, and every key it does not ship is left as the volume had it.**

- A job's **definition** — `name`, `schedule`, `prompt`, `skills`, `script`, `no_agent`, and `enabled` — is shipped, so it tracks the image. Shipping `enabled: false` is therefore the fleet-wide off switch described in [Disabling a watchdog](/kube-agents/concepts/autonomous-watchdogs/#disabling-a-watchdog); the cost is that a job disabled by hand on a live pod is switched back on by the next image roll.
- The scheduler's own state — `last_run` and whatever else Hermes records per job — is shipped by nothing, so it survives untouched. Stating the rule this way rather than as a list of runtime-owned names is deliberate: a list has to be complete to be correct, and it stops being complete the day upstream records a new field.
- `deliver` is the single exception, because a runtime hook owns it: onboarding rewrites it to `origin` on the delivery job, and taking the image's value back would send the report nowhere.
- A job the image does not declare is never deleted; it may have been added by hand. Retirement therefore does not propagate to existing volumes — drop an id only once every live cluster has merged its disabled form.
- A job this script has installed before and that is now absent was removed on purpose, so it is not reinstalled. A ledger at `$HERMES_HOME/.cron_jobs_installed` records which ids those are.

The same start-up reconcile applies to the specialist profiles' `skills/` directories, which are wholly image-owned and replaced from the baked template on every start — see [Skills](/kube-agents/concepts/skills/#importing-external-skills).
