# The capability delivery vehicle: shipped, scheduled, chat-triggered, customizable, self-learning

**Status:** requirements for something not yet built. The [Scope](#scope) section says which parts an
install already has; everything after it states what the vehicle must do, not how it is built.

## In short

Build the lifecycle once, plug capabilities in. Every capability on the vehicle is:

1. **Pre-defined** — ships with the agent, works on day one.
2. **Scheduled** — runs on a cron schedule.
3. **Triggerable** — runs from chat, any time, any scope.
4. **Customizable** — you describe a change in chat, agree it with the agent, and it sticks.
5. **Self-learning** — the agent refines it from your conversations, within limits you set.

## Scope

Two of the five properties exist today for the governance audits, one exists in part, and two do
not exist for anything. This table is the boundary between what an install already has and what
this document asks for.
The audits are the agent-backed entries in `agents/platform/cron/jobs.json`; the roster, not this
table, is the count.

| Property      | Already on `main`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            | What this document adds                                                                                                                                                 |
| ------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Pre-defined   | The governance audits ship in the image on the Platform Agent's roster, each backed by an SOP under `agents/platform/governance/`.                                                                                                                                                                                                                                                                                                                                                                           | Nothing. The requirement below restates the property so a new capability meets it.                                                                                      |
| Scheduled     | The roster is ticked once a minute by `profile-cron-tick`; a run reports through the chat relay (`deliver: "chat"`, or `"all"`, which includes it) and the `fleet-audit` ledger ([autonomous watchdogs](../site/src/content/docs/concepts/autonomous-watchdogs.md), [`cron-report-relay.md`](cron-report-relay.md)).                                                                                                                                                                                         | Nothing.                                                                                                                                                                |
| Triggerable   | In part. The agent creates its own schedules with `cronjob(action='create', deliver='chat')`, and those survive restarts. A scoped chat request becomes a kanban card and an ad-hoc investigation — not the audit's procedure or its ledger. A shipped stream is marked due with `hermes cron run`, which only an operator on the gateway pod can issue today ([autonomous watchdogs](../site/src/content/docs/concepts/autonomous-watchdogs.md), [`agent-shell-sandboxing.md`](agent-shell-sandboxing.md)). | **New.** "Run it now" from chat, and a scoped run that goes through the same procedure and reporting as the schedule. Requirement R3.                                   |
| Customizable  | The entrypoint refreshes `skills/` and `governance/` from the image on every pod start (skills wholesale, governance file by file), so an edit to a shipped file does not survive whatever tool made it, and the shell sandbox carries its own image copy of both ([skills](../site/src/content/docs/concepts/skills.md), [`agent-shell-sandboxing.md`](agent-shell-sandboxing.md)). Thresholds live inline in each SOP; changing one is a pull request against this repository.                             | **New.** A criteria store outside the image-owned trees, a gated write path, and a merge that keeps tuned values across restarts and upgrades. Requirements R4, R6, R7. |
| Self-learning | The Chat Agent writes shared memory; the Platform Agent's memory is read-only and it can only nominate facts for it ([`memory.md`](memory.md)). Nothing turns a conversation into a change in what an audit checks.                                                                                                                                                                                                                                                                                          | **New.** Post-conversation reflection graded by a learning policy. Requirements R5, R6.                                                                                 |

Out of scope here: how any of it is built — file names, merge rules, tool names, and which change
lands first. Those belong to the change that implements it and to the contributor documentation
under `agents/platform/` once it lands. The first two capabilities to be specified for the vehicle
are the [upgrade readiness checks](upgrade-readiness-checks.md) and the
[fleet anomaly detection checks](fleet-anomaly-detection-checks.md); each has its own Scope section
saying which of its checks already run.

## The idea in plain terms

A capability comes pre-built with the agent, so it starts working on day one. It runs on its own on
a schedule and reports what it found. If you want it right now, or just for one part of the fleet,
you ask for it in chat and get the result in the same conversation. When the result is not quite
what you need — a check is too noisy, a threshold is wrong for one group of clusters, something is
missing — you tell the agent, it proposes a change, you agree on it together, and from then on it
runs the new way. Between those conversations the agent keeps learning on its own: after a
conversation it reflects on what you corrected, what you ignored, and what you asked for twice, and
folds that into how it does the job next time, within limits you set. Nothing in that lifecycle
requires a code change or a redeploy.

Rather than building that lifecycle into each capability, the vehicle is built once and every
capability plugs into it. A capability is then just the domain knowledge: what to check, how, and
what counts as a finding.

## Requirements

Each requirement is stated for a capability on the vehicle; a capability that meets all of them is
on it.

### R1 — Pre-defined

- The capability's procedure, its default criteria, and its schedule ship in the image and work on
  a fresh install with no setup step.
- Shipped defaults are quiet rather than thorough: the first weeks on a fleet are for tuning, and a
  report nobody reads is worse than a short one.
- An upgrade delivers procedure fixes without resetting anything the operator tuned (see R6).

### R2 — Scheduled

- The capability fires on a cron schedule in its own process with the Platform Agent's persona,
  tools, and turn budget, as the governance audits do today.
- A clean run posts nothing; a run with findings, or a failed run, reaches chat through the relay
  and the operator can reply in that thread.
- Findings go to the same ledger and remediation path every audit uses. A capability does not build
  its own reporting.

### R3 — Triggerable

Three kinds of request reach the same procedure and the same reporting:

- **"Run it now"** for a shipped stream: the run happens in a fresh process through the identical
  path the schedule uses, never re-enacted inside the chat session that took the request.
- **"Run it against this scope"**: one cluster family, one region, one check, answered in the
  thread that asked.
- **"Run it every Monday for family X"**: a schedule the operator defines in chat, which survives
  restarts and upgrades without an image change.

### R4 — Customizable

- The values a procedure reads — thresholds, scopes, exclusions, severities, report grouping — are
  held apart from the procedure itself, so an operator can change them without changing the
  procedure and an upgrade can change the procedure without resetting them.
- The procedure and its red lines stay image-owned. No customization can remove a safety rule.
- Customization is a conversation: the operator describes the change; the agent shows the exact
  before/after and what will start and stop being flagged; the operator confirms, narrows, or
  rejects; only then is anything written. Every later run — scheduled or requested — uses the
  revision.
- The agent can read the current criteria at the start of a run, and the report names the
  criteria revision that produced it, so a reader knows which values a finding rests on.
- The decision and its reason are recorded where a later conversation can find them ("why is the
  batch family excluded?").

### R5 — Self-learning

- After a conversation that touched a capability — a report thread, an on-request run, a question
  about a finding — the agent reflects on what the operator called noise, acted on, asked about
  twice, kept narrowing to, or asked for that the report lacked, and proposes or applies a change
  to the criteria accordingly.
- What it may apply on its own is graded by a learning policy the operator controls, with the
  defaults in the table below. A change that narrows what is flagged is never applied without a
  human seeing it.
- Every autonomous change is announced in the capability's next report — what changed, why the
  agent thinks so, and how to revert it — so nothing moves silently.
- Everything learned is recorded with its evidence, so "what have you changed about the cost
  report this month, and why?" has an answer with sources.

| Class of change                                                                            | Default       |
| ------------------------------------------------------------------------------------------ | ------------- |
| Adds context: a better explanation of a finding, a link to the runbook the operator pasted | Apply, report |
| Widens what is checked: a new check the operator asked for, a scope the report was missing | Apply, report |
| Narrows what is flagged: raises a threshold, adds an exclusion, lowers a severity          | Propose only  |
| Changes the procedure or a red line                                                        | Never         |

Until a capability has earned otherwise, everything ships as "propose only": the agent may suggest
loosening a class once its proposals for it have been accepted enough times in a row.

### R6 — Durability

- A customized or learned value survives a pod restart and an image upgrade. An upgrade adds new
  keys with their shipped defaults and never resets a tuned one.
- The learning policy is not something an install can loosen and keep against the image: a
  release that tightens a policy reaches every install.
- Three tiers, and the agent says which one a change landed in:

| Tier     | Where the change lives                                                                | Survives            | When                                                                 |
| -------- | ------------------------------------------------------------------------------------- | ------------------- | -------------------------------------------------------------------- |
| Session  | The scope and thresholds named in one on-request run                                  | That run            | Trying a criterion once before adopting it                           |
| Runtime  | The capability's criteria on the profile's persistent volume                          | Restart and upgrade | The normal outcome of customization and of applied learning          |
| Reviewed | A pull request to a configuration repository the operator keeps, applied at pod start | Everything          | A change the operator wants reviewed, or any change to the procedure |

### R7 — Safety of the write path

- Criteria are read and written only through a path the harness controls, never by the agent's
  shell, so a policy cannot be bypassed by editing a file.
- A change is validated against the capability's declared keys and bounds before it is written; an
  unknown key or an out-of-range value is refused with a message that names the defined keys.
- The write path enforces the learning policy in code: a "never" key is refused; a "propose only"
  key is refused unless the call names who confirmed it. What code cannot verify — that the named
  person actually saw the before/after — is the agent's instruction, and the record of the name is
  what lets a reader ask.
- Every accepted change is appended to a per-capability changelog with who confirmed it, why, the
  before and after values, and the resulting revision.
- A key the image stops shipping, or re-bounds, does not wedge the capability: the value stops
  being effective and the next write cleans it up and records that it did.

## Verifying a capability is on the vehicle

| Property      | Check on a real installation                                                                                                                                                                             |
| ------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Pre-defined   | A fresh install lists the schedule and returns the shipped defaults for the criteria with no setup performed.                                                                                            |
| Scheduled     | Marking the stream due produces a run in its own process within a minute, a ledger update, and a chat report; a clean fleet produces silence.                                                            |
| Triggerable   | A scoped chat request returns a scoped report in the same thread; a chat-defined schedule is still listed after the pod restarts.                                                                        |
| Customizable  | A confirmed threshold change is reflected in the next run's findings and in the revision the report names; it is still in effect after a restart and after an image roll that also adds a new default.   |
| Self-learning | Replying "that finding is noise" produces a proposal, not a change; replying "also flag X" produces a change announced in the next report with its revert step, and a changelog entry citing the thread. |

The mechanism-versus-coincidence rule applies: each check sets a value distinctly different from
the default, observes it, and reverts it.
