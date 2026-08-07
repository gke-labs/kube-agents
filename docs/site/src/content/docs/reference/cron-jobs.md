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
  "prompt": "Run the daily fleet security and RBAC posture audit. Read the SOP at 'governance/compliance_audit_sop.md' in your profile home — all 406 lines of it, before you run anything. Its eleven checks are section 2, lines 102-314, so a read that stops early skips almost the entire audit and reports a clean fleet it never looked at. Then execute it exactly, using the fleet-audit skill to open and close the audit run.",
  "skills": ["fleet-audit"],
  "enabled": true,
  "deliver": "all"
}
```

| Field                | Type            | Purpose                                                                                                                                                                                                              |
| -------------------- | --------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `id`                 | string          | Stable identifier used in observability and enable/disable ops. It survives renames — `obtainability-audit` is now the Workload Reliability Audit.                                                                   |
| `name`               | string          | Human-readable name for logs and Chat replies. For the five audits it is also the ledger issue title, via the `AUDITS` map in `fleet-audit`'s `audit_report.py`.                                                     |
| `schedule.kind`      | string          | Only `"cron"` is used today.                                                                                                                                                                                         |
| `schedule.expr`      | string          | Standard 5-field cron expression, evaluated in the pod's time zone (UTC unless overridden).                                                                                                                          |
| `schedule.display`   | string          | Display form (usually equal to `expr`).                                                                                                                                                                              |
| `prompt`             | string          | The literal message sent to the agent when the schedule fires. Governance jobs name their SOP **relative to the profile home** — `governance/<sop>.md`.                                                              |
| `skills`             | array of string | Optional: skills to preload. The five audits preload `fleet-audit`; `github-issue-resolver` preloads its namesake skill. Omit or leave empty to let the SOP load what it needs.                                      |
| `enabled`            | bool            | Set `false` to disable without deleting the entry. See [Disabling a watchdog](/kube-agents/concepts/autonomous-watchdogs/#disabling-a-watchdog) — a deleted entry is not removed from a cluster that already has it. |
| `deliver` (optional) | string          | Chat delivery mode. `"all"` is set on all six jobs, which is safe because each returns exactly `[SILENT]` when it has nothing to report.                                                                             |

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

### How an edit reaches an existing pod

This part is about the **Chat Agent's** file, [`agents/chat/defaults/cron/jobs.json`](https://github.com/gke-labs/kube-agents/blob/main/agents/chat/defaults/cron/jobs.json) — the one the start-up reconcile below acts on. The Platform Agent's roster, which the rest of this page documents, reaches its profile by a separate path: `profile_scaffold.py` scaffolds it, and `merge_cron_store` merges it under the same per-key rule.

`$HERMES_HOME/cron/jobs.json` lives on the agent's persistent volume, and the scheduler writes `last_run` back into it on every tick — so the volume's copy is always newer than the image's, and the entrypoint's update-only defaults copy never overwrites it. Simply overwriting the file is not an option either: it would reset every `last_run` (making all jobs look due at once), discard the chat binding [first-run onboarding](/kube-agents/concepts/chatops/#first-run-onboarding) writes, and reinstate the two onboarding jobs that finishing onboarding deliberately removes.

So [`docker-entrypoint.sh`](https://github.com/gke-labs/kube-agents/blob/main/deploy/shared/docker-entrypoint.sh) runs [`cron_jobs_sync.py`](https://github.com/gke-labs/kube-agents/blob/main/agents/chat/scripts/cron_jobs_sync.py) before the scheduler starts, merging the image's declarations into the volume's file **by job id**. The merge is per key, not per field-list: **the image wins every key it ships, and every key it does not ship is left as the volume had it.**

- A job's **definition** — `name`, `schedule`, `prompt`, `skills`, `script`, `no_agent`, and `enabled` — is shipped, so it tracks the image. Shipping `enabled: false` is therefore the fleet-wide off switch described in [Disabling a watchdog](/kube-agents/concepts/autonomous-watchdogs/#disabling-a-watchdog); the cost is that a job disabled by hand on a live pod is switched back on by the next image roll.
- The scheduler's own state — `last_run` and whatever else Hermes records per job — is shipped by nothing, so it survives untouched. Stating the rule this way rather than as a list of runtime-owned names is deliberate: a list has to be complete to be correct, and it stops being complete the day upstream records a new field.
- `deliver` is the single exception, because a runtime hook owns it: onboarding rewrites it to `origin` on the delivery job, and taking the image's value back would send the report nowhere.
- A job the image does not declare is never deleted; it may have been added by hand. Retirement therefore does not propagate to existing volumes — drop an id only once every live cluster has merged its disabled form.
- A job this script has installed before and that is now absent was removed on purpose, so it is not reinstalled. A ledger at `$HERMES_HOME/.cron_jobs_installed` records which ids those are.

The same start-up reconcile applies to the specialist profiles' `skills/` directories, which are wholly image-owned and replaced from the baked template on every start — see [Skills](/kube-agents/concepts/skills/#importing-external-skills).
