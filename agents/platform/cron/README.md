# The Platform Agent's cron roster

`jobs.json` is the Platform Agent's own cron store. This file holds the rules
for editing it, because the store itself cannot: `cron/jobs.py::_save_jobs_unlocked`
writes `json.dump({"jobs": jobs, "updated_at": ...})`, a fresh dict with exactly
two keys, so any top-level `_comment` a shipped roster carries is destroyed by
the first tick. The live pod confirms it — `/opt/data/profiles/platform/cron/jobs.json`
has top-level keys `['jobs', 'updated_at']` and nothing else. An explanation kept
in the JSON survives in git and vanishes everywhere it would actually be read.

Per-**job** keys do survive that rewrite (the job dicts are dumped verbatim), but
the roster does not use them for prose: the reasoning belongs in one place, and
this is it.

## This roster is not inert

The gateway's own ticker is one thread bound to one `HERMES_HOME`, and this image
runs a single gateway homed at `/opt/data`, so the thread only ever ticks the Chat
Agent's store. What reaches this one is `profile-cron-tick`, a `no_agent` job on
that store which runs `hermes cron tick` against every named profile with work due
(see "What fires the schedule" in
[`autonomous-watchdogs.md`](../../../docs/site/src/content/docs/concepts/autonomous-watchdogs.md)).

An enabled entry here therefore fires in its own process, with this profile's
persona and toolsets (unless the entry's `enabled_toolsets` replaces them) and the entry's own `skills`, `model` and `max_turns` — which is the whole reason
the watchdogs live here rather than as kanban cards filed from the Chat Agent's
roster. A card is not a cron run, and that indirection is what stopped `skills`,
`model` and `deliver` reaching the thing that ran.

## When a card _is_ the right shape

Read the paragraph above as being about watchdogs, not as a rule that a cron job
may never file a card. A watchdog fires unconditionally and its product _is_ the
delivery, so routing it through a card loses the run. A poller is the inverse: it
has nothing to deliver on almost every tick, its product goes to GitHub, and it
owes a model turn only when real work exists — which is why `github-repo-watcher`
is a `no_agent` script that costs nothing when idle and files a card when it
finds something. It still names an audible `deliver` for itself — `"chat"` — like
every other report-producing entry here, so a sweep that cannot run still says so. Being
`no_agent` changes what it delivers, not whether it does: a clean tick prints
nothing and relays nothing, and only a sweep that failed produces text.

Adding a sweep is one line in `github_scan_gate.py`'s `SWEEPS` registry —
`SWEEP_ORDER` is derived from it, so there is no second list to keep in step —
not a new cron entry. The consequences of dispatching through a card are in
[`docs/designs/pr-comment-conversation.md`](../../../docs/designs/pr-comment-conversation.md) §2,
and the env knobs that bound a sweep are in §§2 and 4 of the same document.

## `stall-watch` hands a stall to the AutoOps pipeline

`stall-watch` is a `no_agent` script: the tick prompts no model. Every thirty
minutes it lists the clusters of the management project and of every project a
Cluster Agent profile's `cluster_identity` names, which is how a project
`spec.scope` brings in reaches the watch, and, for every namespace of every
running or reconciling cluster that has a Cluster Agent profile and is not a
system namespace, runs the Cluster Agent's `stall_report.py` over a bounded list
of controller kinds, keeping a ledger of the rows it has seen. On a new stall
episode it sends the Session KV server a `controller-stall` inject, the route
`k8s-event-watcher` and the drift detector use
([design](../../../docs/designs/stall-watch-inject.md)). The server posts the
alert, records its chat thread, and starts a Planning Agent turn that files one
card to that cluster's Cluster Agent, which runs `gke-stall-detection` and
reports in the triage template. The report lands in the alert's thread and is
saved, so a reply of "apply" there reaches the agent that opens the pull request
with the report attached, rather than a bare word it has to investigate from
scratch. The watch files no card itself: a card it filed would have no session,
and so no thread. It finds the card later by `tasks.session_id`, the session that
filed it. The watch follows the reconciler's roster: a cluster with no profile,
one `spec.scope.exclude.clusters` or `RECONCILE_EXCLUDE` pruned or one not yet
scaffolded, is neither read nor alerted for, because the exclusion is the
operator keeping a model turn off that cluster and an alert would hand its rows
to another profile; a cluster, or a whole project, that leaves the roster has its
rows cleared and its open card completed with a comment saying so. At most three
alerts are raised per tick (`MAX_ALERTS_PER_TICK`, the default of the
pull-request poller's `PR_AGENT_MAX_PER_TICK`), since each starts a Planning
Agent turn and a Cluster Agent turn and the number of namespaces with a new
stall is chosen by whoever can create namespaces. A stall claims no alert quota
on the Session KV server, so that cap is the only bound. The rest keep their
rows and wait, oldest first sighting first, so a tenant filling three fresh
namespaces every tick cannot keep an older stall from its alert, and chat gets
one line saying how many wait. An alert the server refused, or a server that
does not advertise the kind on `/healthz`, leaves the namespace with its rows
for the next tick, and the tick stops trying the rest rather than spend a
timeout on each; the "alerts not raised" line below reports them rather than
the held count. A record the server refuses on its own merits (HTTP 400) skips
only that namespace, and the next one in line still gets its alert. Object
names longer than the server's 200-character limit are cut to it in the
record. A new object in a namespace whose card is still open is a comment on that
card; when every object in the namespace has cleared, the card gets a closing
comment and is completed. A `repeating-warnings` or
`dangling-reference` row clears only after two consecutive scans without it, so
a warning that recurs hourly or a referent listing that failed once does not
close and reopen an episode.

`deliver: chat` carries the watch's own one-liners: "stall cleared in
`<project>/<cluster>` (`<location>`) / `<namespace>`: `<objects>`; card `<id>`
closed" (without the card clause when the alert produced no card; a card
filed later still reports the recovery in the alert's thread), "stall
noticed in `<n>` more namespaces; alerts follow on later ticks, 3 a tick", the
cleared line naming at most eight objects, and an "alerts not raised" line when the Session
KV server refuses an alert, said once until the reason changes, with a line
when alerts are raised again. It also carries the sweep-failed and sweep-recovered lines
every roster entry owes, and the budget lines: the budget-exhausted line, said once on the
tick a sweep first stops at its 25-minute budget, carrying the clusters read of the
ones it could sweep, the namespaces read, the clusters it could not read and the cluster the
next tick resumes at, and "the
fleet is swept in one tick again" once a sweep completes having listed every project the
exhausting sweep listed (a sweep that fit without one of them, because its listing failed or an
identity could not be read, says nothing about the fit and posts nothing, for up to three
consecutive ticks, after which the missing project counts as a standing fault and the fit
stands); the ledger's budget entry carries the same coverage on every tick that falls short,
and says when and why the flag is held. A new stall's notice is the alert the Session KV server
posts. A namespace whose alert has produced no card a day after it was raised
(the Planning Agent's turn failed) has its alert raised again, and the new
alert replaces the episode once it is sent; a shorter retry would post a fresh alert every hour or so
for as long as the turns kept failing. Alerts are not raised at all on a board
whose tasks table has no `session_id` column, since the card an alert produces
could not be found. The watch saves its ledger before each inject and fails
the tick, with no alert, when the ledger cannot be saved, so a ledger that keeps failing
to save cannot raise the same alerts every tick. A clean tick prints nothing. Anything a tick
could not read (a
cluster that timed out, a namespace whose scan failed, the rows of a kind a scan skipped or the repeating-warnings rows of one that could not read the events, a project whose listing failed or that gcloud called incomplete, a cluster whose profile's `cluster_identity` could not be read and whose project nothing else lists) keeps its rows and is recorded in the ledger, not posted;
a sweep that hit its 25-minute budget keeps the rows it did not reach the same way, records
what it covered, posts it on the first tick it falls short, and resumes where it stopped. A cluster's
credentials are fetched on its first tick, after a day, and after a read that says the kubeconfig
itself is the problem (the shim refused a stub the sandbox lost, or kubectl met a refused connection or a changed
certificate at a recreated cluster); otherwise the tick reuses the kubeconfig it has, and a read
that timed out, that the proxy refused, or that an unanswering control plane failed leaves the
record for the next tick, and writes one when it followed a fetch (so a recreated cluster whose
old endpoint times out or no longer resolves, rather than refusing, is read again at the daily
refresh, not before). One project's listing failing holds
only that project's rows. The sweep fails, and posts the sweep-failed line, when
every project's listing fails, when no management project resolves, when
`stall_report.py` cannot be found, or when the sandbox is lost.

Every `gcloud`, `kubectl` and `stall_report.py` call runs in the shell sandbox
through `sandbox_exec`, because the agent container carries no kubectl (with the
sandbox switched off the calls run locally and fail with `No such file or
directory: 'gcloud'`, the same failure every `sandbox_exec` caller sees in that
state). The script itself travels on the command's stdin, read from the agent
image's copy in `/opt/defaults/scripts` and run with `python3 -I -`: what the
`hermes` login executes is never a file under the sandbox's agent-owned
`/opt/data`, and isolated mode keeps that directory, which is the command's
working directory, off the module path, so a `json.py` the model dropped there is
not the code that runs. The per-cluster kubeconfigs stay in
`/home/hermes/.kubeconfigs`, where `platform_mcp_server.py` keeps its own, under a
prefix of their own. The kind list is `DEFAULT_KINDS` in the script, cut per
cluster to what `kubectl api-resources` says it serves. The CR's
`spec.harness.stallWatch` sets three knobs, which the operator pins in the
managed `.env` on every install (empty when unset, which is the default):
`STALL_WATCH_EXCLUDE_NAMESPACES` names namespaces never read,
`STALL_WATCH_MIN_AGE_MINUTES` holds a new alert until a stall in the namespace
has lasted that long, and `STALL_WATCH_KINDS` replaces the kind list (`all`
hands `stall_report.py` its every-kind default). The annotation
`kubeagents.x-k8s.io/stall-watch: exempt` leaves a namespace unread, or an
object's rows out of the alerts; `stall_report.py` marks such a row `exempt`
in its `--json` output and its table is unchanged. The k8s-event-watcher and
this job split the work by signal: a Warning whose reason is on the watcher's
list is the watcher's within seconds; a condition, a reference or an event the
list never names is this job's within the half hour. It declares `risk: high`
because every alert it raises starts a Cluster Agent turn over every namespace of
every cluster on the Cluster Agent roster, the management cluster included
unless `spec.scope.exclude.clusters` or `RECONCILE_EXCLUDE` names it. The inject record and chat lines carry object
names, heuristics and durations only, never a row's detail, since condition
reasons, spec paths and event messages are text a tenant writes; the Cluster
Agent reads that text again when it runs the skill, and its read-only skill and
preflight bound what it does with it.

## `upgrade-readiness-watch` reports before anyone asks

The `fleet-upgrade-verification` skill's readiness report grades every cluster
on what would stop its next upgrade. Until this job it ran only when a user
asked in chat. `upgrade_readiness_watch.py` runs once a day and asks the cheap
half first: it runs the skill's version table, with no readiness read, and
collects the target version each cluster's release channel offers and which
clusters sit below it. A version a cluster is below is pending; a pending
version the job's ledger has never seen earns the full readiness report at
once, and a version already reported is refreshed every seven days while any
cluster is still pending it. A version no cluster is pending any more is
retired from the ledger with one line. A tick with nothing due prints nothing,
so a quiet day costs no message.

Which clusters it reads is decided on the agent pod: `UPGRADE_READINESS_PROJECTS`
when set, otherwise the management project and every project a Cluster Agent
profile's identity names, the roster `stall-watch` follows; with neither, the
sandbox's configured project; and with nothing at all the tick fails closed,
because on an install that declares no scope the report script's own fallback
is every project the credential can list. A roster spelling with a space, a semicolon, or a
comma inside one project's name is not a project id or number, so it fails the tick with the
places to correct it. Within those projects the report script
enumerates every cluster, so a cluster `spec.scope.exclude.clusters` keeps a
Cluster Agent from is still read here. On an install that declares a scope (a
`spec.scope` block), the report script, a fleet-audit collector, refuses a bare
`--project` in the sandbox, so the job reads the declared scope on the agent
pod (`fleet_scope_targets.declared_scope_targets()`) and hands the script the
resolved scope beside each `--project`, the `collector_args` the
platform_control `fleet_scope` tool hands the SOPs less the cluster exclusion
(`--scope-projects`, `--scope-unread`): the script reads a project inside the scope and refuses one
outside it, one listed but unread, a project number it cannot resolve, and any project in a
sandbox still marked scoped after the scope is gone, each with the reason; the watch adds the
install's remedy, and the table carries each as that project's failure, and a tick whose every project is
refused fails, announced once. An install that declares none keeps the bare flag, and a scope
render the pod cannot read fails the tick with the reason. The cluster read is `gcloud` cluster metadata
for the version table and, on a report day, the readiness report's read set,
the same as a run on request: three `gcloud` list reads per project (backend
services, instances and GKE operations, projected to the fields the rules read)
beside the server config per location, then one `get-credentials` and six
`kubectl get`s per cluster (budgets, Deployments and StatefulSets; webhook configurations
with their Services, EndpointSlices and Namespaces; DaemonSets, CronJobs,
claims, NetworkPolicies and Namespaces; every pod; the nodes through a
jsonpath projection; StorageClasses and PersistentVolumes), plus
`gcloud logging read` over the Admin Activity log when a registered rule reads
it; the saved report names those objects, pods and nodes among them, and a
fail-closed webhook with no reachable backend names its configuration and its
Service, which is tenant-written text, so the entry declares `risk: high` as
`stall-watch` does. A project
spelled by its number is keyed by the id `gcloud projects describe` resolves,
learned on its first table run. A number the credential cannot resolve is
never a key, so its clusters earn no report until it resolves: when nothing
was read under a key (it is the only project, or every other is an unresolved
number too), the tick fails, posted once as a plain line ("The upgrade readiness
check didn't run today", why, and that an admin needs to fix it, since no retry
resolves the number) with the reason and the remedy in
`logs/upgrade_readiness_watch.log` under the agent home, which fluent-bit
ships, and kept in `last-failure.txt` by the kind of failure until the next
clean tick posts the recovery. A failure a retry may clear (a timeout, a
sandbox that failed) says instead that it tries again tomorrow, or at its next
scheduled run when its roster entry is not daily;
beside projects that were keyed, the table is partial, posted once with the
reason, and the ledger's `table_errors` keeps it. The remedy is
`resourcemanager.projects.get` on the credential (every read role the install
binds carries it, as `cluster_agent_reconcile.py` relies on, so a credential
without it is a custom grant) or the project id in place of the number
(`spec.harness.projectId` has to be the id; `UPGRADE_READINESS_PROJECTS` for a
hand run).

It runs the skill the way `stall-watch` runs `stall_report.py`: every module
under the skill's `scripts/` except its tests, with the data files beside them,
is read from the agent image's `/opt/platform-template/skills/` copy and handed
to `python3 -I -` in the sandbox on stdin, behind a loader (a meta-path finder
serving the shipped set, the agent image's `fleet_scope_args` among them so the
script formats a refusal with the constants the job parses it by) before
running `fleet_upgrade_report.main`. The
loader gives the report a private `tempfile.mkdtemp` directory for its JSON
output and rollout record, so nothing the model can write to is on the path
the result crosses, and removes it afterwards; the sandbox's `/opt/data` is
not this pod's, so the loader prints the report back as JSON and the job
writes the files on this side, under `<agent home>/upgrade-readiness/`:
`ledger.json`, and one dated `.md` and `.json` per report under
`reports/<version>/`, with `latest.md` pointing at the newest, the ten newest
kept per version and a retired version's directory removed with it. Each run starts
from an empty rollout record, so a saved report's progress table is a
first-run baseline, not a week-over-week comparison. Its stdout, one line per
report with the pending clusters, the blocked ones, and the file's path on the
gateway pod, is what `deliver: "chat"` posts; the agent's own tools run in the
sandbox and cannot open that path, which is why the line carries the verdicts.
`--dry-run` prints what a tick would do and changes nothing, the failure
marker included.

Daily at 10:10 UTC, after the morning audits and before the US day; the
version table is a handful of `gcloud` list calls and the readiness report
runs only on the day a version appears or its week comes round, once per
project that holds a pending cluster with its own timeout, so one project the
sandbox cannot finish leaves only its own clusters ungraded and the others'
versions still reported. The whole tick keeps to forty-five minutes of the
hour Hermes gives a `no_agent` script: the version table and the readiness
runs go project by project, the table within the first fifteen minutes and the
readiness runs with the rest, each run gets what its share has left, a project
the budget cannot reach or finish is left unread and named: the partial-table
line names each project with a read error and its reason (the budget, the
sandbox's error, a failed listing, an unresolved project number, a refusal under the
declared scope with the install's remedy), the
ledger's `table_errors` keeps the last tick's, and a version's own line names
the project whose readiness run was not started or was cut short (a version
none of whose projects ran is not recorded and is retried tomorrow),
and each sweep runs the projects least recently run first, so a budget that
never reaches the end of the list does not leave the same project unread
twice, whichever versions are due. Three guards
keep the ledger honest: a version is
retired only when the version table read every project, or read every project
that still pended it and none does, or a readiness run (which lists every
cluster of its project, reached by the table or not) showed every cluster
pending it as no longer below it, a report that
graded none of a version's pending clusters is written but not recorded (the
chat line says "none graded" and the version is tried again tomorrow), and the
weekly comparison carries ten minutes of slack so the tick's own drift cannot
push a refresh to day eight. A partial table, an ungraded version or a failed
tick is announced once, not daily: the ledger keeps what was announced (the
kinds of read error for a partial table, and for a refusal the project and
its cause; each project and how it went ungraded for a version, never the
error text, which an outage rewords from one run to the next), a
failed tick (which writes nothing to the ledger but its record of the projects
the hops refused as outside the declared scope and the ids it learned for
numbered spellings, kept at whichever step the tick failed unless the ledger
itself cannot be written or re-read, so the next tick files them under the same cause)
keeps its key in
`last-failure.txt` beside it (the plain reason, or, for a tick whose every run was refused, the
refused projects and their causes), and the next line is the recovery. A version
none of whose clusters can be read is retried
three days running and then recorded, so an unreachable cluster costs its
project one readiness sweep a week rather than one a day; a `blocked` verdict
the script grades from cluster metadata stands even when the kubectl read
failed. A hand run in the container finds the ticker's ledger under the
platform profile, as `feedback-prompt` does, rather than starting a second one
at the gateway's home. The three
environment knobs (`UPGRADE_READINESS_REFRESH_DAYS`, `_PROJECTS`,
`_WATCH_HOME`) are for a run started by hand in the pod; the operator's
`spec.deployment.env` allowlist does not carry them.

## `upgrade-readiness-weekly-review` and `upgrade-readiness-weekly-brief` are one Saturday in two jobs

The daily watch reads a cluster only while a release channel offers it a version it is below, and
posts one line per report. The weekly pair reads every cluster of the roster's projects whatever its
version (a declared scope refuses a project outside it), and ends in one message a person reads.

`upgrade-readiness-weekly-review` (`0 6 * * 6`) is a `no_agent` script on this pod. It runs the
`fleet-upgrade-verification` readiness report in the sandbox through the watch's own loader, one
run per project inside the watch's 45-minute tick budget (Hermes kills a `no_agent` script at an
hour), and turns every blocker and risk the report names (a drain-blocking budget, a fail-closed
webhook, a pool past or at the skew ceiling, a covering exclusion, and each readiness rule's rows
once the report carries rules) into a finding on the findings queue, through the Session KV
server's `POST /v1/findings` route, reached at the endpoint and with the key `findings_nudge.py`
uses. Each project is registered as soon as its run returns, so a run the hour cuts short keeps
what it registered. The sends have ten minutes of their own past the reads, each waiting no longer
than what is left, and a batch with no time left is booked as not sent, so a queue that hangs
cannot hold the run past the hour before it writes its record. The queue's identity rule makes next Saturday's run an update of the same row;
its rubric sets the severity (a budget over a single replica, a pool past the skew ceiling and a
webhook in the upgrade's path are critical; a budget over several replicas, a pool at the ceiling
and a webhook refusing only what it matches today are major; a covering exclusion is minor; a
readiness rule's blocker is critical, and its risk is minor for catalogue entries 9, 10, 11, 13, 16 and 25 and major for every other entry, and minor for the plan-level `rollout-sequence-precheck`, as the rubric's
tables set it); a row
the user dismissed stays dismissed; and the hourly nudge paces what is named and reminds daily of
undecided criticals. A cluster read in full sends its batch with a `scope` naming the job's check
family (`upgrade-`) and its own source, an empty batch when nothing was found, so a finding the week
no longer reports loses confidence and stops being reminded without touching another family's rows
on that cluster, or the `upgrade-` rows another writer filed there. Three kinds of cluster claim nothing: one
with more findings than one batch holds (500), sent in pieces; two of one name in one project, whose
claims the queue could not tell apart; and one with a row the queue would refuse, since the claim
would book that finding's row absent. A cluster with no upgrade target (no release channel, or a
server config that did not answer) is never read in full, since the grader cannot judge its pools'
skew, so it claims nothing either: a budget or webhook row there that is fixed stays on the queue
until the user decides it. The same holds for a cluster on which a readiness rule failed or left an
`unknown`, even a rule that cannot block: a claim would lower that rule's own rows, which the run
could not read again. A cluster the listing no longer returns (deleted, renamed or out of scope) is
not in the run at all, so nothing is sent for it, and its rows stay until the user decides them. On an install with no declared scope, a roster that names one
project both by number and by id runs it twice until the daily watch has learned the number's id,
and on a Saturday `gcloud projects describe` fails, the number's run is booked as a project not
read; name the project by its id alone to avoid both. The script writes the technical report under `upgrade-readiness/weekly/` beside the
watch's ledger, keeps the newest eight runs, and places a copy of `latest.md` and `latest.json` in
the sandbox at `/opt/data/upgrade-readiness/weekly/` so the agent can read it when asked. It prints
nothing on success: the brief speaks for the week and the queue for each finding. A project it could
not read is one line naming it; a batch the findings queue refused, or a sandbox copy that failed,
is one line each, since both leave the week's findings where the brief cannot act on them; a
failure of the run is one line, once per distinct failure (numbers aside), with the recovery line
on the next clean run. The entry declares `risk: high` as the watch does: the budget, pool, exclusion and webhook names it reads are tenant-written, and they reach the queue, the report, the sandbox copy and, by queue id and object name, the brief in chat.

`upgrade-readiness-weekly-brief` (`0 7 * * 6`, an hour after the review starts and past its
budget) is the model turn, and the reason the pair is not one job: the review can rank by rubric
but cannot tell that two criticals on one cluster are one problem, or that a critical whose clock
ends next week outranks one with no date. Its SOP (`governance/upgrade_readiness_brief_sop.md`) has it
read the review's summary, the queue's open backlog (the rows whose `check` starts with `upgrade-`)
and its snoozed and dismissed rows (read by state, since the backlog never carries them), and
compose a brief of at most twenty-five lines: the week's headline, the
undecided criticals that need a decision (one line per cluster, at most ten, with one clause of
why and one action each), the rest of the week as counts, what was decided earlier, any project the review could not
read, and where the full report is with the sentence that opens it in chat. It never registers,
never decides, never runs the report, and is never `[SILENT]`: the brief is owed every week, a
quiet one in three lines. If the review's summary is missing or older than two days (the review
did not run, the hour cut it short, or its copy did not land), the brief says so in its headline and
composes **Decide now** from the queue alone. The entry declares `risk: high`: a prompt job over
tenant-written names, so its terminal is held to read-only inspection commands. For the same
reason the turn holds no queue tool and no file-writing tool: the entry's `enabled_toolsets` gives
it the terminal tool alone and no MCP server (`no_mcp`), so nothing a name says can make it
register or decide a finding through `platform_control`, or write a file the command policy would
refuse. The queue reads are made before the turn by the entry's
pre-run `script`, `upgrade_readiness_brief_queue.py`, whose JSON the scheduler puts above the
prompt. An open backlog it cannot read is said there and in the brief's headline; a page of
decided rows it cannot read leaves out only the brief's **Decided earlier** line. The `deliver: "chat"` relay posts it as written and stores it against the
thread, so a reply with an id and `dismiss`, `snooze until <date>` or `accept` reaches the agent
with the brief attached.

Why the queue and not a weekly chat line per finding: a line is read once; the queue keeps asking
until a person answers, keeps the answer, and stops. That is the contract the review needs for a
finding a user may have missed, and the one that lets a user say "ignore this" once.

## `sa-role-watch` says when the agent's account holds more than `read-only`

The install grants the agent's Google service account the `read-only`
permission set, and the installer warns about an admin role in a `custom` set,
but only when the set is chosen. A role granted afterwards, by hand or by
another tool, is what the Google-hosted GKE MCP server acts with when the agent
container calls it on that account's credentials, and nothing in the product
said so. `sa_role_watch.py` is that check.

Each tick reads, through the shell sandbox and so through the credential
broker, the `PlatformAgent` in the agent's namespace and the ServiceAccount it
runs the agent and broker pods as. The ServiceAccount's
`iam.gke.io/gcp-service-account` annotation is the account the agent
container's own credentials resolve to; the CR's
`spec.security.workloadIdentityFederation`, when both its fields are set, is the
one the broker impersonates instead, and both are checked. With federation
armed, an absent annotation is said once, since the agent container then has no
Google credential of its own; with neither, there is no account to check and the
job reports a check failure, once. A role bound to the ServiceAccount's own
Workload Identity principal is not checked (#3450). Then `gcloud projects get-iam-policy` runs for the management project and
each project in the resolved `spec.scope` (`fleet_scope_targets`), four at a
time, and every role bound to one of those accounts that is not in the script's
`READ_ONLY_ROLES` is a finding. `tests/test_agent_iam_ceiling.py` holds that
list equal to `read_only_roles` in the full-install composition.

It keeps a ledger at `profiles/platform/cron/sa_role_watch.json`, the way
`stall-watch` does, so chat hears a role on the tick it first appears and again
when it goes. A role still held is said again once a day, on the first tick from
12:00 UTC (`REMIND_HOUR_UTC`), for the reason `kanban-board-health` repeats: a
grant said once in a busy channel can be missed, and nothing else would raise it
again. Any other tick on an unchanged account prints nothing. That is why it can run
hourly: a fresh install or a new grant is named within the hour, and a quiet
hour costs one `kubectl` pair and one policy read per project, no model turn. A
project whose policy cannot be read keeps its last answer and its failure is
said once. So does a project missing from a scope the tick could read only in part (the
render unreadable, or members the reconcile has not resolved), and the partial
scope is said once. Losing the shell sandbox mid-tick is one failure line, not
one per project. It reads project-level bindings only: a role granted on a folder,
the organisation or a group is not in that answer, and reading those needs
verbs the broker's allowlist does not carry. A `custom` set chosen on purpose
is reported like any other grant, and repeated daily like one.

## `kanban-workspace-gc` is neither a watchdog nor a poller

The third shape, and the reason it is here rather than anywhere else: it is
housekeeping that needs the board DB, and the board DB is on the agent pod.
`kanban_workspace_gc.py` removes the scratch workspaces Hermes leaves behind —
`kanban_db._cleanup_workspace` runs from `complete_task` and nowhere else, so a
card that reaches a terminal state any other way keeps its directory forever, and
under `terminal.backend: ssh` the sandbox's copy is never removed even on the
path that works. `docs/designs/agent-shell-sandboxing.md` has the account.

It reports nothing on a clean run and only on a run it could not finish, which
is the same contract `github-repo-watcher` keeps. What it removed goes to
`logs/kanban_workspace_gc.log` in the agent home, which fluent-bit ships to
Cloud Logging, and not to chat: a job announcing its own housekeeping every
night is a job the room learns to skip, and the message that must not be skipped
is the failure. (Not stderr, which the scheduler keeps nowhere for a zero-exit
script; see `feedback-prompt` below.)

The same log gets an inventory of the sandbox's profile homes, so that loose
files a worker leaves there can be seen without exec'ing into the sandbox: each
home's top-level entries that the product doesn't create, and any home the agent
pod no longer has a profile for. The inventory itself moves and deletes nothing;
what the job removes is below. A
`cluster-*` home the agent pod has no profile for is removed: a cluster dropped
from scope or deleted loses its home on the agent pod, and the sandbox's copy,
kubeconfig included, had nothing else to remove it. The agent pod's profile
list is the authority, read after the sandbox is listed, and the job removes
no stale home when that list lacks the platform profile. A stale home whose
`rm` fails reaches chat, as the two guards do when they hold: one bullet
naming it, each night until it goes. A failure to take the inventory is logged there too
rather than posted.

Inside a home the agent pod does have a profile for, two things are removed,
on the agent pod and in the sandbox. Cron run directories
(`cron/runs/<job>_<execution>`, each cron run's working directory) go after
seven days, logged as a count. Override trees go the first night, logged by
name: a top-level directory named `<something>_override`, such as a worker's
patched copy of a shipped skill in `skills_override/`. No product code writes
one, and a test keeps literal paths in shipped import-path calls and configs
off them. Neither prune
follows a symlink.

Daily is deliberate rather than conservative. The first install to run this had
accumulated 34 directories and 3.9 MB on the agent pod and 9 directories and
68 KB in the sandbox; the first sweep removed 21 and 9 of them, leaving 164 KB
and nothing. A tighter interval buys nothing against that rate and spends an SSH
round trip per tick.

## `kanban-board-health` asks whether the board is wedged

The same shape as the collector above: housekeeping that needs the board, which
is on the agent pod at `<agent home>/kanban.db`. `kanban_board_health.py` reads
that file read-only, asks `hermes kanban diagnostics` for the shipped rule
engine's findings, and prints only when something is wrong. One finding is
reported whatever the severity floor says: a card `blocked` for more than a day
with no comment or unblock since. A worker's or operator's block is sticky by
design, nothing retries it, and #656 was a finished report parked that way for
weeks because nothing periodically asked. Cards are listed newest first, with
their kind, reason, and age read from the board; where multiple cards share a
title, they are grouped to keep the report readable, with task IDs and reasons
retained. The `hermes kanban unblock` and `archive` commands appear once in a
footer (using `<task_id>` when multiple cards are stuck) rather than repeated
under every card, so the room knows how to move a card while long reports stay
bounded within chat limits, summarizing any omitted cards by task ID up to
the character budget (and falling back to a count and list command when omitted IDs
exceed the limit).

Daily, and it repeats: the rule is stateless, so a card still blocked is named
(directly, or by task ID in the omitted list up to the budget)
every morning until someone unblocks, comments on, or archives it. That is the
intended nag, the same rule `findings-morning-nudge` keeps for the top
criticals it named, and the reason a finer cadence buys nothing: the engine's threshold is a
day. It fires at 12:35 UTC, half an hour after that nudge's daily reminder, for
the same reason the reminder waits for 12:00: an earlier UTC hour is the middle
of the night in the US, not a morning.

It sits here rather than on the Chat Agent's roster because that roster delivers
`local`. `PLATFORM_AGENT_HOME`, not `HERMES_HOME`, is how it finds the board:
under this roster `HERMES_HOME` is `profiles/platform`, which holds no board.

## `feedback-prompt` asks once, a week after it first runs

The one entry here whose product is a question to the operator rather than a
report on the fleet. Every feedback channel kube-agents has is
reporter-initiated: the tracker, the public form behind the short link, and the
agent handing out both when asked. `feedback_prompt.py` is the one place the
product asks, and it does so once per install: a fixed message in the home
chat channel carrying the form's short link, the form's disclosure that a
submission becomes a public issue, and a line saying a reply in the thread
reaches the agent. `deliver: "chat"` is what makes that last line true: the
relay stores the message against its thread, so a reply there reaches the Chat
Agent with it attached.

It is a daily cron entry that fires once, not a Hermes one-shot, and the
reason is this roster's merge. A shipped entry cannot carry an absolute
`run_at`, and a completed one-shot is pruned from the store after seven days,
at which point `merge_cron_store` sees an id the volume lacks and re-adds it,
re-arming the prompt. So the once-only state lives outside the store, in two
marker files in the profile home (`HERMES_HOME` under this roster is
`profiles/platform`): `.feedback_prompt_armed`, created with `O_EXCL` on the
first tick and holding the anchor time, and `.feedback_prompt_sent`, claimed
with `O_EXCL` before anything reaches stdout, the same claim
`bootstrap_delivery.py` makes and for the same reason, holding the time of the
claim. Both sit on the data volume, which survives
restarts and image rolls and dies only with an uninstall, so an upgrade is not
a new install.

Printing is not delivering: the relay runs after the script, and a claim taken
on a day the relay, the Session KV server or the chat platform was down, or on
an install that had bound no chat platform yet, spends the one message on
nothing. The script does not read the scheduler's record of that delivery
(`last_delivery_error` on its entry in this profile's `cron/jobs.json`, the
field `chat_delivery_watch.py` grades) to post again, because the record
cannot tell a post that landed from one that did not: a `hermes send` that
posts, exits 0 and prints no readable message id is recorded as `composed but
not delivered`, the same words as a send that failed, and so is a relay that
raises or times out after the post has gone out. A retry on that record posts
the request twice, and a message in a channel cannot be taken back. So the
job fails in the direction of at most once: a post the scheduler recorded as
undelivered is a lost request, which `chat-delivery-watch` counts like any
other failed delivery, and which an operator sends again by removing
`.feedback_prompt_sent` from the profile home, after which the next tick
claims afresh. One post is all the job ever costs, on the one tick that
prints. The clock starts at the first
tick, not at any record of when the install finished: nothing in the operator
status carries a ready-since time, and the Chat Agent's onboarding markers
live in a different home. An install that predates the entry therefore gets
the message a week after the upgrade that brings it.

Every other tick prints nothing and relays nothing, so once the message has
landed the job costs one silent subprocess a day, like `github-repo-watcher`'s idle
ticks. Two environment variables, set per install through the chart value
`platformAgent.deployment.env` (on an `install.sh` install, a
`TF_VAR_extra_helm_values` line in `install.env`; on a hand-driven Terraform
apply, `extra_helm_values` in `terraform.tfvars`) and passed to the agent container by the
operator's allowlist, are the whole configuration surface: `FEEDBACK_PROMPT_ENABLED`
(default `true`; `false` neither arms nor claims, so an install that turns it
on later still gets exactly one) and `FEEDBACK_PROMPT_DELAY` (default `7d`;
`<n>d`, `<n>h` or `<n>m`). A hand edit of the CR's `spec.deployment.env` can
be lost at the next chart apply. A delay that does not parse is a failed run, exit 1
with the reason on stderr. The scheduler turns that into its failure line, which
the relay posts once in plain words ("The Feedback Prompt didn't run. It hit an
error."), and the run's output, which the scheduler saves under the profile's
`cron/output/`, keeps the reason; it does not fall back, because the
scheduler keeps a zero-exit script's stderr nowhere and a silent fallback
would leave no trace but the message arriving a week early. The schedule is
daily, so whatever the delay, the message lands on the first 13:00 UTC tick
at or after it; a delay of a day or more is compared with ten minutes of
slack, because the tick's own time drifts by seconds from one day to the next
and a strict week would otherwise land on day eight. The form URL is a
constant, never a knob: the short link is the only address the maintainers
publish. `enabled: false` on the entry stays the fleet-wide switch, and
retiring it follows the two-step path below like any other id; deleting the
entry outright would leave the volume's copy firing against a script the next
image no longer ships.

## `upgrade-retrospective` looks back after the weekend's upgrades

The other audits say what an upgrade _would_ break. Nothing on the roster said
what one _did_ break, so the same surprise waited for the next cluster. This
entry is the fleet-audit stream that fills that gap: at 18:00 UTC on Sunday,
after the maintenance windows most fleets set at night or at the weekend and
before Monday's audits, the Platform Agent follows
`governance/upgrade_retrospective_sop.md`. The SOP resolves the fleet with the
platform_control `fleet_scope` tool first, as every fleet-audit stream does,
probes the collector's lock (`--probe-lock`) before it opens the audit run, and
runs the deterministic collector,
`skills/fleet-audit/scripts/upgrade_retrospective.py`, with `--full`, the
tool's `collector_args` verbatim (`--scope-projects`, `--scope-unread`; nothing
on an install that declares no scope) and `--manifest-file`, so `finish` holds
the findings document to what the collector read, in the foreground under the
terminal tool's `timeout: 600`. The collector keeps a ledger of versions per cluster,
reviews every cluster that is new or was upgraded since the last run, reads
pods, nodes, events and budgets, classifies what it finds against the upgrade
failure catalogue, and writes the three-section report (Errors, Warnings, Info)
to `/opt/data/upgrade-retrospective/` on the shell sandbox's volume, with
`upgrade-retro-report.md` pointing at the newest full run. The SOP's half is
the judgement: it confirms every candidate against the seven checks in its
section 3 and copies the collector's grade (the gate per reviewed cluster included: `clear`, `risk` or `pending` from the pod and node diff plus Cloud Logging ingestion bytes by container, seven days after against seven days before), tailors the catalogue's mitigation
text to the cluster's own objects, files the stream's one ledger issue where a repository is configured, and reaches `finish`, whose record is what chat receives (the relay posts what `finish` rendered, not the run's final message): the headline and the most severe findings, each with its cluster and object; the counts per cluster and the Errors and Warnings entries are the report's. A quiet weekend, with no cluster new or upgraded, posts nothing.

Two things set it apart from the streams beside it. With no GitHub repository in `managed_repos` it takes the skill's no-forge path as they do (`start` and `finish` with no ledger issue, the findings kept in the report store), and keeps its own store on the volume beside it: the ledger, the guards and the dated reports the on-demand route reads. The Chat Agent's
first-run stage (`agents/chat/scripts/oobe.py`) marks this job due after the four
fleet audits it starts once the first inventory scan settles, so a fresh
install's first report is a baseline of every cluster it manages. And it never
opens a pull request on its own: an Error files at
`major`, a Warning at `minor`, nothing at `critical`, every remediation is
`manual` naming the manifest and the change, and none of its check slugs is on
`MAJOR_SWEEP_CHECKS`, so `finish` has nothing to promote. A fix stays a user's
"apply" in chat, through `submit-suggestion`.

The same report answers a question. `agents/platform/CAPABILITIES.md` and the
persona's delegation list route "how did the upgrades go", "did anything break",
"what broke last upgrade" to the SOP's on-demand section (a forward-looking question,
"anything to worry about before we upgrade", is the readiness report's), which answers
from the saved report when it is newer than the last upgrade operation on the
clusters the question covers and runs the collector first when it is not: a
fleet-wide question as a full run, a question about named clusters as a scoped
one (`--cluster`, beside the tool's `collector_args`; the collector refuses a
cluster outside the declared scope), which writes a `-scoped` report, moves no
link and touches no ledger. A second run while one holds the store's lock prints
one line and writes nothing; a question that meets it is answered from the saved
report, naming the run that holds the lock. A collector exit without a manifest
after `start` (the lock lost, a crash record) takes the SOP's recovery `finish`,
which releases the in-flight note at once instead of leaving it to its TTL.

`risk: low`, as every audit: the run's only writes are the collector's files on
the volume, the ledger issue through `audit_report.py`, and the chat message,
and the `high` tier's command allowlist would refuse the collector itself, a
Python script. `deliver: chat`, so the Chat Agent owns the thread a reply lands
in. The collector also keeps a guard per classified failure and per risk shape
in `guards.json` beside its ledger, for the readiness watch to read once its
reader ships ([the readiness design](../../../docs/designs/upgrade-readiness-checks.md));
nothing scheduled reads the file today, and the SOP's on-demand section reads its
risk entries for a cluster the latest report lists as unchanged.

## Never put an id on both rosters

Do not add any id here to `agents/chat/defaults/cron/jobs.json` as well. Two
rosters both carrying one id is that audit running twice per schedule,
concurrently with itself, writing its ledger issue twice. The per-job lock
(`cron/.job-<id>.lock`) is per profile directory, so it does not stop this.

## `deliver` is `"local"` on exactly one job

Every enabled job here sets `deliver` to `"chat"` or `"all"`, the two audible
values (`"chat"` alone for a fleet-audit job, see below), with one exception below. `cron/scheduler_delivery.py::_resolve_delivery_targets`
returns an **empty target list** for `"local"` — the outcome is written to
`last_output` and delivered nowhere. A watchdog whose run failed would then be
indistinguishable from a quiet fleet. Both audible values carry a failure: the
scheduler builds one with `_summarize_cron_failure_for_delivery` and delivers it
on the same leg, and the relay posts it in plain words, once until the job runs
again, for any job.

Silence is still cheap: a run with no findings returns `[SILENT]` and the
scheduler skips delivery, so a steadily clean fleet generates no chat traffic.

The exception is `chat-delivery-watch`, whose job is to notice that the chat leg
itself is down. Its product is a GitHub ledger issue and an `ALERT` line in
`logs/chat_delivery_watch.log` that fluent-bit ships to Cloud Logging, neither of
which passes through chat, and a chat delivery for it would be circular. The
design is in
[`docs/designs/cron-report-relay.md`](../../../docs/designs/cron-report-relay.md)
under "Detecting a broken leg".

`test_every_watchdog_declares_all_delivery` in
`../skills/fleet-audit/scripts/test_audit_report.py` enforces this, and carries
the exemption by name, pinned to a `no_agent` entry whose script exists.

A misspelled `deliver` part next to one that resolves is dropped by the scheduler
with the run still recording `ok`. `check_cron_delivery` in
`scripts/check_prompt_assets.py` (`make prompt-check`) refuses a value outside the
`CRON_DELIVER_VALUES` set in that file, bare or as a `platform:chat_id` prefix, so
the typo fails the pull request instead.

## `deliver: "chat"` — reports the Chat Agent can answer for

`"all"` gets the words into a channel. It does not make them answerable: the
process that produced them has exited, and the Chat Agent — which is who the
user replies to — never saw the finding. `"all"` now expands to include the
relay as well as the channel, so a job left on it is heard twice rather than
not at all. The relay skips a platform the scheduler already posted to only
where that leaves it another platform to post to; on an install where the cron
child can address every enabled channel itself, both copies land
([design](../../../docs/designs/cron-report-relay.md)).

`deliver: "chat"` hands the run's report to the relay instead, which posts it as
written and stores the full report against its thread, so the user's reply there reaches the
Chat Agent with the report attached. A fleet-audit job the image ships is the
exception: its scheduled delivery posts the report the run's `finish` rendered,
not the final message,
which holds whatever else the model wrote in its last turn. A run publishes to
one repository, the one with `role: gitops`; a run that a person ran for several
repositories posts each finished repository's report, then a line naming any it
did not finish. A failed run posts the scheduler's failure line in plain words,
once until the job runs again ([the relay design](../../../docs/designs/cron-report-relay.md)),
and a run that finished no repository without failing posts a one-line notice
that it did not finish. On Slack the report is then replaced by a headline linking its
ledger issue when the run covered one repository; a message with that line, or
with several repositories' reports, is posted whole. It is a delivery mode, not a
prompt contract: **the job's prompt says nothing about it**, because the
scheduler applies `[SILENT]` and builds the failure summary before delivery is
reached.

The relay itself posts to every chat platform the install has enabled, so on a
dual-platform install whose cron child can address both, a job left on `"all"`
is heard twice on _each_ of them rather than twice in one place. Under `"all"`
the scheduler also posts the final message itself, past the relay's choice of
report, so every entry here names `"chat"`.

The mode is a bundled platform plugin, not a patch: `chat` is a delivery-only
platform ([`deploy/docker/plugins/chat/`](../../../deploy/docker/plugins/chat/))
that Hermes registers like any other. So it is one target among several — a job
on `"chat,slack"` relays _and_ posts, and an unreachable relay records
`last_delivery_error` rather than falling back. Note that `"all"` now expands to
include the relay, so `"all"` and `"chat"` together deliver once, not twice, but
`"all"` alone relays too. The full rationale — why the report is posted as the
job wrote it, why the thread is per job per day, why a mode rather than an
instruction, and what the plugin route costs — is
[`docs/designs/cron-report-relay.md`](../../../docs/designs/cron-report-relay.md).

## Moving the roster onto `"chat"` needed no migration

Every report-producing job here names `"chat"`, and getting there was an edit to this file alone
— no script, no one-off Job, nothing run against a live volume. `deliver` is an
image-owned key on this profile: `merge_cron_store` gives the image every key it
ships and leaves the volume only the keys it does not, so the next pod start
rewrites `"all"` to `"chat"` on stores this repo can no longer reach by any other
means. `test_the_image_decides_where_a_report_is_delivered` in
`../scripts/test_profile_scaffold.py` pins that.

It does not generalise. The Chat Agent's roster is reconciled by
`cron_jobs_sync.py`, which lists `deliver` in `RUNTIME_WINS` because onboarding
rewrites it to `origin` on the delivery job — there the volume's value stands and
an image edit is ignored. And a job the agent creates at runtime is not in the
image at all, so nothing rewrites it; that one is answered in `../AGENTS.md`, by
telling the agent to pass `deliver='chat'` in the first place.

## `schedule.display` mirrors `schedule.expr`

For `kind: "cron"`, `cron/jobs.py` sets `display` to the raw expression
(`"display": schedule`); the `every {minutes}m` form is what it generates for
`kind: "interval"`. Nothing validates `display` against `expr`, and
`scripts/generate_docs.py` reads `expr` and its own `CRON_CADENCE` table, falling
back to `display` only for interval jobs — which neither roster has. So `display`
is a second copy of `expr` that can rot silently. Keep the two identical.

## Retiring a watchdog

`profile_scaffold.merge_cron_store` adds and overwrites but never prunes.
Deleting an entry only ends this image's ability to hold the job off: the
volume's copy goes on firing. The sequence is therefore:

1. Ship the entry with `enabled: false`. That is what actually stops it.
2. Delete the id only once no live volume can still be carrying an enabled copy
   — and name it in `--cron-retire` in the same release, or the volume keeps a
   disabled entry no later image can reach.

Step 2 is not optional bookkeeping. A deleted entry the volume still holds is
invisible to every future image: the merge is silent about it, so nothing can
re-enable it, disable it, or remove it, and `cronjob(action='list')` reports it
forever. That is why this roster has no tombstones left — the five retired
watchdogs (`blueprint-sync`, `policy-propagation`,
`global-capacity-orchestrator`, `standardization-validator`,
`lifecycle-deprecation-manager`) were deleted here _and_ named in
`--cron-retire` on the platform force-sync.

`retire_cron_jobs` (`--cron-retire` in `deploy/shared/docker-entrypoint.sh`) is
also the escape hatch for the case step 1 cannot cover — an id that has to stop
firing in one release, as when the seven governance jobs moved back here from
the Chat Agent's roster. It deletes the named ids outright, and the entrypoint
names them explicitly.

`github-issue-resolver` took that route too: its replacement polls the same
repository through the same `resolver.py poll`, so leaving it enabled for a
release would keep paying the 48 daily model turns the replacement exists to
stop.

The five retired watchdogs' SOPs are gone from `../governance/` as well — git
history has them. An SOP is inert without a job to run it, and as written none
of these could produce a finding on a stock install, so reviving one is a new
SOP and a roster entry, not a restore.

## Adding a watchdog: the repository steps

The site's [Autonomous watchdogs](../../../docs/site/src/content/docs/concepts/autonomous-watchdogs.md#adding-a-watchdog)
page lists what an entry needs. Two steps belong to this repository rather than
to an install:

- Run `make docs-generate` after editing either roster. The site's cron
  reference table is generated from both, and a cron expression missing from
  `CRON_CADENCE` in `scripts/generate_docs.py` renders its cadence as `—`. The
  `compliance-audit` entry is also rendered in full as the job-schema example
  on the watchdogs, skills and cron-jobs pages, so an edit to it changes those
  three pages too.
- For a dev workspace, `scripts/dev/dev_rebuild_agent.sh` rebuilds and restarts
  the agent image without a release; `./upgrade.sh --upgrade-mode=harness
--image-tag=<ref>` is the path for an installed cluster.

## How the Planning Agent's roster reaches the volume

The Planning Agent is the `default` profile, which is not scaffolded: it lives
at `$HERMES_HOME` directly and the entrypoint seeds it with
`cp -ru /opt/defaults/. "$TARGET_DIR/"` (`deploy/shared/docker-entrypoint.sh`,
step 2). `cron/` is in neither force-sync list — step 2a covers `SOUL.md`,
`AGENTS.md`, `CAPABILITIES.md` and `hindsight/config.json`, step 2b covers
`scripts/` — and since the
scheduler writes `last_run` into the volume's copy on every tick, that copy's
timestamp is permanently ahead of the image's, and `cp -u` skips it for good.

Step 2c-bis closes that gap: `cron_jobs_sync.py` reconciles
`$HERMES_HOME/cron/jobs.json` against the shipped roster by job id, per key,
under the rule `merge_cron_store` applies on this roster — the image wins every
key it ships, `enabled` among them, and a key it ships nothing for stays as the
volume had it. Two rosters obeying opposite merge rules would be a trap for
whoever edits either. The cron-retirement step 2c (the entrypoint labels two
steps `2c`; this is the one immediately before 2c-bis) forces exactly one id
(`--cron-jobs "profile-cron-tick"`); that narrowness is a deliberate subset of
the same rule rather than a second policy for the same file, because 2c is the
call that also carries `--cron-retire` and an unfiltered merge there would
resurrect the two onboarding jobs `bootstrap_delivery.py` deletes once the
first-run report lands. What stops 2c-bis resurrecting them is a ledger instead:
`$HERMES_HOME/.cron_jobs_installed` records every id the script has installed,
so an id missing from the volume that the ledger already knows about was removed
on purpose, not shipped new, and is never reinstalled.

## Incidents behind the roster's shape

Three rules on the site page came from measured failures, recorded here so the
rule outlives the memory of why:

- **On-demand runs are marked due, not re-enacted in the requesting session; one delegated stream is the exception.**
  On 2026-08-03 a session asked to run several audits at once crammed them into
  one turn budget and produced five hand-typed empty findings documents and a
  fleet-wide all-clear, having issued no `kubectl` at all. That is why the
  Platform Agent marks the job due for the next tick instead of running the SOP
  itself. The one run that does happen in a session (#1876, #1887): a card that
  delegates exactly one stream per its SOP is run by that worker through
  `audit_report.py start … finish` (`skills/fleet-audit/references/on-demand.md`, "Running a
  stream on demand"); several streams still queue.
  Such a run holds no per-job lock, so `start` keeps its own guard: an
  in-flight note per stream on the sandbox pod's own volume, at
  `/opt/data/scratch/inflight_<audit>.json` in that pod (the script runs in
  the sandbox shell, whose `/opt/data` is its own PVC, not the gateway volume
  this README otherwise calls "the volume"), holding the stream id and a start
  time, honoured for two hours and removed by a `finish` that published or
  died (exit 0 or 1); `finish --dry-run` and a `finish` that exited 2 keep it,
  so a note beside a rejected document is a live run, not a stale one. The note spans one
  `start`-`finish` pair, so a multi-repository loop reclaims it at each
  repository. A run that died without `finish` costs the
  stream at most the ticks inside those two hours. For the operator only:
  when you know from outside the run that it is over (its card is closed, its
  pod is gone), deleting that note releases the stream at once. The `.lock`
  file beside the note is created once, mode 0644 whatever the creating
  shell's umask, and never removed; a `START REFUSED` that says the guard
  could not be taken names the error, and a lock that uid 1000 cannot open
  is the first thing to check. The CLI has no
  override flag on purpose; the one it had was taken by refused workers over
  their own live runs (#1876), and a worker's shell is the same shell you
  would use, so the release lives here and not where a worker reads.
- **Overlap is held per job, not per profile.** Holding the profile lock across
  execution — the upstream default — meant a fleet audit blocked every dispatch
  for its whole run; three `github-issue-resolver` firings were measured 418s,
  179s and 1142s late behind one, each recovering within seconds of the audit
  finishing. The per-job `cron/.job-<id>.lock` is the fix.
- **Pollers are `no_agent` scripts.** As a prompt job, `github-issue-resolver`
  ran a third as often as its replacement and still spent 48 model turns a day
  to be told "nothing to do" 47 times.

## Hard-coded line numbers in prompts

Each governance prompt cites its SOP's total length and the line range of its
checks section. Those numbers are load-bearing — they are what stops a model
reading the first screen and reporting a clean fleet it never looked at — and
they rot the moment an SOP is edited. They are generated, not measured by hand:
`make docs-generate` runs `scripts/generate_sop_geography.py`, which reads each
prompt's SOP, recomputes both numbers and rewrites only those digits in
`jobs.json`; the section number and the spelled-out check count stay authored.
`test_cron_prompts_cite_the_real_sop_geography` in
`../skills/fleet-audit/scripts/test_audit_report.py` re-derives both from the
SOP itself, and `make docs-check` runs the generator's `--check`, so an edit
that skips regenerating fails there rather than at 06:20 in production. Run
`make docs-generate` after touching anything in `../governance/`.

No prompt is quoted here on purpose. A copy in prose is one more place for the
same numbers to go stale, and the test above checks the roster against the SOPs
— not this file against the roster.

## The `--scheduled` sentence in audit prompts

Every fleet-audit prompt tells the worker to pass `--scheduled` to `finish`.
Only a run with that flag can get `silent_ok: true`, so the sentence is what
lets a quiet scheduled run answer `[SILENT]`. It is in the prompt and not in
the SOP, because a scheduled run and an on-demand run read the same SOP: an SOP
that passed the flag would let every on-demand run of its stream go silent.
`test_every_audit_cron_prompt_passes_scheduled_to_finish` in
`../skills/fleet-audit/scripts/test_audit_report.py` checks that each audit
prompt has the phrase `pass --scheduled to finish` and that no SOP passes the
flag. A prompt without it
only makes its job noisier. The rule is in
[ledger design §7.5](../../../docs/designs/fleet-audit-issue-ledger.md#75-silent_ok-and-who-is-listening).

## `risk` tier contract

Every job entry across both rosters declares an explicit `"risk": "low" | "high"`.
The field is validated by `scripts/check_prompt_assets.py` (`check_cron_risk`) and enforced
across pod restarts by `profile_scaffold.py::merge_cron_store` (an image-owned key).
Runtime-created jobs (`cron.jobs::create_job` and `tools.cronjob_tools::cronjob`) stamp
`"risk": "low"` at creation time unless an explicit `risk` is provided. Existing unannotated
legacy jobs are backfilled to `"risk": "low"` across all profiles:
Platform Agent roster during scaffold merge (`profile_scaffold.py::merge_cron_store`),
Chat Agent roster during reconciliation (`cron_jobs_sync.py`), and cluster profiles both at
profile creation (`cluster_agent_profile.py`) and pod startup (`docker-entrypoint.sh` via
`profile_scaffold.py --backfill-cron`). Unannotated in-flight executions or dispatches with no
tier default fail-closed to `"high"` under `cron_run_scope.py` and `cron_risk_gate.py`.

- `"low"`: Read-only governance watchdogs, audits, and internal scheduler plumbing. Runs under
  the configured `cron_mode` (typically `approve`), protected by the denylist floor (hardline +
  `approvals.deny` + Tirith POSIX shell content scan), terminal escape rejection, lookalike TLD
  blocks, and `execute_code` blocks.
- `"high"`: Workloads with broad operational authority or untrusted input sources (such as
  `github-repo-watcher`), as well as unannotated dispatches. For agentic (prompt-driven) jobs,
  this applies a fail-closed read-only command policy (`cron_command_policy_block`): every command
  segment must be an allowlisted inspection command (`kubectl get/describe/logs/top`, `gcloud … list/describe`,
  read-only text utilities, `--dry-run` validations); mutating, unknown, or unanalyzable commands
  are refused while the run continues. For `no_agent: true` jobs like `github-repo-watcher`, there is
  no agent loop and therefore no tool-approval surface to gate; `"high"` is a threat classification of
  the untrusted input the subprocess ingests (runtime isolation is tracked in #913; today the tier is
  metadata only for those jobs).

## `max_turns` contract

Every model-run entry (one without `no_agent`) declares `"max_turns"`, a positive integer, and
no `no_agent` entry carries one; `scripts/check_prompt_assets.py` (`check_cron_max_turns`)
enforces both. The image's scheduler reads it ahead of the profile's `agent.max_turns`
(`deploy/docker/patches/apply_cron_job_max_turns.py`, the same change upstream Hermes has
open, so the patch drops out on the tag bump that carries it). The audits carry the budget
their by-hand per-cluster fallback needs; the profile's own value bounds the kanban workers,
which never need that much. Required rather than optional because an absent key silently
runs the job under the worker budget. Hermes reads 0, a negative number, a bool or a
non-numeric string as unlimited; the patch ignores such a roster value and the profile's stands,
so an entry the image does not ship cannot lift a cron run past every budget. `merge_cron_store` carries it to existing installs like any
image-owned key. Raise an entry's number when its SOP outgrows it; do not raise the profile's.
