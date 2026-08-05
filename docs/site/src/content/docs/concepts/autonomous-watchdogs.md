---
title: Autonomous watchdogs
description: Cron-scheduled jobs that make the Platform Agent proactive rather than reactive.
sidebar:
  order: 6
---

`agents/platform/cron/jobs.json` defines the scheduled jobs. Each one fires a pre-authored prompt at the Platform Agent on a cron schedule. The prompts typically point at a [governance SOP](/kube-agents/concepts/governance-sops/); the agent reads the SOP, executes the procedure, and either publishes to your GitOps repo — a proposed PR via `submit-suggestion`, or an audit ledger issue via `fleet-audit` — or posts a proactive Chat alert.

Watchdog runs execute autonomously: the agent config sets `approvals.cron_mode: approve` (see `deploy/shared/defaults/config.yaml`), so commands that would otherwise require human approval run without prompting when triggered by a scheduled job.

Full JSON is annotated on [Reference → Cron jobs](/kube-agents/reference/cron-jobs/), along with the Chat Agent profile's separate job file of `no_agent` script jobs (Cluster Agent reconciliation and first-run onboarding).

## The shipping jobs

The roster, with exact cron expressions, enabled state, and prompts, is generated from `jobs.json` on [Reference → Cron jobs](/kube-agents/reference/cron-jobs/). Six jobs ship enabled: the five fleet audits below and `github-issue-resolver`.

### The five fleet audits

Each audit reads its SOP, executes read-only checks against the fleet, writes a validated findings file, and hands it to the [`fleet-audit`](/kube-agents/skills/) skill's `audit_report.py` helper. The helper owns every git and `gh` operation and renders every body itself — the model never writes one.

| Job                           | SOP                                  | Audits                                                                    |
| ----------------------------- | ------------------------------------ | ------------------------------------------------------------------------- |
| `compliance-audit`            | `compliance_audit_sop.md`            | Security and RBAC posture across the fleet                                |
| `obtainability-audit`         | `obtainability_audit_sop.md`         | Workload reliability: requests, PDBs, HPAs, probes, scheduling rigidity   |
| `security-patch-orchestrator` | `security_patch_orchestrator_sop.md` | Version currency and upgrade-policy hygiene against the cluster's channel |
| `fleet-wide-cost-analysis`    | `fleet_wide_cost_analysis_sop.md`    | Observable waste, in resource units — no billing export required          |
| `fleet-consistency-drift`     | `fleet_consistency_drift_sop.md`     | Clusters diverging from a baseline derived from the fleet itself          |

Two properties matter more than the check lists:

- **One ledger issue per audit, plus fixes on demand.** The helper finds the audit's existing open issue by its `audit:<id>` label and rewrites it in place, commenting only on what changed since the last run. A daily audit therefore produces one issue that stays current, not thirty near-identical PRs a month. A finding whose fix is a manifest is promoted into its own narrow pull request — automatically when it is critical, otherwise when a repo writer asks for it on the ledger. See [Declarative workflow](/kube-agents/concepts/declarative-workflow/#the-fleet-audit-skill).
- **Silence is a real outcome, but it has to be earned.** A run with no findings, which resolved none either, closes the audit's ledger issue as completed and returns `[SILENT]`, so a steadily quiet fleet generates no Chat traffic. The helper decides this, not the agent: `finish` returns `silent_ok`, `true` only when nothing was new, nothing resolved, no coverage gap remained, and no remediation pull request opened or closed. Two clean runs still speak. A run that could not read part of the fleet is never silent, however clean the part it did read: it leaves the ledger open, names the gaps, and reports — "I found nothing" and "I could not look" must not arrive as the same silence. And a run that came back clean after carrying findings reports what closed, because a fleet that just got fixed is the one piece of good news these watchdogs produce.
- **Asking for a run cancels the silence.** `silent_ok` answers "would a channel want this?", and it cannot see that a person is waiting. So a job dispatched on demand — from Chat, or from a kanban card — always reports its outcome and its ledger issue URL, whatever the flag says. The Platform Agent dispatches the real job rather than re-enacting its work, then relays the result on the card, because the card summary is what reaches Chat.

### The disabled jobs

`blueprint-sync`, `policy-propagation`, `global-capacity-orchestrator`, `standardization-validator`, and `lifecycle-deprecation-manager` ship with `enabled: false`. Their SOPs are retained, but as written they cannot produce a finding on a stock install:

- `blueprint-sync` and `standardization-validator` compare clusters against a "master blueprint" / "corporate architectural patterns" document that no install provides. `fleet-consistency-drift` covers the same intent by deriving its baseline from the live fleet instead.
- `policy-propagation` reads policy templates from a `/opt/defaults/templates/` directory that is not shipped.
- `global-capacity-orchestrator` ran hourly with no defined output artifact.
- `lifecycle-deprecation-manager` overlaps `security-patch-orchestrator`.

Re-enable any of them by flipping `enabled` back to `true` and redeploying — but rewrite the SOP first, or the run will find nothing.

## Job shape

Each job in `jobs.json` follows this schema:

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

- **`id`** — stable identifier, referenced in observability and disable/enable ops. It outlives renames: `obtainability-audit` is now the Workload Reliability Audit, but the id stays put.
- **`schedule.expr`** — standard 5-field cron in the pod's local time zone (UTC unless the pod's TZ is overridden).
- **`prompt`** — verbatim message sent to the agent when the schedule fires. Governance jobs point at an SOP **relative to the profile home** (`governance/<sop>.md`), which is where `profile_scaffold.py` overlays the baked `/opt/platform-template/governance/` directory. An absolute `/opt/defaults/governance/...` path does not resolve — nothing is mounted there. The five audit prompts also state how long their SOP is and which section holds the checks, because a read that stops early lands in the preamble and the run reports a clean fleet it never inspected; a test in `audit_report.py`'s suite re-derives both numbers from the file so a stale citation fails there rather than at 06:20. What the prompts deliberately do **not** restate is the `[SILENT]` rule — each SOP's closing section states it in full, qualifiers included, and a shorter version in the prompt would both lose the qualifiers and tell the run what its answer looks like before it decides what to check.
- **`skills`** — optional array of skill names to preload. The five audits preload `fleet-audit`; `github-issue-resolver` preloads its namesake skill.
- **`enabled`** — set to `false` to disable a job without deleting its entry.
- **`deliver`** (optional) — controls chat delivery. `"all"` means every run reports back. It is set on all six enabled jobs, which is safe because each returns `[SILENT]` when it has nothing to say.

## Disabling a watchdog

Edit `cron/jobs.json`, flip `enabled` to `false`, and redeploy the workspace (`provision_08_deploy_platform_agent.sh` or `dev/dev_rebuild_agent.sh`). The change is picked up on the next agent restart.

Flip the flag; do not delete the entry. `cron/jobs.json` is image-owned configuration and live scheduler state in the same file, so start-up merges the two rather than replacing one with the other (`profile_scaffold.py`). The image wins every key it ships — which is what makes `enabled: false` take effect — and the volume keeps every key the image is silent about, so each job's run history survives a rollout and a job the operator added through `cronjob(action='create')` is not swept away by one. The cost of that second half is that a merge cannot tell an operator's job from one the image dropped, so **deleting an entry does not stop it firing** on a cluster that already has it. That is why the roster still lists the watchdogs that no longer run, disabled rather than removed.

## Adding a watchdog

1. Write a governance SOP in `agents/platform/governance/<your-sop>.md`.
2. Add a job entry to `cron/jobs.json` pointing at it as `governance/<your-sop>.md`.
3. If the job files findings, add its id to the allowlist in `agents/platform/skills/fleet-audit/scripts/audit_report.py` and preload `"skills": ["fleet-audit"]`.
4. Run `make docs-generate` — the reference table is generated, and a cron expression missing from `CRON_CADENCE` in `scripts/generate_docs.py` renders its cadence as `—`.
5. Redeploy.

Keep the schedule realistic — LLM inference on every tick has cost. Hourly or daily is the sweet spot for most SOPs; sub-15-minute cadences should have a clear justification. Stagger start minutes so two audits never contend for the same session.

Budget the run as well as the schedule. Every job shares one per-turn tool-calling budget, `agent.max_turns` in the profile's `config.yaml` — 250 for the Platform Agent, against a Hermes default of 90 the fleet audits outgrew. A run that exhausts it is stopped mid-flight and recorded as a `timed_out` event, which reads misleadingly: no clock expired, the agent simply took more steps than it was allotted, and raising any of the `HERMES_*_TIMEOUT` values will not help. The five shipping audits finish well inside 250, but an SOP that gains checks and a fleet that gains clusters both spend against it. There is no per-job override — the scheduler honours a per-job `model` but not a per-job turn budget — so the profile-wide value is the only lever.

## Where to go next

- [Reference → Cron jobs](/kube-agents/reference/cron-jobs/) — full annotated `jobs.json`.
- [Governance SOPs](/kube-agents/concepts/governance-sops/) — the playbooks these watchdogs execute.
- [Declarative workflow](/kube-agents/concepts/declarative-workflow/) — how findings become a ledger issue and remediation PRs.
