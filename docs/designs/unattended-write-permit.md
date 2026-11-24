# Unattended write permit

A proposal for decision. It defines how a run that a built-in schedule starts can publish a
GitOps proposal with no person to confirm it, as an opt-in on the schedule. The security approver
answers each row of [Decisions](#decisions) with yes or no.

Related designs: [`scheduling-on-profiles.md`](scheduling-on-profiles.md) (the built-in table,
canary rows, the schedule tool), [`spec-profiles-dispatch.md`](spec-profiles-dispatch.md) (the
task credential and the confirmed permit),
[`version-control-support.md`](version-control-support.md) (the broker's ownership bar and
per-task bar). The rows for each broker route and verb belong to the broker-surface design
(`profile-broker-surface.md`).

## Problem

A profile worker reaches the broker's version-control write verbs (`publish`, `proposal-create`,
`proposal-update`) only when its task credential carries the permit `vcs:proposal-create`. The
dispatcher mints that permit when three things hold (`carriesPermit` in
`a2a/dispatch/admission.go`, the mint in `taskCredentials` in `a2a/dispatch/dispatcher.go`):

1. The submission says `changeRequested`. The gateway sets it for a structured confirm; a child
   and a wake copy their parent's value; the eval inject door sets it when it is armed.
2. The task names a cluster target.
3. The task runs on its own one-task Job, not on a pool pod.

A run that a schedule starts never has the first: the trigger ingress passes a fixed `false`
(`a2a/gateway/trigger_ingress.go`). A fleet audit also has no single cluster target. So a
scheduled audit cannot publish a fix proposal. This is blocker 12 of
[`platform-as-profile-inventory.md`](platform-as-profile-inventory.md).

No built-in schedule row ships today, so no unattended profile run reaches a forge write path
yet. The audits of today run through the agent's own role, which this design does not change.

## What must hold

1. A person's confirmed change keeps today's permit and today's rule.
2. A schedule publishes proposals only after an opt-in on that schedule. The default is off.
3. The permit goes only to a run that a built-in schedule started. The grant binds the schedule's
   trigger, and the capability verifier held the request's trigger equal to it.
4. A permitted write reaches only the write families that the schedule opted in to, and only
   branches of that schedule.
5. A canary row never gets the permit. Its `dry_run` parameter is behaviour; the missing permit
   is the enforcement, and it stays the enforcement.
6. Only a chain that a person started creates or changes a schedule. The permit gives no way to
   write a schedule or the opt-in.
7. Each mixed pair of versions in an upgrade refuses the write. None admits a write that the
   newer rule refuses.

## Options

| Option                                                                                                       | Result                                                                                                                                                                                     |
| ------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| A. Reuse `vcs:proposal-create`: the trigger ingress sets `changeRequested` for a schedule that opts in       | No new name. The broker cannot tell a confirmed change from an unattended write, a canary is held back by the minter alone, and nothing scopes the write to the schedule. Not recommended. |
| B. One new permit name. Its scope is the credential's `trigger` claim plus a table that the operator renders | The claim shape does not change. The pattern exists: the broker scopes a report write to the trigger's stream. Recommended.                                                                |
| C. Permit entries with fields (name, repository, families), the shape that #4428 asks for                    | The credential states its own scope. `permits` changes from strings to objects, which both verifiers refuse today, so each later field is another version-skew step.                       |

## Decisions

"Where" names what changes when the row is built.

| Id  | Decision                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     | Where                                                                                                                   |
| --- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------- |
| 1   | Name and shape. One new permit, `vcs:unattended-write`, in the closed list. `permits` stays a list of strings. A credential never carries both permits, and a task whose grant binds a trigger never gets `vcs:proposal-create`.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             | `a2a/taskcred/taskcred.go`, `agents/platform/scripts/task_credential.py`, the parity fixtures                           |
| 2   | Scope is the trigger claim. The verifiers accept the new permit only on a credential-proxy credential whose `trigger` has origin `builtin`, a `schedule/<name>` name and a stream that is not empty.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         | the same files                                                                                                          |
| 3   | The opt-in is on the schedule. A new field `spec.writes` of the AgentSchedule lists the write families: `proposal` (the three verbs above) and, when the profile role gets them, `issue`. Empty is the default. Only a built-in schedule can set it: the reconciler does not accept another schedule that does, as for `spec.script`. The built-in admission policy already freezes each key of a built-in's spec but `suspend`, so it needs no change. A pause or resume from chat never sets it. Its value comes from a new `writes` key of the PlatformAgent's schedule override, cut to a ceiling that the operator's row names in code. A canary row's ceiling is empty.                                                                                                                                | the AgentSchedule and PlatformAgent types and CRDs, `platformagent_builtin_schedules.go`, the built-in admission policy |
| 4   | One rendered table, two readers. The operator computes schedule name to families from the PlatformAgent and its rows (not from the AgentSchedule object, so an edit of that object opts nothing in). It mounts the table into the dispatcher and the broker, as it does for the tier table. The table has a version; a reader that does not know the version or a column reads the table as empty. An absent or empty table means no permit. The operator renders the table only where the built-in admission policy is installed: without it, a principal that can update the AgentSchedule can change the prompt of an opted-in schedule.                                                                                                                                                                  | the operator's A2A manifests                                                                                            |
| 5   | The mint. The dispatcher adds the permit when all hold: the task's grant binds a trigger; the table lists the trigger's schedule; the task runs on its own one-task Job; `changeRequested` is false. A cluster target is not necessary. A task of an opted-in schedule that has no Job runner is refused, as a confirmed change is today.                                                                                                                                                                                                                                                                                                                                                                                                                                                                    | `a2a/dispatch/admission.go`, `a2a/dispatch/dispatcher.go`                                                               |
| 6   | The broker's gate. A profile write verb passes with `vcs:proposal-create` as today, or with `vcs:unattended-write` when the broker's own table lists the trigger's schedule and the verb's family. The broker reads the table for each request, so a removed opt-in stops the next write of a credential that is still valid. The managed-repository check and the ownership bar stay, after this gate.                                                                                                                                                                                                                                                                                                                                                                                                      | `profile_vcs_refusal` and the gated sets in `agents/platform/scripts/credential_proxy.py`                               |
| 7   | The schedule's branches. An unattended `publish` is admitted only to a branch under `platform-agent/schedule/<stream>/`. `proposal-create` and `proposal-update` are admitted only for a proposal whose head branch is under that prefix; the ownership bar already reads the head branch. The broker refuses `platform-agent/schedule`, and each name below it, to each profile credential that has no unattended permit for that stream. The agent's own shell role is not held to this, so the prefix bounds profile credentials and is not proof of origin.                                                                                                                                                                                                                                              | `agents/platform/scripts/vcs_broker.py`, `credential_proxy.py`                                                          |
| 8   | The schedule bar replaces the per-task bar for an unattended write. The per-task bar lets a task continue only what that task made, and its record is lost when the broker restarts. A scheduled audit finds the same defect in each run, so a later run of the same schedule must update the proposal of an earlier run. Row 7 gives that with no stored state. The cost: a later run can add commits to, and change the title of, each open proposal under the schedule's prefix, and the run's inputs (cluster state, repository content) are not fixed as its prompt is. One bound, to decide with this row: the broker refuses an unattended `publish` or `proposal-update` for a proposal that has an approving review, and the run opens a new branch. The per-task bar stays for a confirmed change. | `credential_proxy.py`                                                                                                   |
| 9   | No cluster target. A credential with no target exists only at the tier that the report credential uses, and reaches an exact list of audit-report routes today. A run of an opted-in schedule with no cluster carries that credential (no target, the trigger) plus the permit. With the permit it also reaches `POST /v1/vcs/<verb>` for the verbs of the profile role, each listed by name, and only while the broker's table lists the trigger's schedule. It never reaches `/v1/exec` or the MCP relay.                                                                                                                                                                                                                                                                                                  | the route list for a credential with no target, in `credential_proxy.py`                                                |
| 10  | Canary. Three checks. The operator gives a canary row an empty ceiling and reports an override that asks for writes. The rendered table has no canary row. The dispatcher and the broker refuse the permit for a trigger whose schedule name ends in `-canary`, whatever the table says.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     | the operator's table, the dispatcher, the broker                                                                        |
| 11  | Children, first step. Today a scheduled run delegates only to children at the `read` tier with no trigger, and only the parent writes under the schedule's trigger. For a schedule that opts in, this rule does not change: the parent holds the permit and publishes; children read for it.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 | none; tests only                                                                                                        |
| 12  | Children, second step: not in this design. A child that writes needs the schedule's trigger on its credential, and the envelope admits that. But the same trigger makes the child a writer of the parent's report stream, a script trigger makes it a script run, and the credential has no claim that tells a child from its parent. A child writer needs its own design for these three points and for a bound on parallel writers. The answer here is only if row 11 is enough for the first schedules.                                                                                                                                                                                                                                                                                                   | none                                                                                                                    |
| 13  | Schedules stay a person's. The schedule tool is not built; its design gives scheduled work only `list`, `get` and `history`, and gives its write verbs to a chain that a person started. This permit names no verb of that tool and changes neither rule. The opt-in is written only through the PlatformAgent: the agent's role only reads that object, and a profile's kubectl is read-only by the broker's command policy. One path stays, to decide with this row: if a managed repository holds the install's own values, an unattended run can propose a change of the opt-in, and only the merge stops it. Proposed: an unattended proposal never merges without a person.                                                                                                                            | none                                                                                                                    |
| 14  | Tier. The mint keys on the trigger in the grant, not on a tier's name. Today a grant binds a trigger only at the tier named `unattended`. If #6091 admits a custom tier in unattended mode, rows 1 to 8 do not change; row 9 follows the report credential's tier rule. An interactive tier never binds a trigger.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           | none beyond row 5                                                                                                       |
| 15  | Audit record. The broker's log line for an admitted unattended write names the permit, the trigger's name and stream, and the credential's `jti`. No broker line prints the trigger's name today, and the dispatcher's mint line logs the `jti` and the permits.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             | `credential_proxy.py`                                                                                                   |

### The repository in the permit (#4428)

#4428 asks that the confirmed permit names the repository that the person approved. The unattended
permit does not need that shape: its scope is the trigger (rows 2 and 7). If #4428 changes
`permits` to entries, the unattended permit becomes an entry with a name only, in the same change.
This design does not decide #4428.

A permitted unattended write can reach each repository on the install's managed list, as a
confirmed write can today. A narrower bound for a schedule (for example the GitOps repository
only) is a new column of the table and a new table version (row 4).

### The older child issue (#5946)

#5946 describes a refusal of each delegation from an unattended task. On `main` delegation is
admitted, with the rule that row 11 states. Rows 11 and 12 are the decision that #5946 waits for.

## Version skew

Facts, from the code and the upgrade page:

- The dispatcher mints. Its image follows the operator's image.
- The broker and its exec tier verify with the Python verifier. Their image follows the agent's
  tag. A profile worker verifies with the Go package for script runs only; its image follows the
  agent's tag unless a profile pins it.
- The stated upgrade order is the operator first, then the harness. So the minter moves before
  the verifiers, and the time between the two runs has no limit.
- Each verifier refuses a permit that it does not know, and then the whole credential. Parity
  fixtures hold the two verifiers to that.
- The dispatcher and the stateful broker each run one replica with the `Recreate` strategy. The
  exec tier rolls with more than one replica, and serves `/v1/exec` and the cloud relay only.
- The auth callout verifies the bus credential, which never carries a permit.
- A credential is minted once for a task and lives until the task's deadline plus 30 seconds, at
  most 6 hours by default.

Three steps, in three releases:

1. The broker's whole rule. The two verifiers know the name, and the broker holds rows 6 to 10
   and 15 and reads the table. Nothing mints the permit. A later change of the broker's rule is
   a new permit name or a new table version, not a change of this one.
2. The opt-in and the mint (rows 3 to 5), one release after step 1. The upgrade table gets one
   row: upgrade through the release that has step 1 first. The mint stays off while no schedule
   opts in, so this step changes no credential by itself.
3. The first row with a ceiling, and the proof.

| Operator and dispatcher | Broker          | Result                                                                                                                           |
| ----------------------- | --------------- | -------------------------------------------------------------------------------------------------------------------------------- |
| before step 2           | any             | No permit is minted. A write is refused as today.                                                                                |
| step 2 or later         | before step 1   | For a schedule that opted in: the broker refuses the whole credential, so the run loses each broker route. No write is admitted. |
| step 2 or later         | step 1 or later | The rule of this document.                                                                                                       |

The report credential used this order: the broker accepted it before the dispatcher minted it.

Other pairs, each of which refuses the write:

- CRDs older than the operator: the API server drops `spec.writes`, so no schedule opts in.
- A broker of step 1 with a table of a newer version: it admits the credential and refuses the
  write (row 4).
- An operator that is rolled back to before step 2 while the table is still mounted: the older
  dispatcher mints no permit.

Option, if a direct upgrade across step 1 must also keep its runs: the operator renders the table
only while the broker's image is at the operator's own release. With an older broker, the run
gets today's report credential and no permit.

## Proof

A run that the inject door starts cannot show this permit: its requester is not a trigger, so it
holds no trigger claim. The proof is a run that a built-in schedule starts.

- The row. Neither row in work on the schedule track writes (the compliance-audit canary, the
  onboarding shadow row), so the proof needs a row of its own. Proposed: a run-now-only row for
  the workload reliability audit, whose fix is a manifest change, with the ceiling `proposal`,
  and with only the CI lane opted in. The row has a name of its own (for example
  `obtainability-audit-proposals`), because a schedule's name is its report stream and the name
  `obtainability-audit` is the live audit's. The name and the row's place in the built-in table
  are to be agreed with the owner of the schedule track.
- The lane. One CI lane sets `A2A_AGENT_SCHEDULES` and the opt-in. No lane sets the switch today.
  The stack `bench/tf/prebuilt/builtin-schedule-run-now` fires a built-in schedule; no bench case
  uses it yet.
- Who can start a run. A principal that can update the AgentSchedule can set its run-now
  annotation. That starts the schedule's own fixed prompt, within the trigger's rate cap (12
  runs an hour by default). Where the built-in admission policy is installed, it cannot change
  the prompt or the opt-in of a built-in (row 4).
- Evidence that the write was the schedule's: the task id in the AgentSchedule's status; the
  gateway's task record with the requester backend `trigger`; the broker's log line of row 15.
- Refusals, each as a conformance row with its mutation: a canary row; a child (while row 12 is
  not built); a schedule that did not opt in; a family that the schedule did not opt in to; a
  branch outside the schedule's prefix; the schedule's prefix with a confirmed permit; a
  credential with no trigger; `/v1/exec` with a credential that has no target; a pool pod; a
  proposal with an approving review (row 8).

## What stays out

- Event sources (the repository watcher, drift and triage). Their triggers name no schedule and
  no stream, so they get no permit in this design.
- The issue verbs for the audit ledger. They are rows of the broker-surface design; this
  document only reserves the `issue` family.
- User schedules. They do not run today, and the plan gives them no trigger. Row 2 refuses each
  origin but `builtin`.
- The confirmed permit, its Job rule, and #4428.
- Open proposals on the audits' old branch names (`platform-agent/fix-<audit-id>-`). A run with
  the new permit cannot update them; the move of each audit says what happens to them.
- Merge. A proposal from an unattended run merges as each other proposal does.

## Done when

For the group issue of this design:

1. Each row of [Decisions](#decisions) has the security approver's answer.
2. Step 1 of [Version skew](#version-skew) is released: the verifiers and the broker's whole
   rule, with parity fixtures and a conformance row for each refusal in [Proof](#proof).
3. Step 2 is released one release later: the opt-in field, the table and the mint.
4. One built-in row has a ceiling, and a run that its schedule starts opens a proposal on a
   nightly lane, three times.
5. A canary row, a child and a schedule with no opt-in are each shown to be refused in that lane
   or in a conformance row.
6. The security page states the permit and the opt-in, and the upgrade table has the row of step 2.

## Checked, and not checked

Checked in the code on `main` at `b5f4f11f2`: the mint rule and its three inputs; the fixed
`false` of the trigger ingress; the closed permit lists and the unknown-permit refusal of both
verifiers; the broker's gate, its two verb sets and its import-time table check; the report-write
rule that scopes to the trigger's stream; the route list for a credential with no target; the
per-task bar and its in-memory record; the branch prefix of the ownership bar; the child rule;
the canary rows and the empty built-in table; the override type, which sets only `suspend`; the
built-in admission policy and the chart's condition for it; that the agent's role only reads
PlatformAgents; that the broker's command policy holds kubectl to read verbs; the
image that each workload follows; the rollout strategies; the credential's lifetime; the upgrade
order on the upgrade page; the order of the broker and the A2A stack in one operator pass; that
no chart, CI or bench file names the schedules switch; that no bench case names the run-now
stack.

Not checked: the order in which a `full` upgrade rolls the broker and the dispatcher on a real
install; how long installs stay between the operator run and the harness run; whether an install
pins the broker's, the dispatcher's or a profile worker's image apart; whether a branch has a
built-in or canary row; which image the dispatcher takes when the operator's image reference has
a digest and no tag; whether the broker can read a proposal's reviews at `publish` with no new
forge call.
