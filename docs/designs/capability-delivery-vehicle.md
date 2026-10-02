# The capability delivery vehicle: shipped, scheduled, triggerable, customizable, self-learning

**Status:** requirements for something not yet built. The [Scope](#scope) section says which parts an
install already has; everything after it states what the vehicle must do, not how it is built.

## In short

Build the lifecycle once, plug capabilities in. Every capability on the vehicle is:

1. **Pre-defined** — ships with the agent, works on day one.
2. **Scheduled** — runs on a cron schedule: an audit on the schedule it ships with, an advisory
   capability on the re-check its user opts into.
3. **Triggerable** — the same procedure also runs when asked from chat or when an event arrives; any
   time, any scope.
4. **Customizable** — you describe a change in chat, agree it with the agent, and it sticks.
5. **Self-learning** — the agent refines it from your conversations, within limits you set.

## Scope

Two of the five properties exist today for the governance audits, one exists in part, and two do
not exist for anything. This table is the boundary between what an install already has and what
this document asks for.
The audits are the agent-backed entries in `agents/platform/cron/jobs.json`; the roster, not this
table, is the count.

| Property      | Already on `main`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 | What this document adds                                                                                                                                                                                                                                                                         |
| ------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Pre-defined   | The governance audits ship in the image on the Platform Agent's roster, each backed by an SOP under `agents/platform/governance/`.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                | Nothing. The requirement below restates the property so a new capability meets it.                                                                                                                                                                                                              |
| Scheduled     | The roster is ticked once a minute by `profile-cron-tick`; a run reports through the chat relay (`deliver: "chat"`, or `"all"`, which includes it) and the `fleet-audit` ledger ([autonomous watchdogs](../site/src/content/docs/concepts/autonomous-watchdogs.md), [`cron-report-relay.md`](cron-report-relay.md)).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              | Nothing for an audit. An advisory capability's opt-in re-check reporting into the thread that asked is new (R2): today every scheduled report lands in the relay's own per-job session.                                                                                                         |
| Triggerable   | In part. **Chat:** the agent creates its own schedules with `cronjob(action='create', deliver='chat')`, and those survive restarts; a scoped chat request becomes a kanban card and an ad-hoc investigation — not the audit's procedure or its ledger; a shipped stream is marked due with `hermes cron run`, which only an operator on the gateway pod can issue today ([autonomous watchdogs](../site/src/content/docs/concepts/autonomous-watchdogs.md), [`agent-shell-sandboxing.md`](agent-shell-sandboxing.md)). **Event:** the adapters that exist: The Kubernetes event watcher (`k8s-operator/cmd/k8s-event-watcher/`) filters watched event reasons, deduplicates, and opens one session per incident through the Session KV server, which applies a severity gate and a daily alert ceiling; the Chat Agent then files one kanban card to that cluster's Cluster Agent and the result posts to the incident thread ([`agents/platform/docs/session_management.md`](../../agents/platform/docs/session_management.md)). The Pub/Sub platform adapter ([`agentplugins/pubsub-platform/`](../../agentplugins/pubsub-platform/README.md)) filters and deduplicates Cloud Logging alerts itself and dispatches per route — a gateway turn by default, or a kanban task owned by the route's profile, which is how the shipped stockout route reaches `gke-stockout-investigator` on `platform` ([plugin README](../../agentplugins/gke-stockout-investigator/README.md)). Neither path reads a capability's criteria, and only the watcher's envelope carries a typed `kind` and `reason`. Drift detection is partly built — the Pub/Sub sink and a detector command exist, the inject it would emit does not ([`drift-detection.md`](drift-detection.md)). | **New.** "Run it now" from chat; a scoped chat run, and an event-triggered run, that both go through the capability's own procedure, criteria and reporting rather than an ad-hoc prompt; a capability declaring which event kinds wake it, in an envelope every adapter emits. Requirement R3. |
| Customizable  | The entrypoint refreshes `skills/` and `governance/` from the image on every pod start (skills wholesale, governance file by file), so an edit to a shipped file does not survive whatever tool made it, and the shell sandbox carries its own image copy of both ([skills](../site/src/content/docs/concepts/skills.md), [`agent-shell-sandboxing.md`](agent-shell-sandboxing.md)). Thresholds live inline in each SOP; changing one is a pull request against this repository. One git-backed tuning path exists today: the obtainability audit's declared-intent step reads `knowledge/` and every registered `context_repos` repository in the GitOps clone, and a finding the repository declares intentional is reclassified rather than reported (`agents/platform/governance/obtainability_audit_sop.md` §4a, `fleet-audit`'s `declared` record).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         | **New.** A criteria store outside the image-owned trees, a gated write path, and a merge that keeps tuned values across restarts and upgrades. Requirements R4, R6, R7.                                                                                                                         |
| Self-learning | The Chat Agent writes shared memory; the Platform Agent's memory is read-only and it can only nominate facts for it ([`memory.md`](memory.md)). Nothing turns a conversation into a change in what an audit checks.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               | **New.** Post-conversation reflection graded by a learning policy. Requirements R5, R6.                                                                                                                                                                                                         |

**Deferred, deliberately: a GitOps-backed persistence path.** Tuned values live on the profile's
volume in v1 (the Runtime tier under R6), not in the operator's configuration repository. A
git-backed overlay depends on foundational GitOps work in flight separately, so it is out of scope
here and expected as follow-up; R6 records the decision, its costs, and the migration path.

Out of scope here also: how any of it is built — file names, merge rules, tool names, and which
change lands first. Those belong to the change that implements it and to the contributor documentation
under `agents/platform/` once it lands. The first two capabilities to be specified for the
vehicle are the upgrade-readiness and fleet-anomaly-detection checks, proposed separately in
#1778; each has its own Scope section saying which of its checks already run.

## The idea in plain terms

A capability comes pre-built with the agent, so it starts working on day one. It runs on its own on
a schedule — or, if it is advice rather than an audit, on the re-check you ask for — and reports
what it found. If you want it right now, or just for one part of the fleet,
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

- The capability's procedure, its default criteria, and — for an audit — its schedule ship in the
  image and work on a fresh install with no setup step.
- Shipped defaults are quiet rather than thorough: the first weeks on a fleet are for tuning, and a
  report nobody reads is worse than a short one.
- An upgrade delivers procedure fixes without resetting anything the operator tuned (see R6).

### R2 — Scheduled

- The capability fires on a cron schedule in its own process with the Platform Agent's persona,
  tools, and turn budget, as the governance audits do today.
- A clean run posts nothing — with one exception, R5's announcement of an applied criteria
  change; a run with findings, or a failed run, reaches chat through the relay and the operator
  can reply in that thread.
- An audit's findings go to the same ledger and remediation path every audit uses; an advisory
  capability's artifacts go to the thread that asked. A capability does not build its own reporting.
- Two shapes of capability ride the same vehicle, and a capability says which it is. An **audit**
  is what the first three bullets above describe: it fires on its schedule, produces findings, and the ledger
  is its record. An **advisory** capability — a cluster design, a scheduling plan — is asked for
  rather than scheduled, answers in the thread with its artifacts, and gets a schedule only as a
  re-check the user opts into ("probe the recommended window again two hours before it starts");
  that re-check reports to the thread through the relay, not to a ledger.

### R3 — Triggerable

Three trigger paths reach the same procedure, read the same criteria, and use the capability's
reporting (R2): the schedule, a request in chat, and an event.

**From chat**, three kinds of request:

- **"Run it now"** for a shipped stream: the run happens in a fresh process through the identical
  path the schedule uses, never re-enacted inside the chat session that took the request. Today
  that path does not exist — `hermes cron run` is reachable only from the gateway pod, and the
  sandbox-side worker answered "queued" instead
  ([#1876](https://github.com/gke-labs/kube-agents/issues/1876)) — so the fleet-audit skill's
  interim is a single named stream run in-session through `audit_report.py start … finish` with
  its coverage gaps declared.
- **"Run it against this scope"**: one cluster family, one region, one check, answered in the
  thread that asked.
- **"Run it every Monday for family X"**: a schedule the operator defines in chat, which survives
  restarts and upgrades without an image change.

**From an event.** An adapter — the Kubernetes event watcher, the Pub/Sub platform adapter, or a
later one — detects a signal and filters and deduplicates its own noise. How the work then reaches
an agent differs per adapter today: the watcher opens one session per incident through the Session
KV server, whose severity gate and daily ceiling stand between it and chat, and the Chat Agent
files a card to the cluster's Cluster Agent; the Pub/Sub adapter runs whatever its route
configures — a gateway turn by default, or on `dispatch: kanban` a task owned by that route's
`agent_profile`, which for the shipped stockout route is `platform`
([adapter README](../../agentplugins/pubsub-platform/README.md)). The vehicle's requirements on top of that:

- A capability declares the event kinds that wake it, in one envelope shape every adapter emits.
  Today only the watcher's inject envelope carries a typed `kind` and `reason`; a Pub/Sub route
  names a skill and carries the alert as its prompt. An event-triggered run reaches the
  capability's procedure and criteria through that declaration, not through a prompt an adapter
  composes.
- An event-triggered run reports into the incident's thread, so a person replying there is
  answered by a session that saw the finding, and the incident record carries the criteria
  revision the run used.
- Noise control stays in the adapter and the gate. A capability may tune what it reports on
  (criteria), never what the adapter forwards.
- A session an event opened has no person in it. The run writes no memory of its own — the
  Platform Agent's memory is read-only, and a durable fact goes through the nomination path
  [`memory.md`](memory.md) defines — and a change it proposes to its own criteria follows the
  learning policy exactly as a scheduled run's would (R5): an incident is not a confirmation.

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
- Every applied change is announced in the capability's next report — what changed, why the agent
  thinks so, and how to revert it — so nothing moves silently. An applied change therefore makes
  that next run **audible**: today the helper decides silence from the run's own findings alone
  (`silent_ok` in the `fleet-audit` helper: nothing new or resolved, no coverage gap, no
  remediation pull request opened or closed), and a criteria change is none of those, so "first run after a criteria change" becomes one more
  condition that cancels silence. The announcement lands in the capability's **chat thread**
  through the relay, not the ledger — the same run may close the ledger issue, and a closed issue
  is where nobody reads.
- Everything learned is recorded with its evidence, so "what have you changed about the cost
  report this month, and why?" has an answer with sources.

| Class of change                                                                            | Default       |
| ------------------------------------------------------------------------------------------ | ------------- |
| Adds context: a better explanation of a finding, a link to the runbook the operator pasted | Apply, report |
| Widens what is checked: a new check the operator asked for, a scope the report was missing | Apply, report |
| Narrows what is flagged: raises a threshold, adds an exclusion, lowers a severity          | Propose only  |
| Changes the procedure or a red line                                                        | Never         |

What ships is "propose only" for every class, on every install. The table above is the ceiling: a
release may raise a class to its table default for one capability once the agent's proposals in
that class have been accepted consistently — and raising it is a reviewed change to the shipped
policy, never a runtime toggle (R6). The agent may suggest the raise; the suggestion is itself a
proposal. "A new check the operator asked for" means enabling or extending something the
capability already declares as tunable — a listed check turned on, an entry added to a declared
list. A check that needs new procedure is procedure, and the last row governs it: never.

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

**Scope of v1: Runtime is the default tier and Reviewed is the ceiling — a recorded decision, not
an oversight.** A git-backed overlay for criteria depends on foundational GitOps work that is in
flight separately, so v1 keeps tuned values on the profile's volume, where the store the agent
already reaches can hold them. The costs are accepted knowingly: a volume-resident value is not
peer-reviewed and is invisible to git, which is why every accepted change carries a changelog
entry with who confirmed it and why — shaped so accumulated changes can later be harvested into
one reviewed pull request when the overlay lands. Per-object suppressions already have a git path
today (the declared-intent step in the Scope table). Moving the default to Reviewed is expected
follow-up once that foundation exists; nothing in this document rules it out.

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

| Property      | Check on a real installation                                                                                                                                                                                                                                                                                  |
| ------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Pre-defined   | A fresh install lists an audit's schedule and returns the shipped defaults for any capability's criteria with no setup performed.                                                                                                                                                                             |
| Scheduled     | For an audit, marking the stream due produces a run in its own process within a minute, a ledger update, and a chat report, and a clean fleet produces silence; for an advisory capability, an opted-in re-check produces a report in its thread.                                                             |
| Triggerable   | A scoped chat request returns a scoped report in the same thread; a chat-defined schedule is still listed after the pod restarts; an injected event of a kind the capability declares produces a run of the capability's procedure whose report lands in the incident thread and names the criteria revision. |
| Customizable  | A confirmed threshold change is reflected in the next run's findings and in the revision the report names; it is still in effect after a restart and after an image roll that also adds a new default.                                                                                                        |
| Self-learning | Replying "that finding is noise" produces a proposal, not a change; replying "also flag X" produces a proposal, and once confirmed the change is applied, announced in the capability's next report with its revert step, and recorded in a changelog entry citing the thread.                                |

The mechanism-versus-coincidence rule applies: each check sets a value distinctly different from
the default, observes it, and reverts it.
