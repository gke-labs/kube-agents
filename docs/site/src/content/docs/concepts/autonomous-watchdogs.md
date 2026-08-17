---
title: Autonomous watchdogs
description: Cron-scheduled jobs that make the Platform Agent proactive rather than reactive.
sidebar:
  order: 6
---

`agents/platform/cron/jobs.json` defines the governance watchdogs. Each one carries a pre-authored prompt that runs on the Platform Agent on a cron schedule. The prompts typically point at a [governance SOP](/kube-agents/concepts/governance-sops/); the agent reads the SOP, executes the procedure, and publishes what it found to your GitOps repo — an audit ledger issue via `fleet-audit`, plus a proposed PR via `submit-suggestion` where the fix is mergeable. The findings land in the repo rather than in a chat channel: the ledger issue is the report, and it is where a scheduled run is meant to be read.

Watchdog runs execute autonomously: the agent config sets `approvals.cron_mode: approve` (see `deploy/shared/defaults/config.yaml`), so commands that would otherwise require human approval run without prompting when triggered by a scheduled job.

That waives the prompt, not the scan. Every command a scheduled run issues is still put through the Tirith content scan, and a finding refuses that one command — the run carries on and can take another route. The refusal reaches the agent as an ordinary denial string and the gateway logs it (`Cron Tirith block: …`); it is not written to the execution ledger, and it reaches the job's saved output only if the run's final response mentions it. The distinction matters because the threats Tirith looks for are the ones an unattended run is least able to notice: `github-issue-resolver` reads issue text written by anyone with a GitHub account, and a homograph URL arriving that way matches no dangerous-command pattern at all. The scan also catches the shapes the pattern layer _does_ know — `curl | sh`, base64-to-shell — which matters because `approve` is precisely the instruction not to block on those. It is not total coverage: a pure-ASCII lookalike TLD and terminal escape injection get through both layers. `approvals.cron_scan: false` opts a profile out. Regardless of either setting, the hardline floor (`rm -rf /`, `mkfs`, fork bombs, and nine more), the sudo-stdin guard, and any `approvals.deny` globs still apply.

Both rosters are annotated on [Reference → Cron jobs](/kube-agents/reference/cron-jobs/), which also covers the four plumbing jobs on the Chat Agent's: the named-profile cron ticker described in [What fires the schedule](#what-fires-the-schedule), Cluster Agent reconciliation, and the two first-run onboarding steps.

## How a watchdog fires

The schedule sits on the profile that does the work, but what advances it lives elsewhere, and that is worth knowing before reading the roster.

Cron ticking is a property of a running **gateway**, and gateways are per profile. Only the `default` (Chat Agent) profile has one — the Platform Agent is reached through the kanban dispatcher, which spawns a worker per card and exits — so the gateway's own ticker never opens a schedule sitting in the Platform Agent's roster. What does open it is [`profile-cron-tick`](#what-fires-the-schedule), a job on the one store that _is_ ticked, which runs `hermes cron tick` against every named profile with work due, once a minute.

The result is an ordinary cron run: its own process, homed at the Platform Agent's profile, taking that profile's persona, toolsets, `skills`, `model` and `max_turns`, and travelling the same execute → save → deliver → mark path a hand-run `hermes cron tick` takes. Four consequences worth knowing:

- **It is not a kanban card, and there is nothing to complete.** The run has no card, no worker and no board history. Its outcome lands in the Platform Agent's own execution ledger — `cronjob(action='runs')`, or `cronjob(action='history')` for one job — and its report is delivered per the entry's `deliver`.
- **The entry's own settings apply.** `skills` are force-loaded ahead of the first turn, and a per-job `model` is honoured. `max_turns` is not per job: it comes from the profile's `config.yaml` (250 for the Platform Agent). None of the shipped jobs pins a `model`.
- **A job will not run concurrently with itself.** A tick takes `cron/.job-<id>.lock` for the length of a run, so a schedule that fires while its last run is still going is skipped rather than doubled. The skip is recorded: the ledger carries a `skipped` status alongside `completed`, `failed` and `unknown`, with a reason naming which of the five causes applied — the job was already running here, already running in another process, the interpreter was shutting down, the dispatch claim was refused, or the window was missed outright while the agent was down. A `missed_window` row's `error` text also names how many occurrences went by, where that is computable. See [Overlap and backlog](#what-fires-the-schedule).
- **One roster, not two.** No governance id may also appear on the Chat Agent's roster. Both would fire on their own schedules against the same audit, each writing the same ledger issue.

## The shipping jobs

The rosters, with exact cron expressions, enabled state, and prompts, are generated from the two `jobs.json` files on [Reference → Cron jobs](/kube-agents/reference/cron-jobs/). Eight governance jobs ship, all enabled on the Platform Agent's roster: the seven fleet audits below and `github-issue-resolver`.

### The seven fleet audits

Each audit reads its SOP, executes read-only checks against the fleet, writes a validated findings file, and hands it to the [`fleet-audit`](/kube-agents/skills/) skill's `audit_report.py` helper. The helper owns every git and `gh` operation and renders every body itself — the model never writes one.

| Job                           | SOP                                  | Audits                                                                     |
| ----------------------------- | ------------------------------------ | -------------------------------------------------------------------------- |
| `compliance-audit`            | `compliance_audit_sop.md`            | Security and RBAC posture across the fleet                                 |
| `obtainability-audit`         | `obtainability_audit_sop.md`         | Workload reliability: requests, PDBs, HPAs, probes, scheduling rigidity    |
| `security-patch-orchestrator` | `security_patch_orchestrator_sop.md` | Version currency and upgrade-policy hygiene against the cluster's channel  |
| `fleet-wide-cost-analysis`    | `fleet_wide_cost_analysis_sop.md`    | Observable waste, in resource units — no billing export required           |
| `fleet-consistency-drift`     | `fleet_consistency_drift_sop.md`     | Clusters diverging from a baseline derived from the fleet itself           |
| `ai-security-audit`           | `ai_security_audit_sop.md`           | AI inference and training workloads: exposure, model provenance, weights   |
| `stockout-prevention`         | `stockout_prevention_sop.md`         | Capacity obtainability, ComputeClass resilience, and single-zone stockouts |

Two properties matter more than the check lists:

- **One ledger issue per audit, plus fixes on demand.** The helper finds the audit's existing open issue by its `audit:<id>` label and rewrites it in place, commenting only on what changed since the last run. A daily audit therefore produces one issue that stays current, not thirty near-identical PRs a month. A finding whose fix is a manifest is promoted into its own narrow pull request — automatically when it is critical, otherwise when a repo writer asks for it on the ledger. See [Declarative workflow](/kube-agents/concepts/declarative-workflow/#the-fleet-audit-skill).
- **Silence is a real outcome, but it has to be earned.** A run with no findings, which resolved none either, closes the audit's ledger issue as completed and returns `[SILENT]`, so a steadily quiet fleet generates no Chat traffic. The helper decides this, not the agent: `finish` returns `silent_ok`, `true` only when nothing was new, nothing resolved, no coverage gap remained, and no remediation pull request opened or closed. Two clean runs still speak. A run that could not read part of the fleet is never silent, however clean the part it did read: it leaves the ledger open, names the gaps, and reports — "I found nothing" and "I could not look" must not arrive as the same silence. And a run that came back clean after carrying findings reports what closed, because a fleet that just got fixed is the one piece of good news these watchdogs produce.
- **Asking for a run cancels the silence.** `silent_ok` answers "would a channel want this?", and it cannot see that a person is waiting. So a job asked for on demand always reports its outcome and its ledger issue URL, whatever the flag says. The Platform Agent does not re-enact the audit in the session that took the request — several audits crammed into one turn share one turn budget, and on 2026-08-03 that produced five hand-typed empty findings documents and a fleet-wide all-clear from a session that issued no `kubectl` at all. It marks the job due instead, with `HERMES_HOME=/opt/data/profiles/platform hermes cron run <job-id>`, and the next `profile-cron-tick` picks it up within the minute and runs it through the identical path the schedule uses. `cronjob(action='run')` is the trap to avoid: it executes the job synchronously inside the calling session, which is the re-enactment this exists to prevent.

### The retired jobs

Five watchdogs — `blueprint-sync`, `policy-propagation`, `global-capacity-orchestrator`, `standardization-validator`, and `lifecycle-deprecation-manager` — shipped disabled for several releases and are now gone from the roster entirely. As written none could produce a finding on a stock install: two compared clusters against a "master blueprint" document no install provides, one read policy templates from an unshipped `/opt/defaults/templates/`, one ran hourly with no defined output artifact, and one overlapped `security-patch-orchestrator`.

Their SOPs are retained under `agents/platform/governance/`, so reviving one is a matter of rewriting the SOP against something a stock install actually has and re-adding the job — see [Adding a watchdog](#adding-a-watchdog). Re-adding the entry alone will not help; the SOP is why they were retired.

Retiring them took two steps, in two different releases, because `merge_cron_store` adds and overwrites but never prunes. Deleting an entry only ends the image's ability to hold the job off: a volume provisioned while the job was enabled keeps firing it. Shipping it `enabled: false` is what actually stops it, on every volume, at the next pod restart — so the five ran as **tombstones** for several releases first, and only became safe to delete once no live volume could still be carrying an enabled copy.

Deleting the entries is not the whole of the second step either. The same silence that makes the merge safe makes it forgetful: an entry the volume holds and the image no longer mentions can never again be re-enabled, disabled, or removed, and `cronjob(action='list')` reports it forever. So a deletion has to be paired with `retire_cron_jobs` — `profile_scaffold.py`, exposed as the entrypoint's `--cron-retire`, which runs after the merge and deletes the named ids from the store outright. The Platform Agent's force-sync names these five; the Chat Agent's names every governance id, which had to stop firing there the moment they resumed firing on the Platform Agent's roster. Both lists are hand-maintained and load-bearing: for as long as an id is on one it cannot be scheduled on that profile at all, because the deletion outranks anything the image ships, and taking a name off strands whatever copies remain.

## What fires the schedule

Hermes' cron ticker is a thread inside `hermes gateway run`, and everything it touches — the job store, the execution ledger, the tick lock — resolves from that process's `HERMES_HOME`. It never enumerates profiles. This image runs a single gateway, homed at `/opt/data`, so the only roster that thread ticks is the Chat Agent's. The Platform Agent's lives at `/opt/data/profiles/platform/cron/jobs.json`, which the thread never opens.

`profile-cron-tick` is what ticks it. It is a `no_agent` script job on the Chat Agent's roster — the one store that does tick — and each minute it runs `hermes cron tick` as a subprocess against every named profile with work due. Every governance watchdog fires through it, as does anything an operator schedules on a named profile's own store:

```text
gateway ticker              →  profile-cron-tick  →  hermes cron tick
(HERMES_HOME=/opt/data)        (every 1m)            (HERMES_HOME=<profile>)
```

A watchdog therefore fires through the same execute → deliver → record path a manual `hermes cron tick` takes, with its own profile's persona, toolsets, and `max_turns`. Three consequences worth knowing:

- **A minute is the floor, not the guarantee.** A named profile's schedule is only ever inspected as often as the dispatcher runs, so a cadence finer than `* * * * *` cannot be honoured there. The dispatcher is itself scheduled as `* * * * *` rather than as a one-minute `interval`, and deliberately: Hermes re-anchors an `interval` job to the moment the last run _finished_, while the gateway ticker sleeps a fixed sixty seconds _after_ each tick returns — so the next due time always lands just past the next wake and a one-minute interval job quietly fires every two. A cron expression is immune, because the completion time is snapped up to the next wall-clock minute. What is left is narrower: a dispatch that actually ran a watchdog blocks for up to 45 seconds waiting on the subprocess (see `DEFAULT_BUDGET_SECONDS` in `profile_cron_tick.py`), and that usually costs the minute after it, so roughly one dispatch in twenty is collapsed and it is almost always the minute following a watchdog run. Quiet minutes are on time. Treat every schedule below as accurate to about a minute, which is well inside the sweet spot in [Adding a watchdog](#adding-a-watchdog) and does not constrain the shipping roster. Nothing here is latency-critical; if something ever is, it needs its own timer, not a finer cron expression.
- **Overlap and backlog are `tick()`'s problem, not the dispatcher's.** A tick takes an exclusive lock on the profile's store (`cron/.tick.lock`) while it decides what is due and advances every due job's `next_run_at`, then releases it once every due job has been dispatched rather than holding it until they finish — so an agent down for two days runs each missed daily audit once on return, not once per missed day. Overlap is held per job rather than per profile: a job takes `cron/.job-<id>.lock` for as long as it runs, so a second dispatch will not start the same job twice, but it will start a _different_ one. That distinction matters here because each platform tick is a separate `hermes cron tick` process. Holding the profile lock across execution — the upstream default — meant a fleet audit blocked every dispatch for its whole run; three `github-issue-resolver` firings were measured 418s, 179s and 1142s late behind one, each recovering within seconds of the audit finishing.
- **A broken ticker is loud.** Dispatches are silent on a quiet minute, but a tick that fails is recorded as a failed `profile-cron-tick` run with the subprocess output in `<profile-home>/cron/tick.log` — the one failure mode this job must not have is the silence it exists to end. Individual watchdog runs stay where every other run is: the profile's own execution ledger, and `cronjob(action='history')`. That ledger is pruned per job rather than per board — 1000 rows each (`MAX_TERMINAL_EXECUTIONS_PER_JOB`), then a 5000-row sweep across the board that may never take a job's newest 50, so the floor outranks the ceiling and the real bound is 5000 plus 50 per job id. The distinction is the difference between a usable history and none: pruning to the newest 1000 rows across all jobs let the minute-ly ticker evict a daily watchdog's only row inside seventeen hours, so the history of the job you would actually go looking at was always empty.

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
- **`prompt`** — what the run is asked to do, copied verbatim into the turn. Governance jobs point at an SOP **relative to the profile home** (`governance/<sop>.md`), which is where `profile_scaffold.py` overlays the baked `/opt/platform-template/governance/` directory. An absolute `/opt/defaults/governance/...` path does not resolve — nothing is mounted there. The seven audit prompts also state how long their SOP is and which section holds the checks, because a read that stops early lands in the preamble and the run reports a clean fleet it never inspected; a test in `audit_report.py`'s suite re-derives both numbers from the file so a stale citation fails there rather than at 06:20. What the prompts deliberately do **not** restate is the `[SILENT]` rule — each SOP's closing section states it in full, qualifiers included, and a shorter version in the prompt would both lose the qualifiers and tell the run what its answer looks like before it decides what to check.
- **`skills`** — the skills the work needs. The scheduler prepends each one's content to the prompt, force-loading it ahead of the first turn rather than leaving the load to the model's discretion. The seven audits use `fleet-audit`; `github-issue-resolver` uses its namesake skill.
- **`no_agent`** and **`script`** — a subprocess instead of an LLM turn, used by the four plumbing jobs on the Chat Agent's roster (`script` resolves in that profile's `scripts/`). The governance watchdogs omit both: they are model runs.
- **`enabled`** — set to `false` to disable a job without deleting its entry.
- **`deliver`** — where the run's outcome goes: `"all"` sends it to the configured target, `"local"` resolves to no target at all and drops it. All eight governance jobs use `"all"`, so a watchdog that has stopped working is visible rather than indistinguishable from a quiet fleet.

## Disabling a watchdog

Flip `enabled` to `false` in `agents/platform/cron/jobs.json`. The scheduler honours the flag directly: the job stops being due, and nothing runs.

Flip the flag; do not delete the entry. An id can be dropped from the roster only once no live cluster still needs the image to hold the job off, which is the path the five [retired watchdogs](#the-retired-jobs) took. Deleting is never the whole step: the same release must also name the id in the platform force-sync's `--cron-retire` (`deploy/shared/docker-entrypoint.sh`), or the volume keeps a disabled entry no later image can reach.

**The Platform Agent's roster travels.** That profile is scaffolded by `profile_scaffold.py` on every start-up, and its `merge_cron_store` merges the image's roster over the volume's unfiltered — the image wins every key it ships, the volume keeps every key it does not. So redeploying an entry with `enabled: false` switches it off on every existing cluster at the next pod restart, and a stale enabled copy cannot outlive the release that retired it. A job an operator added by hand survives, because the merge never prunes — unless its id is on the force-sync's `--cron-retire` list, which is applied after the merge and deletes outright.

**The Chat Agent's roster travels too, by a different road.** The Chat Agent is the `default` profile, which is not scaffolded: it lives at `$HERMES_HOME` directly and the entrypoint seeds it with `cp -ru /opt/defaults/. "$TARGET_DIR/"` (`deploy/shared/docker-entrypoint.sh`, step 2). `cron/` is in neither force-sync list — step 2a covers `SOUL.md`, `AGENTS.md` and `CAPABILITIES.md`, step 2b covers `scripts/` — and since the scheduler writes `last_run` into the volume's copy on every tick, that copy's timestamp is permanently ahead of the image's, and `cp -u` skips it for good. Step 2c-bis closes that gap: `cron_jobs_sync.py` reconciles `$HERMES_HOME/cron/jobs.json` against the shipped roster by job id, per key, under the rule `merge_cron_store` already applies on the other side — the image wins every key it ships, `enabled` among them, and a key it ships nothing for stays as the volume had it. Two rosters obeying opposite merge rules would be a trap for whoever edits either. Step 2c, immediately before it, forces exactly one id (`--cron-jobs "profile-cron-tick"`); that narrowness is a deliberate subset of the same rule rather than a second policy for the same file, because 2c is the call that also carries `--cron-retire` and an unfiltered merge there would resurrect the two onboarding jobs `bootstrap_delivery.py` deletes once the first-run report lands. What stops 2c-bis resurrecting them is a ledger instead: `$HERMES_HOME/.cron_jobs_installed` records every id the script has installed, so an id missing from the volume that the ledger already knows about was removed on purpose, not shipped new, and is never reinstalled.

Three consequences worth stating plainly:

- **A hand-added job still survives every upgrade.** Neither step deletes an entry the image does not declare, and neither touches a key the image does not ship, so an operator's own job and the scheduler's own state both come through untouched.
- **A hand-edit on a live pod does not survive one.** Editing `enabled` in `$HERMES_HOME/cron/jobs.json` on the PVC holds only until the next restart, which is precisely when the reconcile takes the image's value back — silently, since reconciling a key to the image is the expected path and logs nothing job-specific. The image is the declaration of record; edit `agents/chat/defaults/cron/jobs.json` and roll. If you must stop a job before the next roll can ship, treat the PVC edit as a stopgap and land the image change behind it.
- **`deliver` is the one shipped key the runtime keeps.** Onboarding rewrites it to `origin` on the delivery job, so taking the image's value back would send the first-run report nowhere.

The `cronjob` tool is not a route to either roster: it is denied to the Chat Agent (`agents/chat/config.yaml`), and the Platform Agent's copy addresses that profile's own store, not this one.

## Adding a watchdog

1. Write a governance SOP in `agents/platform/governance/<your-sop>.md`.
2. Add a job entry to `agents/platform/cron/jobs.json` pointing at it as `governance/<your-sop>.md`. That is the Platform Agent's own roster, fired by [`profile-cron-tick`](#what-fires-the-schedule), so the run gets that profile's persona, toolsets and `skills`. Do not also add the id to the Chat Agent's roster, or the job runs twice.
3. If the job files findings, add its id to the allowlist in `agents/platform/skills/fleet-audit/scripts/audit_report.py` and set `"skills": ["fleet-audit"]`.
4. Run `make docs-generate` — the reference table is generated from both rosters, and a cron expression missing from `CRON_CADENCE` in `scripts/generate_docs.py` renders its cadence as `—`.
5. Redeploy (`provision_08_deploy_platform_agent.sh`, or `dev/dev_rebuild_agent.sh` for a dev workspace). The entry lands on the next pod restart, on new and existing clusters alike — the Platform Agent's roster is merged in full, so no PVC edit is needed. A job on the Chat Agent's roster lands the same way, through step 2c-bis's `cron_jobs_sync.py`, which reconciles that roster in full too; step 2c's narrower `--cron-jobs` allowlist is not a gate a new job has to pass.

Keep the schedule realistic — LLM inference on every tick has cost. Hourly or daily is the sweet spot for most SOPs; sub-15-minute cadences should have a clear justification. Stagger start minutes so two audits never contend for the same session.

Budget the run as well as the schedule. Every job shares one per-turn tool-calling budget, `agent.max_turns` in the profile's `config.yaml` — 250 for the Platform Agent, against a Hermes default of 90 the fleet audits outgrew. A run that exhausts it is stopped mid-flight and recorded as a `timed_out` event, which reads misleadingly: no clock expired, the agent simply took more steps than it was allotted, and raising any of the `HERMES_*_TIMEOUT` values will not help. The seven shipping audits finish well inside 250, but an SOP that gains checks and a fleet that gains clusters both spend against it. There is no per-job override: the scheduler honours a per-job `model` but not a per-job turn budget, so the profile-wide value is the only lever.

## Where to go next

- [Reference → Cron jobs](/kube-agents/reference/cron-jobs/) — full annotated `jobs.json`.
- [Governance SOPs](/kube-agents/concepts/governance-sops/) — the playbooks these watchdogs execute.
- [Declarative workflow](/kube-agents/concepts/declarative-workflow/) — how findings become a ledger issue and remediation PRs.
