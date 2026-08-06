---
title: Autonomous watchdogs
description: Cron-scheduled jobs that make the Platform Agent proactive rather than reactive.
sidebar:
  order: 6
---

`agents/platform/cron/jobs.json` defines the scheduled jobs. Each one fires a pre-authored prompt at the Platform Agent on a cron schedule. The prompts typically point at a [governance SOP](/kube-agents/concepts/governance-sops/); the agent reads the SOP, executes the procedure, and either publishes to your GitOps repo — a proposed PR via `submit-suggestion`, or an audit ledger issue via `fleet-audit` — or posts a proactive Chat alert.

Watchdog runs execute autonomously: the agent config sets `approvals.cron_mode: approve` (see `deploy/shared/defaults/config.yaml`), so commands that would otherwise require human approval run without prompting when triggered by a scheduled job.

Full JSON is annotated on [Reference → Cron jobs](/kube-agents/reference/cron-jobs/), along with the Chat Agent profile's separate job file of `no_agent` script jobs (Cluster Agent reconciliation, first-run onboarding, and the dispatch triggers below).

## How a watchdog fires

The schedule and the work live in different profiles, and it is worth knowing why before reading the roster.

Cron ticking is a property of a running **gateway**, and gateways are per profile. Only the `default` (Chat Agent) profile has one — the Platform Agent is reached through the kanban dispatcher, which spawns a worker per card and exits. A schedule sitting in the Platform Agent's own roster has nothing to advance it.

Moving the jobs verbatim into the Chat Agent's roster would not work either, and for a reason worth stating plainly: that profile's toolsets are deliberately stripped to `mcp-router`, `kanban` and `memory` (`agents/chat/config.yaml`). It has no `terminal`, so no kubectl or gcloud, and no `skills`, so `"skills": ["fleet-audit"]` could not even resolve. The front door is not allowed to touch the fleet, and that restriction is the point of it.

So what lives in the Chat Agent's roster is the **trigger**, not the work. Each Platform Agent job has a matching `dispatch-<id>` entry there, on the same cron expression, marked `no_agent` — a plain subprocess run outside the toolset denylist, prompting no model and spending no tokens. It calls `agents/chat/scripts/platform_cron_dispatch.py`, which files one kanban card assigned to `platform`. The dispatcher spawns a Platform Agent worker with the full toolset, and the card asks for exactly one thing: `cronjob(action='run', job_id='<id>')`.

The card **names** the job rather than carrying a copy of its prompt. `agents/platform/cron/jobs.json` stays the single definition of what each audit does, and the dispatched run gets that job's own prompt, skills, model and turn budget — the same reason `agents/platform/AGENTS.md` gives for never re-enacting a scheduled job's work by hand. Setting `enabled: false` there stops the trigger too: the script reads the Platform Agent's roster on every tick and files nothing for a disabled job.

Three consequences follow from the bridge, and none is incidental:

- **A tick can decline to run.** Before filing, the script checks the board for a card of the same title still in flight, so a fleet audit that outlasts its own schedule does not get a second copy of itself. A `blocked` card is not counted as in-flight — it is waiting on a person, and letting one bad run switch the audit off indefinitely is the failure this bridge exists to end.
- **The card is silent; the run it dispatches is not.** The gateway notifier delivers from `kanban_notify_subs`, whose rows are written at `kanban_create` time from the originating chat session — and a cron script has no session, so the card's own completion posts nowhere. The run itself is unaffected: `cronjob(action='run')` goes through `run_one_job`, the same execute→save→deliver body a scheduler tick uses, so the job's `deliver` setting and its `[SILENT]` handling behave exactly as they would have. What is lost is a second, redundant Chat message summarising the card. If you do want one — a per-card progress line in a specific thread — the missing piece is a subscription row, not a change to the script; `agents/platform/scripts/kanban_notify_propagate.py` writes exactly that.
- **Each tick also takes a card away.** `github-issue-resolver` alone files forty-eight cards a day, so a bridge that only ever created them would bury the board within a week. The same listing that answers "is one still in flight?" also names this job's finished cards, and everything past the newest three is archived. Archiving is not deletion: those cards stay readable under `kanban list --archived`, and `kanban gc` is what eventually reclaims their workspaces. Blocked cards are never swept up — one is the only durable sign that a job needs a person, and this bridge deliberately keeps scheduling past it. Nor is another job's history touched; the sweep is scoped to cards with this job's title.

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
- **Asking for a run cancels the silence.** `silent_ok` answers "would a channel want this?", and it cannot see that a person is waiting. So a job dispatched on demand — from Chat, or from a kanban card — always reports its outcome and its ledger issue URL, whatever the flag says. The Platform Agent dispatches the real job rather than re-enacting its work, then relays the result on the card, because the card summary is what reaches Chat.

### The retired jobs

Five watchdogs — `blueprint-sync`, `policy-propagation`, `global-capacity-orchestrator`, `standardization-validator`, and `lifecycle-deprecation-manager` — shipped disabled for several releases and are no longer in the roster. As written none could produce a finding on a stock install: two compared clusters against a "master blueprint" document no install provides, one read policy templates from an unshipped `/opt/defaults/templates/`, one ran hourly with no defined output artifact, and one overlapped `security-patch-orchestrator`.

Their SOPs are retained under `agents/platform/governance/`, so reviving one is a matter of rewriting the SOP against something a stock install actually has and re-adding the job — see [Adding a watchdog](#adding-a-watchdog). Re-adding the entry alone will not help; the SOP is why they were retired.

On a cluster provisioned before they were dropped, the five entries remain on the volume's `cron/jobs.json` in the disabled state that release left them in — see [Disabling a watchdog](#disabling-a-watchdog) for why. They stay off; the image simply no longer has a say.

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

One flag is enough: leave the `dispatch-<id>` trigger alone. `platform_cron_dispatch.py` reads the Platform Agent's roster on every tick and files nothing for a job it finds disabled, and `cronjob(action='run')` refuses a disabled job in any case. Flipping the flag in the Platform Agent's file is the only edit, and it is the file whose per-key merge makes `enabled: false` survive a rollout.

Flip the flag; do not delete the entry. `cron/jobs.json` is image-owned configuration and live scheduler state in the same file, so start-up merges the two rather than replacing one with the other (`profile_scaffold.py`). The image wins every key it ships — which is what makes `enabled: false` take effect — and the volume keeps every key the image is silent about, so each job's run history survives a rollout and a job the operator added through `cronjob(action='create')` is not swept away by one. The cost of that second half is that a merge cannot tell an operator's job from one the image dropped, so **deleting an entry does not stop it firing** on a cluster that already has it — it only ends the image's ability to hold it off.

Deleting an id from the roster is therefore a second step, not the first one. Ship `enabled: false`, let every live cluster merge that state, and only then drop the entry: from that point the volume's own copy keeps the job off with no help from the image. That is the path the five [retired watchdogs](#the-retired-jobs) took.

## Adding a watchdog

1. Write a governance SOP in `agents/platform/governance/<your-sop>.md`.
2. Add a job entry to `cron/jobs.json` pointing at it as `governance/<your-sop>.md`.
3. Give it a trigger, or it will never fire — see [How a watchdog fires](#how-a-watchdog-fires). Copy one of the `dispatch_*.py` wrappers in `agents/chat/scripts/`, changing only the job id, and add a matching `dispatch-<id>` entry to `agents/chat/defaults/cron/jobs.json` on the same schedule. `test_platform_cron_dispatch.py` fails if the two rosters disagree.
4. If the job files findings, add its id to the allowlist in `agents/platform/skills/fleet-audit/scripts/audit_report.py` and preload `"skills": ["fleet-audit"]`.
5. Run `make docs-generate` — the reference table is generated, and a cron expression missing from `CRON_CADENCE` in `scripts/generate_docs.py` renders its cadence as `—`.
6. Redeploy.

Keep the schedule realistic — LLM inference on every tick has cost. Hourly or daily is the sweet spot for most SOPs; sub-15-minute cadences should have a clear justification. Stagger start minutes so two audits never contend for the same session.

Budget the run as well as the schedule. Every job shares one per-turn tool-calling budget, `agent.max_turns` in the profile's `config.yaml` — 250 for the Platform Agent, against a Hermes default of 90 the fleet audits outgrew. A run that exhausts it is stopped mid-flight and recorded as a `timed_out` event, which reads misleadingly: no clock expired, the agent simply took more steps than it was allotted, and raising any of the `HERMES_*_TIMEOUT` values will not help. The five shipping audits finish well inside 250, but an SOP that gains checks and a fleet that gains clusters both spend against it. There is no per-job override — the scheduler honours a per-job `model` but not a per-job turn budget — so the profile-wide value is the only lever.

## Where to go next

- [Reference → Cron jobs](/kube-agents/reference/cron-jobs/) — full annotated `jobs.json`.
- [Governance SOPs](/kube-agents/concepts/governance-sops/) — the playbooks these watchdogs execute.
- [Declarative workflow](/kube-agents/concepts/declarative-workflow/) — how findings become a ledger issue and remediation PRs.
