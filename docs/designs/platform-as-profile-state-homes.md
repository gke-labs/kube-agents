# Homes for the Platform's Durable State When Its Work Moves to Profiles

> **STATUS: draft for a decision.** Nothing here ships. This is the state design for the epic
> [#6526](https://github.com/gke-labs/kube-agents/issues/6526), for its groups
> [#6669](https://github.com/gke-labs/kube-agents/issues/6669) (durable state) and
> [#6673](https://github.com/gke-labs/kube-agents/issues/6673) (the autonomy dial and the post-merge
> state). Each route, verb and state home below needs the decision of the security approver before
> any code, and the approver confirms the "Done when" lists here. The dial and post-merge rows
> also need the view of the owner of that code.

The platform agent keeps its durable state on its own pod: in the Session KV database, in files
under its Hermes home, and in a tree on its volume. A profile worker runs in a pod with no
persistent volume and is reset after each task
([`platform-as-profile-inventory.md`](platform-as-profile-inventory.md), blocker 6). So a duty that
reads or writes that state cannot move to a profile until the state has a home that the profile can
reach. This document has one row for each piece of that state, says where it is today, and proposes
where a profile reaches it.

## What was checked

Checked in the code on `main` at `26fa76913`: the three fact columns of the two tables ("Where it
is today", "Who uses it", "A profile today"), the broker's route table, the read and write rules of
the report route, the mounts of the agent's pod, and the statements about the written direction. The
author read them, and a second read in a clean context checked them. The issues and pull requests
that the next section links were read for what they intend. Not checked: their code, a running
install, the tests, and an install with more than one broker pod.

## The work that this design starts from

This design adds to the work below. It links that work and does not repeat its details.

| Work                                          | What it gives                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  | Where                                                                                                                                                                                                           |
| --------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| The report store behind the broker            | The one state route that a profile has, and the pattern for each new one.                                                                                                                                                                                                                                                                                                                                                                                                                      | [`fleet-audit-report-store.md`](fleet-audit-report-store.md), [#5619](https://github.com/gke-labs/kube-agents/issues/5619)                                                                                      |
| The broker store for each writer              | The shell sandbox and each profile worker use the broker's store, and a seed keeps the reports that an install already has. This is the home and the migration path of the report tree.                                                                                                                                                                                                                                                                                                        | [#6110](https://github.com/gke-labs/kube-agents/issues/6110)                                                                                                                                                    |
| A start record for each run                   | A stream shows a run that started and did not finish.                                                                                                                                                                                                                                                                                                                                                                                                                                          | [#6818](https://github.com/gke-labs/kube-agents/pull/6818)                                                                                                                                                      |
| Shadow runs                                   | A canary row of the built-in schedule table runs beside the job that it can replace. The table says of its run: it writes its report to a stream of its own and nothing else (no ledger, no findings queue, no remediation, no cluster change), and its delivery is local. The store-only finish ([#6956](https://github.com/gke-labs/kube-agents/pull/6956)) stores only the report; shadow streams are hidden from the readers ([#6958](https://github.com/gke-labs/kube-agents/pull/6958)). | The built-in table (`platformagent_builtin_schedules.go`), the check `findings_queue_untouched` in [`bench-case-format.md`](bench-case-format.md), [#5623](https://github.com/gke-labs/kube-agents/issues/5623) |
| The direction for job state and for the queue | Points O2, O3 and O4 of the scheduling inventory. See "The direction that is already written".                                                                                                                                                                                                                                                                                                                                                                                                 | [`scheduling-on-profiles-inventory.md`](scheduling-on-profiles-inventory.md), [#5895](https://github.com/gke-labs/kube-agents/issues/5895)                                                                      |
| The event producers                           | The event watcher and the drift detector move to triggers, and the triage gating with its ledger moves with them. The stall-watch job, the third writer of that ledger, is a script run of the schedule track. The daily recap reads that ledger: the plan of #6572 moves it with the watcher, and the scheduling inventory gives its run form as open (O3).                                                                                                                                   | [#6528](https://github.com/gke-labs/kube-agents/issues/6528), [#6572](https://github.com/gke-labs/kube-agents/issues/6572), [#5622](https://github.com/gke-labs/kube-agents/issues/5622)                        |
| Thread incidents and pushed reports           | A scheduled report is kept as its thread's incident, and the pushed reports are kept in session state.                                                                                                                                                                                                                                                                                                                                                                                         | [#6124](https://github.com/gke-labs/kube-agents/pull/6124), [#6889](https://github.com/gke-labs/kube-agents/pull/6889), [#6870](https://github.com/gke-labs/kube-agents/issues/6870)                            |
| The unattended write permit                   | What lets a scheduled run make a change.                                                                                                                                                                                                                                                                                                                                                                                                                                                       | [#6672](https://github.com/gke-labs/kube-agents/issues/6672)                                                                                                                                                    |
| The two designs beside this one               | The broker-surface design gives each other broker route and verb its row. The write-permit design gives the permit, its rendered table and its rule for a canary row. This design depends on both.                                                                                                                                                                                                                                                                                             | [`profile-broker-surface.md`](profile-broker-surface.md), [`unattended-write-permit.md`](unattended-write-permit.md)                                                                                            |

Three rules follow from that work. The design of the queue holds each of them:

1. **One home for each piece.** The report store is the home of each report and of the state that
   a job keeps between runs. This design adds no second home for them. The queue stays the one home
   of a finding's state: what a person decided and what was shown.
2. **A shadow run on a profile changes only its own report stream.** No shadow run of a profile
   task gets a write to the queue. The broker holds that, not only the script: the built-in table
   calls the `dry_run` parameter behaviour, not enforcement. It names the missing write permit as
   what stops a shadow run. That permit gates version-control writes, so the queue needs a rule of
   its own.
3. **One form for a write to a state store.** A profile's write to the queue follows the write
   rule of the report store: the task's trigger names the built-in schedule, and the schedule's
   stream names what the task may write. Version-control writes keep the permit and the ownership
   bar; the source check of question 5 is the ownership rule of the queue.

## What a profile can reach today

The credential broker gives the profile role four routes (`ROUTE_ROLES` in
`agents/platform/scripts/credential_proxy.py`): `/v1/exec` for `kubectl` and `gcloud`, the MCP
relay, `/v1/vcs/`, and `/v1/audit-reports`. The last one is the only state route:

- The store is a tree on the agent's volume. A listener of its own
  (`agents/platform/scripts/audit_report_store.py`, port 8700, in the `agent-api-auth` container of
  the agent's pod) serves that tree and nothing else.
- Only the broker can call the listener: each request carries the broker's projected token, and a
  NetworkPolicy rule admits only the broker's pods.
- The broker decides for each request. A profile reads only with a task credential whose profile
  is on the install's allowlist. It writes only with such a credential and a trigger of origin
  `builtin` that is named `schedule/<stream>`, only that stream, and only to the install's publish
  key. The write rule reads no write permit. The broker stamps the writer on each run.
- A scheduled run with no cluster target holds a report credential. For a run without the write
  permit, that credential reaches only the routes that `REPORT_CREDENTIAL_ROUTES` names. The
  write-permit design adds a named set of version-control verbs for a run that holds the permit.

Session KV (`agents/platform/scripts/session_kv_server.py`) listens on loopback of the agent's pod.
One bearer key admits each of its routes but `/healthz`. No profile reaches it, and this design does
not change that.

## The direction that is already written

[`scheduling-on-profiles-inventory.md`](scheduling-on-profiles-inventory.md) gave a direction for
scheduled jobs in its open points O2 and O3, and a decision in O4:

- O2: the state that a job keeps between runs becomes one small record for each job in the report
  store. A report that one job writes for another goes in the same store.
- O3: broker verbs front the findings queue and a read-only endpoint for the events ledger, scoped
  by the task's grant as the report route is. No job opens the Session KV database file.
- O4: the weekly brief runs on a narrow profile with no platform MCP tools. Text that a tenant can
  write then cannot make its turn register, verify or decide a finding.

[#5895](https://github.com/gke-labs/kube-agents/issues/5895) was the issue for the verbs of O3. It
was closed as not planned, because no profile task needed the queue then. Its closing note says to
reopen it, or to fold it into #6669, when the platform duties move. This design follows that
direction and names where it differs.

This design answers O3 for the findings queue only. For the events ledger it proposes no route,
so O3 stays open there: the daily recap opens the Session KV database file until question 7 has
an answer.

## The options for a home

1. **A broker route in front of the state, where the state is today.** The data does not move.
   The broker checks the caller, and a broker-only listener on the agent's pod serves the named
   routes and no other. This is the report store's form.
2. **A new home off the agent's pod** (a bucket on the bus, or a service of its own). The data
   moves, with a migration.
3. **The state stays where work starts**, and no profile reaches it. Today that place is Session
   KV. A profile gets what it needs in its task, and gives its result back in its answer.

Our recommendation: option 1 for state that a profile task must read or write during its run,
option 3 for state that only starts or frames a task, and option 2 for no piece now. Option 2 is
the end state when the agent's pod goes away. Each move of data is a migration, and nothing in the
epic needs one before the retirement step.

## The durable state of group #6669

| Piece                                                                              | Where it is today                                                                                                                                                                                                                                                                                                                         | Who uses it                                                                                                                                                                                                                                                                                           | A profile today                            | Home, and the work that covers it                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| ---------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Findings queue                                                                     | Tables `findings`, `queue_publications`, `findings_additions` (`findings_queue.py`) in the Session KV database. Ten routes under `/v1/findings`: four read, six write.                                                                                                                                                                    | Writers: the first-run delivery and the inventory sweep (register, mark shown), the weekly upgrade-readiness review (register), the hourly nudge (mark surfaced, expire snoozes), the MCP tools of the platform. Readers: the same tools, the nudge, the weekly brief, the Slack Home board snapshot. | No route.                                  | **Option 1.** The data stays. The broker serves `findings.read` and `findings.write` to profile tasks, through a broker-only listener. No work covers it: see the next section.                                                                                                                                                                                                                                                                                                                                                                                                            |
| Events ledger                                                                      | Table `intercepted_events` in the Session KV database, with `alert_quota`, `drift_backlog_lease` and the excluded-events record. `GET /v1/intercepted-events`.                                                                                                                                                                            | Writers: the event watcher, the drift detector and the stall-watch job (a `controller-stall` inject), each through Session KV. Readers: the `get_alert_history` tool, and the daily recap job, which opens the database file.                                                                         | No route.                                  | Covered in part by [#6572](https://github.com/gke-labs/kube-agents/issues/6572): the two watchers and the triage gating, which holds the ledger. The stall-watch job is a third writer. It moves as a script run of the schedule track ([#4685](https://github.com/gke-labs/kube-agents/issues/4685)), so where its ledger row goes is part of question 7. The daily recap opens the database file for the ledger. The plan of #6572 moves the recap with the watcher; the scheduling inventory gives its run form as open (O3). This design proposes no events route now. See question 7. |
| Fleet-audit report store                                                           | A tree on the agent's volume, served by the listener on port 8700. With the shell sandbox on, the fleet-audit scripts write the sandbox's own file store.                                                                                                                                                                                 | Writers: `audit_report.py finish` on the platform, and a profile task on its own builtin schedule. Readers: the platform, allowlisted profiles, the A2A reports role.                                                                                                                                 | **Reachable** through `/v1/audit-reports`. | No new home. Covered by [#6110](https://github.com/gke-labs/kube-agents/issues/6110), with [#6818](https://github.com/gke-labs/kube-agents/pull/6818) for the start record of a run.                                                                                                                                                                                                                                                                                                                                                                                                       |
| State that a job keeps between runs                                                | Files in the Hermes home and on the volume: `stall_watch.json`, `sa_role_watch.json`, `.findings_nudge_state.json`, the ledgers of the upgrade-readiness watch and of `leeway-watch`, the two markers of `feedback-prompt`, `github-repo-watcher/unreadable-prs.json`. The nudge also reads the onboarding markers of the first-run flow. | Each job, for itself.                                                                                                                                                                                                                                                                                 | No route.                                  | The report store (O2): one small record for each job. The write rule of the store gives each built-in schedule a stream of its own. No new route and no second home. The form of a job record belongs to the track that moves each job ([#4685](https://github.com/gke-labs/kube-agents/issues/4685)). Two facts for that track: a profile writes only under the install's publish key, and the report readers list each stream as an audit.                                                                                                                                               |
| State of rostered jobs that do not move to a profile                               | `chat_delivery_watch.json` and `kanban_board_health.json` in the cron folder of the platform profile, and the table `kanban_block_rechecks` in `kanban.db`.                                                                                                                                                                               | The jobs `chat-delivery-watch`, `kanban-board-health` and `kanban-block-recheck`, each for itself.                                                                                                                                                                                                    | Not needed.                                | No home for a profile, and no record in the report store. The scheduling inventory retires the two kanban jobs with kanban, and it replaces the chat delivery watch with a controller and a condition on the PlatformAgent. The state of each job goes away with the job.                                                                                                                                                                                                                                                                                                                  |
| A report that one job writes for another                                           | Files on the volume: the weekly review's `latest.json` and `latest.md`, the upgrade-readiness reports, the upgrade-retrospective reports.                                                                                                                                                                                                 | Writer: one job. Readers: another job, or the agent on a person's ask.                                                                                                                                                                                                                                | No route.                                  | The report store (O2). No second home.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| Posted-run records and the run ledger                                              | Tables `audit_report_runs` and `scheduler_failure_posts` in the Session KV database; `cron/executions.db` in the profile home.                                                                                                                                                                                                            | Session KV, the scheduler, and the first-run delivery, which reads `audit_report_runs` from the database file.                                                                                                                                                                                        | No route.                                  | Not in this design. [`scheduling-on-profiles.md`](scheduling-on-profiles.md) gives the run ledger to the schedule's status. The posted-run records belong to the delivery of reports ([#5618](https://github.com/gke-labs/kube-agents/issues/5618)).                                                                                                                                                                                                                                                                                                                                       |
| Task listing                                                                       | `GET /v1/tasks` and `GET /v1/tasks/{id}` read the Hermes kanban database, and for one task the worker's session store. They hold no state of their own.                                                                                                                                                                                   | `feedback_summary.py` calls the list route.                                                                                                                                                                                                                                                           | No route.                                  | **Option 3.** It is a view of the board, and the board stays where work starts. If the feedback summary moves to a profile, its read needs a new row in the broker route group ([#6670](https://github.com/gke-labs/kube-agents/issues/6670)).                                                                                                                                                                                                                                                                                                                                             |
| Cap of open triage pull requests                                                   | Table `triage_pr_reservations` in the Session KV database, and a count of the open triage pull requests on the forge. Session KV applies the cap at a triage start, when the install turns it on.                                                                                                                                         | Session KV.                                                                                                                                                                                                                                                                                           | No route.                                  | **Option 3.** It stays with the triage start. A profile does not read or write it. Whether the move of the triage start carries it is question 8.                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| Monitor of the data volume                                                         | A thread in Session KV and the table `data_volume_warning`. It posts a warning and an all-clear in chat.                                                                                                                                                                                                                                  | Session KV.                                                                                                                                                                                                                                                                                           | Not needed.                                | No home for a profile: a profile has no persistent volume. It goes away with the volume.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| The two volumes                                                                    | The agent's data volume (the Hermes home) and the `system-metadata` volume (the Session KV database).                                                                                                                                                                                                                                     | The agent's pod.                                                                                                                                                                                                                                                                                      | None.                                      | No home for a profile. They go away with the agent's pod, after each row above has its home.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| Incidents                                                                          | Tables `incidents` and `incident_applies`, and the routes under `/v1/incidents`.                                                                                                                                                                                                                                                          | Session KV, the bridge, the plugins and tools of the platform.                                                                                                                                                                                                                                        | No route.                                  | Not decided here. The nearest work keeps each scheduled report as its thread's incident ([#6124](https://github.com/gke-labs/kube-agents/pull/6124)) and keeps the pushed reports in session state ([#6889](https://github.com/gke-labs/kube-agents/pull/6889), [#6870](https://github.com/gke-labs/kube-agents/issues/6870)). Question 9 asks which rows that work covers.                                                                                                                                                                                                                |
| Route and origin records, eval-run tags, stamp tickets, the board, the transcripts | Tables `session_metadata`, `cron_job_origins`, `session_eval_runs`, `kanban_stamp_tickets`, `kanban_task_origins`; `kanban.db`; the Hermes `state.db`.                                                                                                                                                                                    | The bridge, Session KV, the harness.                                                                                                                                                                                                                                                                  | No route.                                  | **Option 3.** They frame a conversation or a card. No profile needs them.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |

## The findings queue: one design

The queue gets one route group in the form of the report route. The trigger's stream decides what
a task may store, as today. A new table of streams adds what that stream may write to the queue.

**Who may read.** A profile task with a credential whose profile is on an allowlist, as for a
report read. Question 4 asks if that list is the report route's list or a list of its own. A read
is install-wide: each task of a listed profile reads each finding of each cluster. It needs no
trigger and it is not held to the task's target. It never crosses installs. A shadow run reads as
a live run does: the four read routes change nothing.

**Who may write.** A profile task writes the queue only when each of these holds:

- its profile is on the allowlist;
- its trigger has the origin `builtin` and the name `schedule/<stream>`, as for a report write;
- the stream does not end in the canary suffix;
- the broker's table of streams has a row for that stream, and the row names the verb.

A row names the write verbs that its stream may call, the one source that it may register, and
the check family when the source shares its families with another writer. A stream with no row
writes nothing. A task with no trigger (a delegated task, a task that a person started) is
refused, as its report write is.

**What holds a shadow run.** Two rules hold it. The first is default deny: a stream with no row
writes nothing, and the operator renders no stream row for a canary row. The second is the suffix
rule: the stream of a canary row is the row's own name, which ends in the canary suffix, and the
broker refuses each queue write of such a stream, whatever the table says. A stream is always the
schedule's own name, and a task cannot choose it. The task credential does not carry the `dry_run`
parameter, so the broker goes by the name.

The built-in table names the missing write permit as what stops a shadow run. That permit gates
version-control writes; a queue write, as a report write, reads no permit. So this design adds the
suffix rule for the queue, in the form that the write-permit design
([#6672](https://github.com/gke-labs/kube-agents/issues/6672)) uses for its permit. Both hold: the
permit for a version-control write, the suffix rule for a queue write. Question 6 asks if a live
queue write also needs the permit.

These rules hold a profile task. A run on the platform agent itself does not pass the broker for
the queue: the platform's MCP tools reach Session KV on loopback. A shadow run that is started by
hand there (the `<audit>-shadow` stream) is held only by the behaviour of the scripts, as today.

**What keeps O4.** The stream of the weekly brief gets no write row. A stream whose model turn
reads text that a tenant can write gets a row only by a decision of the security approver, verb
by verb. Question 3 asks which verbs a run that no person started may call.

**What the rule cannot hold alone.** The six write routes are not equal:

- A register carries the source and the check of each finding in its body. To hold it to the row,
  the broker parses each batch. It refuses a finding whose `source` is not the row's source, and a
  finding whose check is not in the row's family when the row has one. When the batch carries a
  complete scope, the broker also requires `scope.source` equal to the row's source, and
  `scope.check_prefix` when the row has a family: without `scope.source`, a complete scope with a
  prefix lowers the rows of each writer in that family. A batch with no scope stays permitted: a
  partial run sends none.
- A finding's id holds no source, and a finding's source is its last writer
  ([#3561](https://github.com/gke-labs/kube-agents/issues/3561)). A permitted register can take over
  a finding that another source registered. A dismissed finding is not rewritten. A finding in each
  other state gets the new descriptive columns, the source included. This design does not change
  that. Question 5 asks if a register is held to the findings that its source wrote.
- A surfaced mark, an update and a verification name a finding id and carry no source. The ten
  routes have no read by id. So a row can grant or refuse the verb, and it cannot limit the verb
  to the findings of its source. To limit it, Session KV must check the source: the listener
  passes the row's source with the request, and Session KV refuses a finding of another source.
  That is a change of Session KV. Question 5 asks if the approver wants it.
- The snooze expiry has no argument and changes rows of each source. Only the row of the nudge
  gets it.

| Thing that changes      | What                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| ----------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Broker verbs            | `findings.read`: the ranked list, the list, one day's additions, a publisher's record. `findings.write`: register, mark surfaced, update, record a verification, expire snoozes, write a publisher's record.                                                                                                                                                                                                                                                                                                                                                      |
| Broker route table      | A prefix for the profile role, default deny, with the checks above. The routes also join `REPORT_CREDENTIAL_ROUTES` by name: the weekly review and the nudge have no cluster target, so as scheduled runs they hold a report credential.                                                                                                                                                                                                                                                                                                                          |
| The table of streams    | New. One row for each built-in schedule that may write the queue: its verbs, its source, its check family, and the publisher that it may act as. The stream rows can be columns of the table that the write-permit design ([#6672](https://github.com/gke-labs/kube-agents/issues/6672)) renders, or a second table under the same rules: a version; an absent table, an unknown version or an unknown column means no queue write; the rows come from the PlatformAgent and the operator's table, not from the AgentSchedule object. See question 4.             |
| Listener                | It forwards each named route to Session KV on the pod's loopback, with the Session KV key that its container already holds for the watcher. Two facts for the decision: that key admits each Session KV route, so the listener's path check is the only limit; and the container outlives the agent's container, so these routes fail while Session KV is down, which the report routes do not. The other form, in which the listener opens the database file, needs a new mount in `agent-api-auth`: that container does not mount the `system-metadata` volume. |
| The report store design | [`fleet-audit-report-store.md`](fleet-audit-report-store.md) says that the listener serves the store routes and nothing else. The forward form on that listener changes that sentence; a second listener of the same form does not. The answer to question 2 decides which. With one listener, the change that builds the route edits the store design in the same pull request. This document does not edit it.                                                                                                                                                  |
| The scripts of a job    | A script reaches the queue through the broker when it runs on a profile, in the way that the report scripts reach the store. In a shadow run it skips each queue write, as the `dry_run` parameter says.                                                                                                                                                                                                                                                                                                                                                          |
| The platform's tools    | The inventory plans a home for the findings tools of `platform_control` on the broker relay. Those tools then call these verbs, under the same rule. This design adds no second path. One result needs a decision. Today the tool `update_finding` is the only writer of a person's decision (accept, snooze, dismiss), in a turn that the person started. Such a task has no trigger, so the proposed rule refuses it. See question 6.                                                                                                                           |
| Session KV              | No change with the forward form, unless the approver wants the source check of question 5.                                                                                                                                                                                                                                                                                                                                                                                                                                                                        |
| Conformance and bench   | One conformance row for each verb and each refusal, with its mutation, as for the report routes. The bench check `findings_queue_untouched` reads the rows of one source. It does not read the surfaced marks of other rows or a publisher's record, so a canary of the nudge needs a new check.                                                                                                                                                                                                                                                                  |
| Data                    | None moves. No migration.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |

What this design does not add: a copy of a finding in the report store, a copy of a report in the
queue, or a way for a profile to write the queue without a schedule row.

Who owns the rows: the findings routes, their entries in `REPORT_CREDENTIAL_ROUTES` and their
conformance rows are rows of this design. Each other broker route and verb is a row of the
broker-surface design ([#6670](https://github.com/gke-labs/kube-agents/issues/6670)). The findings
change takes its turn in that design's order of work on `credential_proxy.py`.

Where this differs from #5895: the snooze expiry is a write that no agent tool makes today, and
#5895 limited writes to where agent tools write; #5895 planned the routes on a Session KV listener,
while the listener that was built is a process of its own with no access to the database; and
#5895 named no rule for a shadow run.

## The dial and the post-merge state of group #6673

This code has an owner and changes each week. This section proposes no change of it. It states the
rows and the options, and it asks for the owner's view.

| Piece                                                | Where it is today                                                                                                                                                                                                        | Who uses it                                                                                                                                                                                                                                                                                                                                                                                                | A profile today                                                                                                      | Options                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| ---------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Dial record                                          | One JSON file under the Hermes home of the agent: the level, who set it and when, and for each kind of fix its repositories (`dial_state.py`).                                                                           | Session KV answers the three dial actions of the board. It checks the presser against the admin list that the operator pins, writes the record, and reports the level to the broker each minute. The Slack Home panel draws the dial from the board snapshot.                                                                                                                                              | No route, and none is needed to apply the dial: see the next row.                                                    | (a) It stays where an admin's action arrives. (b) It moves to a durable store on the broker's side. The broker has no durable store today, so (b) needs one.                                                                                                                                                                                                                                                                              |
| Level that the broker applies                        | The broker keeps the level that `POST /v1/dial` reports in memory, with a copy on its emptyDir (`DIAL_STATE_FILE` in `vcs_broker.py`). A new broker pod reads Explain until the next report.                             | The broker's write gate. While the dial switch is on, it refuses each branch and proposal write of each caller at Explain, and each write that adds the auto-merge label below Auto-merge.                                                                                                                                                                                                                 | **Applies already**: a profile's write passes the same gate.                                                         | No change. The gate does not move.                                                                                                                                                                                                                                                                                                                                                                                                        |
| Who may report the dial                              | `/v1/dial` admits the chat role only, and refuses a caller with no role.                                                                                                                                                 | The dial reporter in Session KV.                                                                                                                                                                                                                                                                                                                                                                           | Refused, and that is right.                                                                                          | The reporter stays one principal whose token no command that a model runs can present. A profile never gets the route.                                                                                                                                                                                                                                                                                                                    |
| Switches, admin list and the per-kind switch         | The install switches (`kubeagents_switches.py`), the admin list, and `automerge_switch.py`, which runs in the shell sandbox after a dial action.                                                                         | Session KV and the sandbox.                                                                                                                                                                                                                                                                                                                                                                                | No carrier.                                                                                                          | The dial does not work without them. They need a carrier when the sandbox goes away. See question 13.                                                                                                                                                                                                                                                                                                                                     |
| Post-merge records and the count of opened proposals | Tables `post_merge_records` and `proposals_opened` in the Session KV database. Routes: `POST /v1/post-merge/records`, `GET /v1/post-merge/clean-merges`, `POST /v1/proposals/opened`, `GET /v1/proposals/opened/counts`. | Writers: the `pr_opened` plugin of the platform's harness (a count for each claimed proposal; a record for a fix of a known kind, when the check is on), and the `post-merge-check` job, whose steps update each record in the database file. Readers: the same steps, the clean-merge counter, the dial snapshot, the `github-repo-watcher` job (from the file), and the chat metrics plugin (the count). | No route. A proposal that a profile opens gets no record and no count, because the plugin does not run in a profile. | (a) The broker makes the record when it serves `proposal-create`. The broker sees the repository, the proposal and the task. It does not see the finding, the workload or the thread, and it has no store and no route to the records. (a) needs the finding and the thread in the request, a store, and a route to the records. (b) A write route for a profile, in the form of the findings route.                                      |
| The post-merge check job                             | The `post-merge-check` job and its steps (`post_merge_*.py`), on the platform's scheduler.                                                                                                                               | The scheduler.                                                                                                                                                                                                                                                                                                                                                                                             | No carrier.                                                                                                          | It moves as a script run in the schedule track. It then needs to read and to update the records, which have no route today, and two verbs that the broker-surface design gives no grant until this group decides: `proposal-reviews` and `revision-contains`. The same holds for `proposal-practice-label`, with which the audit labels an auto-merge candidate. Where this work runs decides if the verbs need a grant. See question 12. |

## What stays out

- The incidents, the session front door, the schedules, the watchers and the delivery of
  reports. They belong to other tracks.
- The board and its actions.
- Any move of data off the agent's pod, and the retirement of that pod.
- Any change of `credential_proxy.py`, `session_kv_server.py`, `dial_state.py` or the post-merge
  scripts. This document changes no code.

## Questions

Each question has our recommendation, or "No recommendation", below it. A recommendation is a
proposal: the question stays open until its owner answers it. "No recommendation" means that we
have no ground for one, and it says what is missing. The reasons use facts of the code and of the
other designs that were read on `main` at `b9e487e8e`.

For the security approver:

1. Is option 1 with these verbs and this write rule the form to build for the findings queue, and
   is #5895 reopened for it, or folded into #6669?

   Recommendation: build option 1 as it is written here, with the write verbs for a profile that
   the answers to questions 3 and 6 grant. Fold the work into
   [#6669](https://github.com/gke-labs/kube-agents/issues/6669), which then links
   [#5895](https://github.com/gke-labs/kube-agents/issues/5895); that issue stays closed.

   Reason: the data does not move, and the form is the form of the report route, which is built.
   The plan of the closed issue differs from this design in three points: see the last paragraph
   of "The findings queue: one design". So a reopened issue would start from a plan that is out of
   date.

2. One listener for the report store and the queue, or a second listener of the same form? Is the
   forward form acceptable, given the two facts in the table?

   Recommendation: a second listener of the same form, with the forward form.

   Reason: the report listener keeps its one duty and its sentence in
   `fleet-audit-report-store.md`. The path check is the only limit of the Session KV key. It is
   then in a process that serves the findings routes and nothing else. The forward form needs no
   new mount in `agent-api-auth`. The costs: a second port with its own NetworkPolicy rule and
   token check, and routes that fail while Session KV is down.

3. Which write verbs may a run that no person started call? An update holds a person's decision
   (dismiss, snooze, accept) and the pull request fields. A verification can resolve a finding.

   Recommendation: for a script run with no model turn, the register and the surfaced mark. The
   row of the nudge also gets the snooze expiry. No update, no verification and no publisher's
   record. A stream whose model turn reads a tenant's text gets a verb only by the approver's
   decision, verb by verb. No stream row grants the update or the verification (question 6).

   Reason: the register, the surfaced mark and the snooze expiry are the writes that the two
   scripts of scheduled jobs make today (`upgrade_readiness_review.py`, `findings_nudge.py`).
   `inventory_findings.py` registers in the ranking step of the first-run flow, which is a model
   turn. Its verb goes with the first-boot track
   ([#5623](https://github.com/gke-labs/kube-agents/issues/5623)) and the approver's decision.
   Only the platform's tools call the update, the verification and the publisher's record. The
   write of a publisher's record stays ungranted. No job writes one today: the backlog publisher
   is not built, and the nudge keeps its state in a file. O4 keeps a register, a verification and
   a decision out of the weekly brief's turn, because that turn reads a tenant's text. We propose
   the same rule for each such stream.

4. Is a row for each built-in schedule the right bind for a queue write, and does the operator
   render the table? One table with the write permit, or two? Is the read allowlist the report
   route's list, or a list of its own? Does a read need a schedule trigger, or a hold to the
   task's cluster?

   Recommendation: a row for each built-in schedule, which the operator renders, as columns of the
   permit's table. The read allowlist is a list of its own. A read needs no schedule trigger. A
   task with a cluster target reads the findings of that cluster only. A task with no cluster
   target reads each cluster. A team profile's task is held to its namespaces when the namespace
   bound of [#7354](https://github.com/gke-labs/kube-agents/pull/7354) exists. Until then, no
   team profile is on the read allowlist. With a yes, "Who may read" changes: it says today that a
   read is not held to the task's target.

   Reason: a stream is the schedule's own name and a task cannot choose it, so the schedule is a
   bind that the broker can check. The write-permit design offers these columns, and its
   conditions for a rendered table then hold for a queue row too. The columns have four costs. A
   reader that does not know a column reads the table as empty. So a release that adds a column
   refuses each permitted write until both readers know the column. A schedule with no permit
   opt-in still has a row, with the queue columns and no write family. Such a row must not count
   as "the table lists the schedule" for the mint of a permit (row 5 of that design). An install
   without the built-in admission policy gets no table, so it gets no queue write. Row 4 of that
   design has an open point on the trust of the profile and its images. If its answer needs
   images by digest, a queue row needs them too, and no install pins an image by digest today.

   The queue holds today what no stored report holds: each person's decision, and the findings of
   the sweep and of the readiness review. So the report list does not say who needs the queue. A
   person's question about the fleet is a task with no trigger, so a read cannot need one. The
   nudge, the weekly review and the weekly brief have no cluster target and need each cluster, so
   they keep the whole read. A task that is confined to one cluster needs only the findings of its
   cluster. The text of a finding can come from a tenant. The list route takes a cluster filter
   today, so the broker can set it from the task's target. The ranked list and the day's additions
   return the whole queue. They need a filter in Session KV, or they stay with the tasks that have
   no cluster target.

5. Is a verb that names a finding id held to the findings of the row's source? That needs a check
   in Session KV. Is a register held to the findings that its source wrote? Which stream may act
   as which publisher? A surfaced mark that names a paced publisher spends the pacing budget of
   the nudge.

   Recommendation: yes for a row that names a source. Its verbs with a finding id are held to that
   source, with the check in Session KV. The row of the nudge names no source: it marks the
   findings of each source, as it alone gets the snooze expiry. A stream acts only as the
   publisher that its own row names. That is `nudge` for the nudge; `first_report` stays with the
   first-run delivery and its track
   ([#5623](https://github.com/gke-labs/kube-agents/issues/5623)). No recommendation for the
   register: what is missing is how often a finding changes its source in a real install
   ([#3561](https://github.com/gke-labs/kube-agents/issues/3561)).

   Reason: these routes have no read by id, so the broker cannot hold a verb to a source. Without
   the check, a row that registers one source marks the findings of each source. The nudge reads
   the ranked list of each source and marks what it shows, so its row cannot have that hold. A
   surfaced mark that names a paced publisher spends the day's budget that the nudge reads,
   whichever paced publisher it names. So a stream must not name a publisher that is not its own.
   A hold on the register changes which writer keeps a finding, and
   we did not measure what that breaks.

6. A queue write with no trigger is refused under the proposed rule. Then no profile task writes
   a person's decision. Which one: (a) the decision stays with the session and the board
   (option 3), and no stream row grants a change of a decision; or (b) a task that a person
   started writes a decision, with a hold of its own? Does a queue write of a live scheduled run
   also need the permit of [#6672](https://github.com/gke-labs/kube-agents/issues/6672)?

   Recommendation: (b), for the update only, with this hold: only a task whose chain a person
   started. The allowlist condition stays, and no stream row grants the update. The verification
   stays ungranted until a duty needs it. A queue write of a live scheduled run needs no permit.

   What (b) changes: it is a second write path beside the schedule-row rule. For the update, a
   task needs no trigger and no stream row. With a yes, four places of this design change. They
   are rule 3, "Who may write", the row "The platform's tools", and "What this design does not
   add".

   What (b) depends on: the task credential has no fact for "a person started the chain". A `read`
   child of a scheduled run has no trigger too, so the broker cannot tell the two apart today.
   (b) needs the interactive-origin fact of
   [`scheduling-on-profiles.md`](scheduling-on-profiles.md)
   ([#6790](https://github.com/gke-labs/kube-agents/issues/6790)) in the credential. A queue write
   with no trigger stays refused until the broker can read that fact.

   Reason: today the platform's tool writes a person's decision, and nothing in the code holds the
   tool to a turn that a person started. With (b) the writer is a profile task through the broker,
   not the platform's tool server. The last "Done when" line for #6669 needs only accept, snooze
   and dismiss, and the update holds these three. (a) is the stricter rule. Its cost is a writer
   at the front door, which does not exist and which question 10 leaves to the retirement step.

   The risk of (b) is a turn that a person started and that reads a tenant's text. It can write a
   decision that the person did not give. One such write can be large. A dismiss is sticky: a
   later register does not open the finding again. A decision also covers the other shown rows of
   the same item. A hold to the task's cluster needs the Session KV check of question 5.

   The permit gates a change of a repository. A queue write changes a record of the install's own
   store, as a report write does, and the stream row is its gate. With the permit as a second
   gate, an install that gives no permit stores each report and registers no finding.

7. Does [#6572](https://github.com/gke-labs/kube-agents/issues/6572) carry the daily recap and the
   stall-watch row of the ledger, or does the recap need the `events.read` endpoint of O3, and who
   builds it?

   Recommendation: that issue carries the recap. A reader that runs on a profile (the recap, the
   `get_alert_history` tool) gets the `events.read` endpoint of O3, in the form of the findings
   read. The track that moves the first such reader builds it. That endpoint is a new read for a
   profile and needs the approver's decision; this design still builds no events route. No
   recommendation for the stall-watch row: the text of the issue does not name it.

   Reason: [`scheduling-on-profiles.md`](scheduling-on-profiles.md) (section 3, item 4) moves the
   two watchers of that issue in two phases. In phase 1 the daemon keeps each gate, and the ledger
   that the daily recap reads is one of them. So phase 1 of that issue moves no reader. O3 says
   that no job opens the database file, and the ledger has two readers.

8. Does the move of the triage start carry the cap of open triage pull requests?

   Recommendation: yes. The cap goes where the triage start goes, in the same change.

   Reason: Session KV applies the cap at a triage start, when the install turns it on. A start
   that moves without the cap does not hold the limit of such an install.

9. Which incident rows of the inventory does the work on thread incidents cover: the tables, the
   `apply` record, the `recent` list, the `incident_context` plugin? A row that it does not cover
   needs a row here.

   Recommendation: No recommendation. Which rows that work covers is a fact of that work, and what
   is missing is the list from its owner. One fact for a row that it leaves: the
   `send_notification` tool posts an incident to `/v1/incidents` during a run of the platform.
   That row is a write during a run, so option 3 does not fit it.

10. The rows with option 3 stay in Session KV today. What holds them after Session KV goes away
    is not in this design. Is that the retirement step, or a row for the front door now?

    Recommendation: the retirement step.

    Reason: nothing in the epic needs a move of data before that step. A row now would design a
    store for the front door before the front door has a store design.

The owner of the dial and post-merge code decides questions 11 to 13. Our recommendations there are
proposals for the owner to change or decline.

For the owner of the dial and post-merge code:

11. Where does the dial record go: option (a) or (b)? When it moves, who checks that the presser
    is an admin? Who reports the level to the broker when the chat-role reporter in Session KV
    goes?

    Recommendation: (a) for now. When Session KV goes, the component that receives the board's
    action checks the admin list and is the one reporter.

    Reason: the broker's gate applies the level to a profile's write already, so no profile duty
    needs the record in another place. (b) needs a durable store on the broker's side, which the
    broker does not have. The reporter stays one principal whose token no command of a model can
    present.

12. Which form for the post-merge record, (a) or (b)? The same question holds for the updates
    that the check job makes, and for the count of opened proposals. Where does the check job
    run, and who labels an auto-merge candidate? That decides if `proposal-reviews`,
    `revision-contains` and `proposal-practice-label` need a grant for a profile.

    Recommendation: (a) for the record and for the count, with the records where they are. The
    broker writes them through a broker-only listener, in the form of the findings route. We
    propose the listener of question 2 with these routes added. That listener then serves more
    than the findings routes, so that sentence of the reason of question 2 changes. This adds a
    route, so it also needs the security approver. No recommendation for the updates of the check
    job, for where that job runs and for who labels a candidate. What is missing is the owner's
    plan for that job after the scheduler moves.

    Reason: the broker serves each `proposal-create` of each caller, so the write no longer
    depends on a plugin in the worker. A record still needs the finding id, the cluster, the
    object and the origin in the caller's request. Only a fix of a known kind gets one. The count
    needs an origin too, and this text does not say which origin the broker gives a profile's
    proposal. The table names a store and a route for (a). With the listener, the listener is the
    route and the store stays the Session KV database, so (a) needs no store on the broker's side.
    That agrees with the recommendation for question 11. A proposal of the platform then has two
    writers of its record, the plugin and the broker. The change names one of them.

13. What carries `automerge_switch.py` and the switches when the shell sandbox goes away?

    Recommendation: No recommendation. What is missing is the answer to open question 6 of the
    broker-surface design ([`profile-broker-surface.md`](profile-broker-surface.md)). Until then
    the verbs `proposal-close` and `branch-delete` have no grant for a profile. A move of this
    script adds a grant, so it also needs the security approver.

    Facts for the answer: an admin's dial action starts the script today (Session KV,
    `dial_state.py`), and a pending check runs it with `--check`. The header of the script, which
    says that nothing calls it yet, is out of date. For each repository named, one run can close
    the pending pull request of the opposite state and delete its branch. It can then open one new
    pull request to `.kube-agents/automation.yaml`, with `publish` and `proposal-create`. A profile
    task reaches those two verbs only with the write permit
    ([`unattended-write-permit.md`](unattended-write-permit.md)). The operator delivers the
    install switches to the agent pod's managed `.env` and to a root-owned file in the shell
    sandbox (`kubeagents_switches.py`). The broker reads its two switches from its own
    environment. So the copy in the sandbox is the one that needs a carrier.

## Done when

The security approver confirms these lists in this document.

For #6669:

- Each Session KV table and each state file that #6669 and section (e) of the inventory name has
  a decision: a route, "stays where work starts", or "moves with another track", with its issue.
- No change for #6669 adds a store, a table or a file for a report or for a job's state.
- A profile task on a built-in schedule registers and reads findings through the broker, with a
  conformance row for each verb and each refusal. One of the refusals is a write from a stream
  that ends in the canary suffix.
- A shadow run of a job that registers leaves the rows of its source as they were: the check
  `findings_queue_untouched` passes for its canary row.
- A job on a profile reaches the three queue tables only through the broker. Two jobs open the
  Session KV database file for other tables. The daily recap opens it for the events ledger; it
  moves with the track that the answer to question 7 names. The first-run delivery opens it for
  `audit_report_runs`; it moves with the first-boot track
  ([#5623](https://github.com/gke-labs/kube-agents/issues/5623)). The jobs that open the file for
  `post_merge_records` are in #6673.
- No existing findings data is lost: the data did not move, or a migration test shows the move.
- If the findings routes use the listener of the report store, `fleet-audit-report-store.md` says
  so.
- A person's accept, snooze and dismiss of a finding, given in chat, reach the queue after the
  findings tools move, with a test. This holds for each answer to question 6.

What these lines need from other work: the built-in table ships no row today, so the third and
fourth lines need a row of our own there, agreed with the owner of that table, and the script run of
the schedule track ([#4685](https://github.com/gke-labs/kube-agents/issues/4685)). The fourth line
also needs the store-only finish ([#6956](https://github.com/gke-labs/kube-agents/pull/6956)). If
question 6 is answered yes, the third and fourth lines also need the write permit. If question 5 is
answered yes, the third line gets a refusal for a finding of another source. The proof on a real
duty is a pair of cases for the weekly upgrade-readiness review, one on today's platform agent and
one on the profile. That review reads the fleet, which a report credential cannot do today (blocker
4 of the inventory), so that pair is a line of the track that moves the review.

For #6673:

- The decision on the dial record and on the post-merge record is written in this document, with
  the owner's agreement.
- A proposal of a profile that fixes a finding of a kind that the post-merge check knows has the
  same record as a proposal of the platform, with a test.
- A conformance row holds that the profile role cannot call `/v1/dial`.

What the second line needs from other work: the answer to question 12, a store or a route for the
record, and the script run of the schedule track.
