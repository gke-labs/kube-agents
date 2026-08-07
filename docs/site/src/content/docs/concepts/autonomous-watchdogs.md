---
title: Autonomous watchdogs
description: Cron-scheduled jobs that make the Platform Agent proactive rather than reactive.
sidebar:
  order: 6
---

`agents/chat/defaults/cron/jobs.json` defines the scheduled jobs. Each one carries a pre-authored prompt that reaches the Platform Agent on a cron schedule. The prompts typically point at a [governance SOP](/kube-agents/concepts/governance-sops/); the agent reads the SOP, executes the procedure, and either publishes to your GitOps repo — a proposed PR via `submit-suggestion`, or an audit ledger issue via `fleet-audit` — or posts a proactive Chat alert.

Watchdog runs execute autonomously: the agent config sets `approvals.cron_mode: approve` (see `deploy/shared/defaults/config.yaml`), so commands that would otherwise require human approval run without prompting when triggered by a scheduled job.

Full JSON is annotated on [Reference → Cron jobs](/kube-agents/reference/cron-jobs/), which also covers the three non-governance jobs in the same file: Cluster Agent reconciliation and the two first-run onboarding steps.

## How a watchdog fires

The schedule lives in one profile and the work happens in another, and it is worth knowing why before reading the roster.

Cron ticking is a property of a running **gateway**, and gateways are per profile. Only the `default` (Chat Agent) profile has one — the Platform Agent is reached through the kanban dispatcher, which spawns a worker per card and exits — so a schedule sitting in the Platform Agent's own roster has nothing to advance it. That roster is empty for exactly this reason; every job is in the Chat Agent's.

The Chat Agent cannot run an audit itself, and is not asked to. Its toolsets are deliberately stripped to `mcp-router`, `kanban` and `memory` (`agents/chat/config.yaml`), with no `terminal` and no `skills`. So every governance job is marked `no_agent`: a plain subprocess that prompts no model, files one kanban card assigned to `platform`, and exits. The card carries the job's `prompt` — read off the roster entry at tick time, never restated — and the gateway dispatcher spawns a Platform Agent worker on it with the full platform toolset.

`agents/chat/scripts/platform_cron_dispatch.py` is the script behind every job, invoked through a one-line `dispatch_<id>.py` wrapper that supplies the job id (the scheduler runs a script with no arguments, so the wrapper is the only place the id can live). Its module docstring is the reference for the rest. Four behaviours are worth knowing here:

- **A tick can decline to file.** A card of the same title still in flight means the last run has outlasted its own schedule, and the tick skips rather than run the audit concurrently with itself. `blocked` does not count as in-flight — it is waiting on a person, and one bad run must not switch the audit off indefinitely.
- **The card completes silently.** Chat notifications come from a subscription row written at `kanban_create` time from the originating chat session, and a cron script has no session — so the card's completion posts nowhere. That suits an audit whose deliverable is its ledger issue. To get a per-card message, add the subscription (`agents/platform/scripts/kanban_notify_propagate.py`), not a change to the script.
- **The worker is a kanban worker, not a cron run.** It does not inherit the entry's `model` or `skills`; it gets the platform profile's own settings, including `agent.max_turns: 250`. None of the shipped jobs pins a `model`, and the card body restates `skills` so the field is not merely decorative.
- **Finished cards are swept.** `github-issue-resolver` alone files forty-eight a day, so a filing tick also archives this job's finished cards past the newest three. Archiving is not deletion (`kanban list --archived`; `kanban gc` reclaims the workspaces), blocked cards are never swept, and no other job's history is touched.

## The shipping jobs

The roster, with exact cron expressions, enabled state, and prompts, is generated from `jobs.json` on [Reference → Cron jobs](/kube-agents/reference/cron-jobs/). Six jobs ship, all enabled: the five fleet audits below and `github-issue-resolver`.

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
- **Asking for a run cancels the silence.** `silent_ok` answers "would a channel want this?", and it cannot see that a person is waiting. So a job asked for on demand always reports its outcome and its ledger issue URL, whatever the flag says. The Platform Agent does not re-enact the audit in the session that took the request: it runs `platform_cron_dispatch.py <job-id>` — the same code path the schedule uses, so the card gets the same prompt and the same in-flight guard — and copies its own chat subscription onto the new card so the report reaches the person who asked.

### The retired jobs

Five watchdogs — `blueprint-sync`, `policy-propagation`, `global-capacity-orchestrator`, `standardization-validator`, and `lifecycle-deprecation-manager` — shipped disabled for several releases and are no longer in the roster. As written none could produce a finding on a stock install: two compared clusters against a "master blueprint" document no install provides, one read policy templates from an unshipped `/opt/defaults/templates/`, one ran hourly with no defined output artifact, and one overlapped `security-patch-orchestrator`.

Their SOPs are retained under `agents/platform/governance/`, so reviving one is a matter of rewriting the SOP against something a stock install actually has and re-adding the job — see [Adding a watchdog](#adding-a-watchdog). Re-adding the entry alone will not help; the SOP is why they were retired.

On a cluster provisioned before they were dropped, the five entries remain on the Platform Agent profile's `profiles/platform/cron/jobs.json` in the disabled state that release left them in — see [Disabling a watchdog](#disabling-a-watchdog) for why. They stay off; the image simply no longer has a say.

The six live watchdogs left the same file when they moved to the Chat Agent's roster, and they left it in one step rather than through a disabled release. On an upgraded cluster their old entries survive there too, still marked enabled. Nothing comes of it — that profile has no gateway and so no ticker, which is the reason the jobs moved — but `cronjob(action='list')` run against the Platform Agent will list them, with whatever prompt the release that shipped them carried. The roster that decides when an audit runs is the Chat Agent's, and only that one.

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
  "prompt": "Run the daily fleet security and RBAC posture audit. Read the SOP at 'governance/compliance_audit_sop.md' in your profile home — all 406 lines of it, before you run anything. Its eleven checks are section 2, lines 102-314, so a read that stops early skips almost the entire audit and reports a clean fleet it never looked at. Then execute it exactly, using the fleet-audit skill to open and close the audit run.",
  "skills": ["fleet-audit"],
  "no_agent": true,
  "script": "dispatch_compliance_audit.py",
  "enabled": true,
  "deliver": "all"
}
```

- **`id`** — stable identifier, referenced in observability and disable/enable ops. It outlives renames: `obtainability-audit` is now the Workload Reliability Audit, but the id stays put.
- **`schedule.expr`** — standard 5-field cron in the pod's local time zone (UTC unless the pod's TZ is overridden).
- **`prompt`** — the body of the kanban card the tick files, copied verbatim. Governance jobs point at an SOP **relative to the profile home** (`governance/<sop>.md`), which is where `profile_scaffold.py` overlays the baked `/opt/platform-template/governance/` directory. An absolute `/opt/defaults/governance/...` path does not resolve — nothing is mounted there. The five audit prompts also state how long their SOP is and which section holds the checks, because a read that stops early lands in the preamble and the run reports a clean fleet it never inspected; a test in `audit_report.py`'s suite re-derives both numbers from the file so a stale citation fails there rather than at 06:20. What the prompts deliberately do **not** restate is the `[SILENT]` rule — each SOP's closing section states it in full, qualifiers included, and a shorter version in the prompt would both lose the qualifiers and tell the run what its answer looks like before it decides what to check.
- **`skills`** — the skills the work needs. A `no_agent` tick prompts no model, so the scheduler ignores the field; the dispatch script reads it instead and passes each name to `kanban create` as `--skill`, which the gateway expands to `--skills` when it spawns the worker, preloading the skill's text before the worker's first turn. That is the same force-load the cron scheduler performed by prepending skill content to the prompt — naming the skill in the card body alone would have left loading it to the worker's discretion. The body names them too, as the board's record of what the job expected. The five audits use `fleet-audit`; `github-issue-resolver` uses its namesake skill.
- **`no_agent`** and **`script`** — the tick is a subprocess, not an LLM turn. `script` names a `dispatch_<id>.py` wrapper in `agents/chat/scripts/`, which supplies the job id to `platform_cron_dispatch.py`.
- **`enabled`** — set to `false` to disable a job without deleting its entry.
- **`deliver`** — where a tick's stdout goes. A successful tick prints nothing and is delivered as a silent run, so this only matters on failure: `"all"` sends the watchdog alert to the configured target, while `"local"` resolves to no target at all and would drop it. All six governance jobs use `"all"`, so a bridge that stops filing cards is visible rather than indistinguishable from a quiet fleet.

## Disabling a watchdog

Edit `agents/chat/defaults/cron/jobs.json`, flip `enabled` to `false`, and redeploy the workspace (`provision_08_deploy_platform_agent.sh` or `dev/dev_rebuild_agent.sh`). The change is picked up on the next agent restart. One flag, one file: the scheduler stops ticking the job, so nothing is filed and nothing runs.

Flip the flag; do not delete the entry. `cron/jobs.json` is image-owned configuration and live scheduler state in the same file, so start-up merges the two rather than replacing one with the other (`profile_scaffold.py`). The image wins every key it ships — which is what makes `enabled: false` take effect — and the volume keeps every key the image is silent about, so each job's run history survives a rollout and a job the operator added through `cronjob(action='create')` is not swept away by one. The cost of that second half is that a merge cannot tell an operator's job from one the image dropped, so **deleting an entry does not stop it firing** on a cluster that already has it — it only ends the image's ability to hold it off.

Deleting an id from the roster is therefore a second step, not the first one. Ship `enabled: false`, let every live cluster merge that state, and only then drop the entry: from that point the volume's own copy keeps the job off with no help from the image. That is the path the five [retired watchdogs](#the-retired-jobs) took.

## Adding a watchdog

1. Write a governance SOP in `agents/platform/governance/<your-sop>.md`.
2. Add a job entry to `agents/chat/defaults/cron/jobs.json` pointing at it as `governance/<your-sop>.md` — that is the Chat Agent's roster, and the only one that ticks. Adding it to `agents/platform/cron/jobs.json` instead is the one mistake this page exists to prevent: nothing there ever fires.
3. Copy one of the `dispatch_*.py` wrappers in `agents/chat/scripts/`, changing only the job id, and point the entry's `script` at it. `test_platform_cron_dispatch.py` fails if a job has no wrapper, or a wrapper no job.
4. If the job files findings, add its id to the allowlist in `agents/platform/skills/fleet-audit/scripts/audit_report.py` and set `"skills": ["fleet-audit"]`.
5. Run `make docs-generate` — the reference table is generated, and a cron expression missing from `CRON_CADENCE` in `scripts/generate_docs.py` renders its cadence as `—`.
6. Redeploy.

Keep the schedule realistic — LLM inference on every tick has cost. Hourly or daily is the sweet spot for most SOPs; sub-15-minute cadences should have a clear justification. Stagger start minutes so two audits never contend for the same session.

Budget the run as well as the schedule. Every job shares one per-turn tool-calling budget, `agent.max_turns` in the profile's `config.yaml` — 250 for the Platform Agent, against a Hermes default of 90 the fleet audits outgrew. A run that exhausts it is stopped mid-flight and recorded as a `timed_out` event, which reads misleadingly: no clock expired, the agent simply took more steps than it was allotted, and raising any of the `HERMES_*_TIMEOUT` values will not help. The five shipping audits finish well inside 250, but an SOP that gains checks and a fleet that gains clusters both spend against it. There is no per-job override: the work happens in a kanban worker, which reads the profile's `config.yaml` and knows nothing about the roster entry that filed its card, so the profile-wide value is the only lever.

## Where to go next

- [Reference → Cron jobs](/kube-agents/reference/cron-jobs/) — full annotated `jobs.json`.
- [Governance SOPs](/kube-agents/concepts/governance-sops/) — the playbooks these watchdogs execute.
- [Declarative workflow](/kube-agents/concepts/declarative-workflow/) — how findings become a ledger issue and remediation PRs.
