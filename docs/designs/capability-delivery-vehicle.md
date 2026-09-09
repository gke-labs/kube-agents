# The capability delivery vehicle: shipped, scheduled, chat-triggered, customizable, self-learning

## In short

Many of the things the agent can do for a fleet share one lifecycle. The capability comes
pre-built with the agent, so it starts working on day one. It runs on its own on a schedule and
reports what it found. If you want it right now, or just for one part of the fleet, you ask for
it in chat and get the result in the same conversation. When the result is not quite what you
need — a check is too noisy, a threshold is wrong for one group of clusters, something is
missing — you tell the agent, it proposes a change, you agree on it together, and from then on
it runs the new way. And between those conversations the agent keeps learning on its own: after
a conversation it reflects on what you corrected, what you ignored, and what you asked for
twice, and folds that into how it does the job next time. Nothing in that lifecycle requires a
code change or a redeploy.

Rather than building that lifecycle into each capability, this design builds it once, as a
delivery vehicle, and every capability plugs into it. A capability is then just the domain
knowledge — what to check, how, and what counts as a finding — and gets the five properties for
free:

1. **Pre-defined.** It ships in the image with sensible defaults and works without setup.
2. **Scheduled.** It fires on a cron schedule without anyone asking.
3. **Triggerable.** It can be run from chat at any time, for the whole fleet or a narrower scope.
4. **Customizable.** The operator changes what it does by describing the change in chat; the
   agent proposes the edit, they align, and the agent writes it for every later run.
5. **Self-learning.** The agent revises the capability on its own after reflecting on
   conversations, within limits the operator sets, and says what it changed.

The first capabilities on the vehicle are the [upgrade readiness checks](upgrade-readiness-checks.md)
and the [fleet anomaly detection checks](fleet-anomaly-detection-checks.md). The obtainability
critical user journeys under `bench/cuj/obtainability/`, and the existing governance audits under
`agents/platform/governance/`, have the same lifecycle and are the next candidates to move onto
it.

## Purpose

This document describes the vehicle: what a capability has to provide to plug in, how each of the
five properties is delivered, what already exists in the runtime, and what has to be built. Each
capability's own document lists only its checks and points here for everything about invocation
and maintenance.

## What a capability provides

A capability plugs in as one directory under `agents/platform/skills/<name>/` plus one roster
entry:

| Part                     | What it carries                                                                                                                                                                              |
| ------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `SKILL.md`               | The procedure: how to enumerate scope, which commands to run, how output becomes findings, and the red lines the run may never cross. Image-owned.                                           |
| `criteria.yaml`          | The tunable part: thresholds, scopes, exclusions, severities, extra checks, report grouping. Read by the procedure. Operator- and agent-owned; this is what customization and learning edit. |
| `learning.yaml`          | What the agent may change on its own, what needs confirmation, and what it may never touch. Shipped with conservative defaults; operator-owned after that.                                   |
| `cron/jobs.json` entry   | The schedule, the skill to preload, and `deliver: "chat"`. Fires the procedure unattended ([autonomous watchdogs](../site/src/content/docs/concepts/autonomous-watchdogs.md)).              |

The split between `SKILL.md` and `criteria.yaml` is what makes properties 4 and 5 safe. The
procedure and its red lines stay image-owned, so an upgrade can fix how a check runs and no
customization or learned change can remove a safety rule. The criteria are owned outside the
image, so a tuned threshold survives an upgrade and an upgrade never resets it. Existing
governance audits express criteria inline in an SOP; moving onto the vehicle means lifting those
into `criteria.yaml` and leaving the SOP as procedure.

Findings leave every run the same way regardless of how it was triggered: the `fleet-audit`
skill's ledger issue per stream, remediation pull requests for findings that are a file change,
and the report relayed to chat ([`cron-report-relay.md`](cron-report-relay.md)). A capability
does not build its own reporting.

## Pre-defined

The skill, its default `criteria.yaml` and `learning.yaml`, and its roster entry ship in the
image. The profile scaffold installs them on first start and refreshes the image-owned parts on
every start after that, so a new install has every capability working with defaults and an
upgraded install picks up procedure fixes without losing what the operator tuned. The default
criteria are chosen to be quiet rather than thorough: a capability's first weeks on a new fleet
are for tuning, and a report nobody can read is worse than a short one.

## Scheduled

The roster entry is on the Platform Agent's own roster, ticked once a minute by the Chat Agent's
`profile-cron-tick`. A due job runs in its own process with the Platform Agent's persona,
toolsets, the skill named in its `skills` list, and the profile's turn budget. A clean run returns
`[SILENT]` and posts nothing; a run with findings, or a run that failed, is handed to the Chat
Agent, which posts it and owns the thread the operator replies in.

The roster entry is image-owned per key (`merge_cron_store`): schedule, prompt, skills, and
`enabled` track the image on every pod start; `last_run` and other runtime state stay with the
volume. Disabling a shipped job is therefore an image change, and an operator who wants a
different cadence creates a second job rather than editing the shipped one — see the next
section.

## Triggerable

Three kinds of request reach the same procedure:

- **"Run it now."** For a shipped stream, the agent marks the job due (`hermes cron run <id>` on
  the gateway), so the next tick runs it through the identical path the schedule uses, with the
  same skill and turn budget. The agent does not re-enact the audit inside the chat session that
  took the request: that session has neither the turn budget nor the fresh process a run needs,
  and past attempts produced hand-typed all-clears. Until the shell-sandbox deferral in
  [`agent-shell-sandboxing.md`](agent-shell-sandboxing.md) is settled the agent cannot issue that
  command itself; it says so, and an operator with cluster access issues it against the gateway
  pod.
- **"Run it against this scope."** A narrower ask — one cluster family, one region, one check —
  is a normal chat task: the Chat Agent delegates a kanban card to the Platform Agent, which loads
  the skill and runs the procedure with the scope the operator named, and the result comes back
  in the thread. This path works today and needs no cron machinery.
- **"Run it every Monday for family X."** The Platform Agent creates a scheduled job at runtime
  with `cronjob(action='create', deliver='chat')`, naming the skill and the scope in the prompt.
  Runtime-created jobs are not in the image, so the merge leaves them alone and they survive pod
  restarts and upgrades.

## Customizable

Hermes' skill system is built to be edited by the agent that uses it: the `skill_manage` tool
creates and patches files in the profile's `skills/` directory, and upstream describes the
intended loop as autonomous skill creation after complex tasks with skills improving during use.
The Platform Agent has that toolset; the Chat Agent has it disabled on purpose, so every edit is
made by the profile that runs the procedure.

Explicit customization is a conversation, not a silent self-edit:

1. **The operator describes the change** in chat, usually in reply to a report: "zonal skew under
   15% is noise for the batch family", "also flag PDBs with `minAvailable` equal to replicas",
   "group the cost report by region, not by cluster".
2. **The agent proposes the revision.** It reads the current `criteria.yaml` (and `SKILL.md` if
   the change is procedural), drafts the exact edit, and shows it: what will be flagged that was
   not before, what will stop being flagged, and which scheduled runs it affects.
3. **The two align.** The operator confirms, narrows, or rejects. Nothing is written before this
   step.
4. **The agent writes it** with `skill_manage` and records the decision and its reason in shared
   memory (`scope:shared`, through the Chat Agent's Hindsight provider), so a later conversation
   can answer "why is the batch family excluded?" without re-deriving it.
5. **Every later invocation uses the revision** — the next scheduled tick, the next on-request
   run, and any runtime-created job that names the skill.

## Self-learning

Passive learning is the same edit made without being asked. After a conversation that touched a
capability — a report thread, an on-request run, a question about a finding — the agent reflects
on it: which findings the operator called noise, which it acted on, which it asked about twice,
what scope it kept narrowing to, what it asked for that the report did not have. Hermes already
nudges the agent to persist what it learned; the vehicle gives that nudge a place to land.

What the reflection produces is graded by `learning.yaml`, which every capability ships with the
same conservative defaults:

| Class of change                                                                                  | Default        |
| ------------------------------------------------------------------------------------------------ | -------------- |
| Adds context: a better explanation of a finding, a link to the runbook the operator pasted       | Apply, report  |
| Widens what is checked: a new check the operator asked for, a scope the report was missing       | Apply, report  |
| Narrows what is flagged: raises a threshold, adds an exclusion, lowers a severity                 | Propose only   |
| Changes the procedure or a red line in `SKILL.md`                                                | Never          |

"Apply, report" writes the change to `criteria.yaml` and says so in the capability's next report
— what changed, why the agent thinks so, and how to revert it — so nothing moves silently.
"Propose only" queues the change and raises it the next time the operator is in the thread, which
turns into the explicit loop above. A finding suppressed without a human seeing it is the failure
mode this table exists to prevent, so the narrowing row does not loosen on its own; the operator
can move a class to a different default per capability, and the agent may suggest doing so once
its proposals have been accepted enough times in a row.

Everything learned is also recorded in shared memory with its evidence — the conversation it came
from — so the operator can ask "what have you changed about the cost report this month, and
why?" and get an answer with sources.

## Durability

None of this survives on its own in the current deployment. `profile_scaffold.py` copies the
image template over the Platform Agent's profile on every pod start, `skills/` and `governance/`
included, so a file patched with `skill_manage` reverts at the next restart and a skill directory
the image does not ship is removed with it. That is deliberate — an upgraded pod must run the
image's skills, and the skill provenance check at boot exists to say so — and it is exactly what a
customization or a learned change has to survive. Hence the file split: `criteria.yaml` and
`learning.yaml` are the runtime-owned files, and the scaffold treats them the way it already
treats `cron/jobs.json` — a `MERGE_PATHS` entry read before the copy and restored after it, with
the image contributing keys it ships defaults for and the volume keeping everything the operator
or the agent set.

Three tiers of durability follow, and the agent says which one a change landed in:

| Tier     | Where the change lives                                                          | Survives            | When                                                                    |
| -------- | ------------------------------------------------------------------------------- | ------------------- | ----------------------------------------------------------------------- |
| Session  | The scope and thresholds named in one on-request run                            | That run            | Trying a criterion once before adopting it                              |
| Runtime  | `criteria.yaml` / `learning.yaml` on the profile volume, merged across starts   | Restart and upgrade | The normal outcome of customization and of applied learning             |
| Reviewed | A pull request to the operator's configuration repository                       | Everything          | A change the operator wants reviewed, or any change to the procedure    |

The reviewed tier reuses what the remediation path already has: the `fleet-audit` skill opens a
narrow pull request carrying one file, and a criteria or procedure change is one file. An operator
who keeps overrides in a repository mounts them into the pod as a ConfigMap referenced from the
`PlatformAgent` resource, and the scaffold overlays that after the image template — the same order
as the runtime tier, so the two never disagree about who wins. A procedural change goes to the
reviewed tier by default: the agent may draft the `SKILL.md` edit and run it once on request to
show the operator what it finds, but the shipped procedure changes through review, because it is
the file that carries the red lines.

## What exists and what the vehicle adds

Already in place: the cron roster and `profile-cron-tick`, `deliver: "chat"` and the report relay,
the `fleet-audit` ledger and remediation pull requests, on-request delegation through kanban,
runtime-created cron jobs that survive restarts, the `skill_manage` tool on the Platform Agent,
Hermes' persist-what-you-learned nudge, and shared memory written through the Chat Agent.

The vehicle adds: the `criteria.yaml` and `learning.yaml` conventions and their `MERGE_PATHS`
entries in `profile_scaffold.py`; a skill template that reads criteria from the file, states the
alignment step before any `skill_manage` write, and runs the post-conversation reflection against
`learning.yaml`; the "what I changed" paragraph in the report relay; the optional ConfigMap overlay
for reviewed overrides; and, per capability, the lift of inline SOP thresholds into criteria. The
on-demand `hermes cron run` gap is tracked in `agent-shell-sandboxing.md` and is not part of this
design.

## The first two capabilities on the vehicle

- **Upgrade readiness** ships as a weekly job. Its `criteria.yaml` holds the per-family target
  version and date, the add-on compatibility matrix, the PDB and surge rules, and which
  deprecation insight subtypes are blocking. On request it runs for one family ahead of that
  family's upgrade window; the post-upgrade diff is the same skill run against a cluster the
  operator names. Learning will mostly widen it — add-ons the operator asked about that the
  matrix did not cover.
- **Fleet anomaly detection** ships as three jobs — guardrails daily, usage daily, costs weekly —
  sharing one skill and one `criteria.yaml` with a section per stream. The family grouping, the
  skew and quota thresholds, and the exclusions are what customization and learning will mostly
  touch, and most of what they touch narrows, so the first weeks of a pilot are expected to spend
  more turns on proposals than on findings.
