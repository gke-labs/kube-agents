# Customizable scheduled skills: shipped, chat-triggered, and tuned after deployment

## In short

Think of each of these capabilities as a routine inspection the agent knows how to do — checking
whether a fleet is ready for an upgrade, say, or looking for a cluster that has quietly drifted
from the rest. It comes with the agent already set up, so it runs on its own on a schedule and
reports what it found. If you want the same inspection right now, or just for one part of the
fleet, you ask for it in chat and get the result in the same conversation. And when the report
is not quite what you need — a check is too noisy, a threshold is wrong for one group of
clusters, something is missing — you tell the agent. It proposes a change, you agree on it
together, and from then on the inspection runs the new way, whether it was scheduled or asked
for. Nothing here requires a code change or a redeploy: the agent adjusts its own instructions
as you work with it.

Three things make that work:

1. **It runs on a schedule.** The inspection ships pre-defined and fires on its own.
2. **It runs on request.** Ask in chat at any time, for the whole fleet or a narrower scope.
3. **It can be tuned after deployment.** It is delivered as a skill — a set of instructions the
   agent follows — and Hermes lets the agent revise its own skills. You describe the change, the
   agent proposes the edit, you align on it, and every later run uses it.

## Purpose

This document describes how any capability of that shape is invoked and maintained, so each
feature doc can point here instead of restating it. The two first features cut to this shape are
the [upgrade readiness checks](upgrade-readiness-checks.md) and the
[fleet anomaly detection checks](fleet-anomaly-detection-checks.md). The rest of the document is
the technical account: what ships, how each trigger reaches the same procedure, how a
customization is made, and — the part that needs building — how it survives a pod restart and an
upgrade.

## The delivery unit

A capability of this shape ships as three files, all under `agents/platform/`:

| Part                          | What it carries                                                                                                                                                                    |
| ----------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `skills/<name>/SKILL.md`      | The procedure: how to enumerate scope, which commands to run, how to turn output into findings, the red lines the run may never cross. Loaded on demand.                           |
| `skills/<name>/criteria.yaml` | The tunable part: thresholds, scopes, exclusions, severities, extra checks, report grouping. Read by the procedure; this is the file customization edits.                          |
| `cron/jobs.json` entry        | The schedule, the skill to preload, and `deliver: "chat"`. Fires the procedure unattended (see [autonomous watchdogs](../site/src/content/docs/concepts/autonomous-watchdogs.md)). |

The split between `SKILL.md` and `criteria.yaml` is the design decision that makes tuning after
deployment safe. The procedure and its red lines stay image-owned, so an upgrade can fix a bug in
how a check is run and a customization can never remove a safety rule. The criteria are
operator-owned, so a customization survives an upgrade and an upgrade never resets a threshold
the operator tuned.
Existing governance audits express criteria inline in an SOP under `governance/`; a capability
adopting this shape moves those into `criteria.yaml` and leaves the SOP as procedure only.

Findings leave every run the same way regardless of trigger: the `fleet-audit` skill's ledger
issue per stream, remediation pull requests for findings that are a file change, and the
report relayed to chat ([`cron-report-relay.md`](cron-report-relay.md)).

## Scheduled invocation

The `cron/jobs.json` entry is on the Platform Agent's own roster, ticked once a minute by the
Chat Agent's `profile-cron-tick`. A due job runs in its own process with the Platform Agent's
persona, toolsets, the skill named in its `skills` list, and the profile's turn budget. A clean
run returns `[SILENT]` and posts nothing; a run with findings, or a run that failed, is handed to
the Chat Agent, which posts it and owns the thread the operator replies in.

The roster entry is image-owned per key (`merge_cron_store`): schedule, prompt, skills, and
`enabled` track the image on every pod start, while `last_run` and other runtime state stay
with the volume. Disabling a shipped job is therefore an image change (`enabled: false`), and an
operator who wants a different cadence creates a second job rather than editing the shipped one
— see the next section.

## Invocation on request

Three requests reach the same procedure:

- **"Run it now."** For a shipped stream, the operator asks for a run and the agent marks the job
  due (`hermes cron run <id>` on the gateway), so the next tick runs it through the identical
  path the schedule uses, with the same skill and turn budget. The agent does not re-enact the
  audit inside the chat session that took the request: that session has neither the turn budget
  nor the fresh process a run needs, and past attempts produced hand-typed all-clears. Until the
  shell-sandbox deferral in
  [`agent-shell-sandboxing.md`](agent-shell-sandboxing.md) is settled the agent cannot issue that
  command itself; it says so, and an operator with cluster access issues it against the gateway
  pod.
- **"Run it against this scope."** A narrower ask — one cluster family, one region, one check —
  is a normal chat task: the Chat Agent delegates a kanban card to the Platform Agent, which loads
  the skill and runs the procedure with the scope the operator named, and the result comes back
  in the thread. This path is available today and needs no cron machinery.
- **"Run it every Monday for family X."** The Platform Agent creates a scheduled job at runtime
  with `cronjob(action='create', deliver='chat')`, naming the skill and the scope in the prompt.
  Runtime-created jobs are not in the image, so the merge leaves them alone and they survive pod
  restarts and upgrades. This is how an operator gets a cadence or a scope the shipped entry does
  not have without changing the image.

## Customization through the skill-learning loop

Hermes' skill system is built to be edited by the agent that uses it: the `skill_manage` tool
creates and patches skill files in the profile's `skills/` directory, and upstream describes the
intended loop as autonomous skill creation after complex tasks with skills improving during use.
The Platform Agent has that toolset; the Chat Agent has it disabled on purpose, so every edit is
made by the profile that runs the procedure.

The loop for a scheduled skill is a conversation, not a silent self-edit:

1. **The operator describes the change** in chat, usually in reply to a report: "zonal skew under
   15% is noise for the batch family", "also flag PDBs with `minAvailable` equal to replicas",
   "group the cost report by region, not by cluster".
2. **The agent proposes the revision.** It reads the current `criteria.yaml` (and `SKILL.md` if
   the change is procedural), drafts the exact edit, and shows it: what will be flagged that was
   not before, what will stop being flagged, and which scheduled runs it affects.
3. **The two align.** The operator confirms, narrows, or rejects. Nothing is written before this
   step; a threshold moved without a human seeing it is a finding silently suppressed.
4. **The agent writes it** with `skill_manage`, and records the decision and its reason in
   shared memory (`scope:shared`, via the Chat Agent's Hindsight provider) so a later conversation
   can answer "why is the batch family excluded?" without re-deriving it.
5. **Every later invocation uses the revision** — the next scheduled tick, the next on-request
   run, and any runtime-created job that names the skill.

### Why persistence needs designing

In this deployment a runtime edit to a skill does not survive on its own. `profile_scaffold.py`
copies the image template over the Platform Agent's profile on every pod start, `skills/` and
`governance/` included, so a `SKILL.md` patched with `skill_manage` reverts at the next restart
and a skill directory the image does not ship is removed with it. That behaviour is deliberate —
an upgraded pod must run the image's skills, and the skill provenance check at boot exists to
say so — and it is exactly what a customization has to survive. Hence the split above:
`criteria.yaml` is the runtime-owned file, and the scaffold treats it the way it already treats
`cron/jobs.json` — a `MERGE_PATHS` entry that is read before the copy and restored after it,
with the image contributing keys it ships defaults for and the volume keeping everything the
operator set.

Three tiers of durability follow, and the agent says which one a change landed in:

| Tier     | Where the change lives                                          | Survives            | When to use                                                                            |
| -------- | --------------------------------------------------------------- | ------------------- | -------------------------------------------------------------------------------------- |
| Session  | The scope and thresholds named in one on-request run            | That run            | Trying a criterion once before adopting it                                             |
| Runtime  | `criteria.yaml` on the profile volume, merged across pod starts | Restart and upgrade | The normal outcome of the loop above                                                   |
| Reviewed | A pull request to the operator's configuration repository       | Everything          | A change the operator wants reviewed, or that edits the procedure rather than criteria |

The reviewed tier reuses what the remediation path already has: the `fleet-audit` skill opens a
narrow pull request carrying one file, and a criteria or procedure change is one file. An
operator who keeps their overrides in a repository mounts them into the pod as a ConfigMap
referenced from the `PlatformAgent` resource, and the scaffold overlays that after the image
template — the same order as the runtime tier, so the two never disagree about who wins.

A procedural change — a new check that needs a command the skill does not run — goes to the
reviewed tier by default. The agent may draft the `SKILL.md` edit and run it once on request to
show the operator what it finds, but the shipped procedure changes through review, because it is
the file that carries the red lines.

## What exists and what this adds

Already in place: the cron roster and `profile-cron-tick`, `deliver: "chat"` and the report
relay, the `fleet-audit` ledger and remediation pull requests, on-request delegation through
kanban, runtime-created cron jobs that survive restarts, the `skill_manage` tool on the Platform
Agent, and shared memory written through the Chat Agent.

This design adds: the `criteria.yaml` convention and its `MERGE_PATHS` entry in
`profile_scaffold.py`; a `SKILL.md` template that reads criteria from that file and states the
alignment step before any `skill_manage` write; the optional ConfigMap overlay for reviewed
overrides; and, per capability, the migration of inline SOP thresholds into criteria. The
on-demand `hermes cron run` gap is tracked in `agent-shell-sandboxing.md` and is not part of
this design.

## How the two first features use this

Both feature docs list criteria only, and each becomes one delivery unit of the shape above:

- **Upgrade readiness** ships as a weekly job. Its `criteria.yaml` holds the per-family target
  version and date, the add-on compatibility matrix, the PDB and surge rules, and which
  deprecation insight subtypes are blocking. On request it runs for one family ahead of that
  family's upgrade window; the post-upgrade diff is the same skill run against a cluster the
  operator names.
- **Fleet anomaly detection** ships as three jobs — guardrails daily, usage daily, costs weekly —
  sharing one skill and one `criteria.yaml` with a section per stream. The family grouping, the
  skew and quota thresholds, and the exclusions are what the customization loop will mostly
  touch; the first weeks of a pilot are expected to spend more turns tuning these than acting on
  findings.
