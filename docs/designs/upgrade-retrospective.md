# Upgrade retrospective: what the last upgrade did, and the fix set up before the next one

**Status:** design. Nothing here is built on `main`. The collector it specifies is the first of
three changes; the schedule and the agent's part follow it. The
[upgrade failure catalogue](upgrade-failure-catalogue.md) is the list of failures it classifies
against, and the [readiness checks](upgrade-readiness-checks.md) are the before-the-upgrade half it
feeds.

## Requirements

A GKE cluster has a control plane and one or more node pools. GKE upgrades the control plane and
then recreates the nodes of each pool, one node at a time, on the cluster's maintenance schedule.
Most upgrades complete without damage to the workloads. Some upgrades leave a workload down, stuck
or restarting. GKE reports the upgrade as complete in both cases, so the damage is found later,
often by a user. Today the Platform Agent reports what would stop an upgrade before it starts, when
a user asks. No component reviews a cluster after an upgrade and records what the upgrade damaged.
The next cluster meets the same failure.

This design adds a scheduled review, the **upgrade retrospective**. The requirements are:

1. **Run without a request, and answer a request.** The review runs once when the agent is
   installed, over every cluster it finds, then at the end of each weekend, and within half an hour
   of an upgrade for the cluster the upgrade changed. A user can also ask
   for it in their own words, for example "how did the last upgrades go" or "is there anything to
   fix before the next one". The question does not have to name the feature, a cluster or a
   version. If no cluster was upgraded since the last report, the agent answers from that report.
   If a cluster was upgraded, the agent runs the review first.
2. **Review only what changed.** The review examines a cluster when the cluster is new to the agent,
   or when GKE upgraded it since the last review. For a cluster with no change, the report has one
   line that says so. Exception: a cluster that still has a finding from an earlier review is
   checked again for that finding each time. This includes a finding that matches none of the
   twenty failures. A fix made between two upgrades then clears the finding before the next
   upgrade. A finding whose only evidence was events is checked again the same way. The next
   weekend review reads the events and the pods of the object again. It clears the finding when it
   finds no live symptom on the object. A read that fails keeps the finding, and the report names
   the failed read.
3. **Write the `upgrade-retro-report`** with three sections, **Errors**, **Warnings** and **Info**.
   A section can be empty. An entry under Errors or Warnings is one incident: one workload object
   on one cluster, with four parts:
   - **(A) What happened.** The upgrades that ran on the cluster: the component (control plane or
     node pool), the source and target versions, the start and end times, the duration, and the
     result GKE reported (complete or failed).
   - **(B) What failed.** The state of the object after the upgrade (down, stuck or restarting),
     the catalogue failure it matches out of the twenty known failures, the confidence of the
     match, and the evidence.
   - **(C) The warning sign and the fix.** The sign that was visible before the upgrade, whether a
     check the agent already runs reads that sign, and the fix: the change to make before the next
     upgrade, and the change that repairs the cluster now. The text is specific to this object.
   - **(D) The fix, set up.** Two actions happen without a request. The daily readiness check
     learns the failure, so the next pre-upgrade report says that the last upgrade damaged this
     object, for as long as the damage is present. If the install has a GitHub repository, the
     review's one tracked issue carries the checklist of fixes, one entry per cluster and object.
     The next fleet-wide review rewrites the checklist. A failure that is still present keeps its entry. The
     entry says when the review first saw the failure and how many reviews have seen it. The
     review removes the entry when the failure is gone. The review closes the issue when it finds
     nothing.

   An incident is an **Error** when the match is certain and the object belongs to the user, when
   GKE reported the upgrade as failed, or when a node did not become ready after its recreation. An
   incident is a **Warning** when the match is tentative, when the object belongs to the cluster's
   system components, when the symptom matches none of the twenty failures, or when a failure from
   an earlier review is still present with no new evidence. An earlier review here is an earlier
   review of the whole fleet, at install or at a weekend. The after-upgrade review reports a failure
   at once, at its grade, and does not write the checklist. The next weekend review grades the
   failure again from its own reads and writes it to the checklist at that grade. The first
   checklist entry for a failure carries its own grade, whatever review found the failure first.
   **Info** lists each cluster that was upgraded or found with no failure, each cluster with no
   change, and each item the review could not read.

   A cluster with no failure is not an empty entry. For such a cluster, part (C) is a pre-flight for
   its next upgrade: the version its release channel moves it to and when, and the known failure
   shapes present on the cluster today, each with its fix. Examples of shapes: a PodDisruptionBudget
   that permits no disruption, a node selector on a label the next version removes, an image from a
   retired registry, a volume attached through a removed in-tree plugin, a DaemonSet that depends on
   the node's network or container runtime, data on local node storage, a single replica behind a
   PodDisruptionBudget, a GPU workload pinned to a driver version, a fail-closed webhook with no
   backend. Part (D) records the baseline for the next review and marks each shape as a risk for
   the readiness check to name before the next upgrade. The line for an unchanged cluster gives the
   date of its last upgrade and its next target version. The line for a cluster the review could
   not read gives the reason. The report lists a cluster in the middle of an upgrade as upgrading
   now. The review reads it after the upgrade ends. Until then, the tracked issue stays open and
   names the cluster as not read. The checklist does not show that cluster's entries while it is
   not read. The review keeps each entry's first-seen date and review count in its own record. The
   entries return with those values when a review reads the cluster and finds the failures. A
   failure that is gone by then leaves no entry. "None" appears only when the count is zero.

4. **Store the report where the agent can read it,** on the storage its tools use, with the latest
   report at a fixed path. Post one line per reviewed cluster in chat, with the counts and the most
   important finding. A weekend with no change posts nothing. A GitHub repository is not required;
   it adds only the tracked issue.
5. **Be testable on the failures the test fleet already contains.** The test fleet has planted
   examples of the catalogue's failures. The first report over that fleet must classify those
   examples correctly, and each example becomes a nightly test.

The review does not upgrade, roll back or change a cluster. A pull request that fixes a manifest
stays a user's "apply", through the route the agent uses for every other proposed change.

## 1. Why

The [readiness checks](upgrade-readiness-checks.md) end with "a diff after the upgrade", and the
[catalogue](upgrade-failure-catalogue.md) says post-upgrade detection is one mechanism for every
entry: watch the operation, then compare pod health against the same measurements taken before.
Neither is built. The reproductions behind the catalogue measured why it matters: on a surge
upgrade GKE refused the eviction a budget protected for an hour, force-killed the pod, and reported
the operation `DONE` two minutes later while the replacement was still Pending. The symptoms of the
twenty failures sit in pod status, node conditions and `Warning` events that nothing scheduled
reads, and what one cluster's upgrade broke never reaches the next cluster's readiness report.

One thing on the roster and one design shape this one. The fleet audits pair a deterministic
collector (`collect.py`, `patch_readiness.py`) with a governance SOP the Platform Agent follows,
which is the split used here. The daily readiness watch, `upgrade-readiness-watch`, is specified
beside the [readiness checks](upgrade-readiness-checks.md) and is in review rather than on `main`:
it produces the before-the-upgrade report and keeps its own state on the gateway's profile volume;
this design's (D) lands its guards on the shell's volume (§3.6), which the watch reads through its
sandbox hop once it ships.

## 2. Target model

```
upgrade-retrospective (Platform Agent roster: Sunday 18:00 UTC; once, from the Chat Agent's
                       first-run stage after the inventory scan settles)
upgrade-retrospective-after-upgrade (daily 05:00 UTC as a sweep, and woken early by
                       upgrade_retrospective_watch.py within fifteen minutes of an upgrade;
                       a scoped run over the clusters whose operations are not yet reviewed)
  └─ collector  upgrade_retrospective.py                          deterministic, no model
       ├─ ledger: versions per cluster, last run                   /opt/data/upgrade-retrospective/ledger.json (the shell's volume)
       ├─ select: new (not in ledger) or upgraded (version differs, or an
       │          UPGRADE_MASTER / UPGRADE_NODES operation since the last run)
       ├─ (A) operations in the window, versions before and after
       ├─ (B) symptoms read from the cluster, classified against the catalogue
       ├─ (C) detect-and-mitigate rows from the catalogue table, per finding
       ├─ (D) guards.json merged: new, seen again, gone
       ├─ report.json + the Markdown report (A, B, C, D per cluster)
       └─ the fleet-audit collector manifest: the contract `finish` reads
  └─ Platform Agent, following governance/upgrade_retrospective_sop.md
       ├─ reads the collector's output, tailors (C) to the cluster's objects
       ├─ (D) the stream's one ledger issue, an entry per cluster and object, where a repository is linked
       └─ posts one line per cluster to chat; names the report's path
  └─ upgrade-readiness-watch (daily, once it ships)
       └─ its sandbox hop reads guards.json and prints each live guard in its next report
  └─ on demand: a question about what an upgrade did, or whether upgrades have problems,
       routed by the chat roster and the platform persona to this report; the SOP answers from
       the saved report when it is newer than the last upgrade operation, else runs the collector
```

## 3. Decisions

### 3.1 Trigger: first run, the end of each weekend, and soon after each upgrade

A retrospective is only useful soon after the upgrades it reviews. GKE's automatic upgrades land
inside maintenance windows that most fleets set at night or at weekends, so the run sits at the end
of the weekend, Sunday 18:00 UTC: late enough to see Saturday night's and Sunday's windows in most
time zones, still inside the weekend the requirement names, and before Monday's audits. Events
are a weak witness at that distance: the API server keeps them for one hour by default
(`kube-apiserver --event-ttl`, [Kubernetes reference](https://kubernetes.io/docs/reference/command-line-tools-reference/kube-apiserver/)),
so (B) leans on pod and node state and uses events as corroboration, not as the only source. The
first run comes from the Chat Agent's first-run stage, the same place the four fleet audits start
once the onboarding scan settles, so a fresh install's first report is a baseline of every cluster
it manages rather than a blank. Two things stand in the way today and the second work item
removes both. The first-run stage skips every audit when no GitOps repository is configured, and
the fleet-audit skill's `start` fails outright without one (`INSTALL.md` says so), because the
streams it serves exist to file a ledger. This stream does not: the collector needs no repository
and the report on the volume is the record. Where a repository is configured and the run is full,
the SOP calls `start`, then runs the collector, then `finish` with the collector's manifest (`finish`
refuses a manifest older than the run); without a repository it runs the collector alone, manifest
included, and the first-run stage marks this job due without one. That is the "chat-only mode" the first-run design lists as an open question,
scoped to this one stream. An install that onboarded before the job existed gets its baseline from
the first Sunday tick; the after-upgrade sweep, which on most such installs arrives first, finds no
full run in the ledger and prints only that (§3.2).

A third trigger reviews a cluster soon after its upgrade rather than at the weekend. It is a
second roster entry, `upgrade-retrospective-after-upgrade`, whose prompt is the SOP's
after-upgrade route: the collector run with `--after-upgrade`, which selects the clusters with an
`UPGRADE_MASTER` or `UPGRADE_NODES` operation inside the cluster's selection window (§3.2 defines
it once: from the cluster's last full review, or fourteen days back when it has none, to the run)
that reached `DONE` at least fifteen minutes before the run (the same floor the watch applies, so a
second cluster's fresher operation waits for the next wake rather than being reviewed before its
pods settle) and is not in the ledger's reviewed operations, as a scoped run (§3.2) that posts its
lines and files no ledger issue. Both bounds matter: the floor keeps a run off pods that have not
settled, and the window's start, which Sunday's full run advances, keeps Monday's sweep from
re-selecting the operations Sunday reviewed once they leave the reviewed list. The entry has a daily schedule as a sweep, 05:00 UTC, and is woken early by a
`no_agent` script, `upgrade_retrospective_watch.py`, which runs every fifteen minutes on the gateway
pod, reads the roster projects' operations through the sandbox hop the readiness watch uses
(`sandbox_exec` as its default principal, read only) and, when an operation reached `DONE` at least
fifteen minutes earlier that it has not seen, marks the after-upgrade job due with the same
`trigger_job` call the first-run stage uses, which carries a job id and nothing else, behind the
same guard the stage puts in front of it: `trigger_job` also sets a job's `enabled` back to true,
so the watch reads the Platform Agent's roster first, as the stage's `audit_holds` does, and when
the entry is absent, paused or disabled it marks nothing and logs the hold with its reason. An
operator's pause therefore stands until the operator lifts it, and the operations that complete
meanwhile wait for the next full run, or for the sweep once the job is enabled again. The job finds
its own scope from GKE and the collector's ledger, so nothing has to cross from the watch to the
agent. The job runs as the agent, in the agent's shell, the only principal that may write the
store (§3.6); the hop writes nothing, so a `hermes` caller never touches `/opt/data`, and a model
turn is spent only when an upgrade completed (or once a day for the sweep). A second wake for the
same operation finds it reviewed and prints nothing. The review lands fifteen to twenty-nine minutes
after `DONE` plus the job's start: the replacement pods have had time to settle, and the events of
the operation's last half hour are still inside the API server's hour; events emitted earlier in a
long drain are already gone, which is why (B) reads pod and node state first and treats events as
corroboration. The Sunday run stays the fleet-wide baseline and the only scheduled run that files
the ledger issue (the fleet-wide on-demand route is a full run too, and files it); it reports an operation the after-upgrade job already reviewed as such. The watch and the
second entry are the last part of the second work item (§5), after the scheduled run and the
on-demand route.

### 3.1a On demand: the same report, from a generic question

The routing that sends "is the fleet ready to upgrade" to the readiness report sends "how did the
upgrade go", "did anything break", "any upgrade concerns" here: one line in the chat roster
(`CAPABILITIES.md`) and one in the platform persona's delegation list name what an upgrade _did_
as this report and what it _would do_ as the readiness report. The SOP decides freshness: when the
latest saved report is newer than the last upgrade operation on the clusters the question covers,
answer from it and say when it was produced; otherwise run the collector for those clusters first.
A question that names a cluster the last run did not review forces that cluster (`--cluster`).

### 3.2 Scope: new or upgraded since the last run

The ledger holds, per cluster, the control-plane version, every node pool's version, the time of
the last full review (`last_run`), the symptom set seen at the last full run (owner, category,
reason and onset, no tenant text), and the operations reviewed (id, type, target, end time and the
route that reviewed each, kept while the end time is inside the cluster's selection window). The
selection window is defined once, here, for every route: per cluster, from its last full review to
the run, or the first-run window of fourteen days back when the ledger has no `last_run` for it;
`--since` widens a scoped run's window by hand and never moves `last_run`. Only an operation that
reached `DONE` inside the window is considered on any route; the after-upgrade route adds its
fifteen-minute settle floor at the near end; and the reviewed list is pruned at the earlier of
`last_run` and the fourteen-day floor, so an operation leaves the list only once it can no longer
be selected: Sunday's full run moves `last_run`, and Monday's sweep sees only what ended after it.
The after-upgrade route also needs a baseline to diff against: when the ledger records no full run
it prints one line, "no baseline yet; the first full run establishes it", and writes nothing, so an
install that predates the job gets its first report from the first-run stage or the first Sunday
tick (§3.1), never from a scoped sweep over a fortnight of operations. A scoped run writes, for the clusters it reviewed, its report, the reviewed
operations and the guards (so the readiness check learns a failure the same day, requirement 3);
versions, last-run time and the symptom set stay the full run's, so the next Sunday still selects
the cluster on the operation's end time, reviews it in full against the stored set the last full
run wrote, and grades each incident from its own reads: an Error the after-upgrade run reported
and still present on Sunday is an Error on Sunday too and files at `major`, since Sunday is the
first run that files it, and one that is gone clears its guard and is filed nowhere. What Sunday
marks as already reviewed is the operation, in (A), with the route and time that reviewed it; the
guard the scoped run wrote keeps its first-seen date and gains a review. A second wake for an
operation already in the list prints nothing. This is the one statement of what a scoped run
writes; §3.5 and §3.6 defer to it. The before side of the catalogue's diff is established two ways, and the stronger one
decides. Every full run reads pods and nodes on every fleet cluster, upgraded or not (one list call
each; events too on a cluster holding an event-only guard, §3.5), so the stored set is at most a
week old rather than as old as the previous upgrade; what the stream's checks say about an
unchanged cluster rests on the operations listing that found no upgrade, and §3.3 states the
manifest entry that makes such a Sunday a complete run. And each
symptom carries its own onset. The default is the pod's own evidence: a Pending pod's start, a
crash-looping or not-ready pod's `Ready=False` transition (falling back to its start when the pod
has restarted; a container's last termination is the latest crash, not the first, and is never the
onset), a node condition's transition, an event's first observation. A node-pool upgrade drains
every node and recreates every pod on it, so a pod created inside a pool operation's window is
ambiguous for the failures a recreation carries over (a crash loop, an OOM kill, an image that will
not pull), and for those the owner is consulted as proof of age only: a Deployment whose
`Available=False` or `Progressing=False` transition predates the window makes the symptom "predates
the upgrade", a Warning. Nothing else at the owner is proof: a transition inside the window proves
nothing (on a probe-less crash loop `Available` trails the latest crash), conditions that never
flipped prove nothing, and the ReplicaSet's age proves nothing either, since a pod can run for months
on an old ReplicaSet and fail only on the rebuilt node (a ReplicaSet created inside the window merely
says a rollout happened during it). With no proof of age, a first run
(no stored set) grades the symptom `medium`, a Warning with the reason "the pod was recreated by the
upgrade; the failure may predate it", never an Error; a later full run settles it by the stored set.
A Pending pod created inside the window is not ambiguous: it is the replica the drain displaced,
and it is new. Symptoms bound to an operation, a budget that held a drain (entry 1), a displaced
replica (entry 2), a node that did not come back (entry 17), are incidents of that operation: the
stored set keys them by the operation, so the same budget holding the next upgrade's drain is a new
Error, not "already recorded". A symptom whose onset is
earlier than the first operation of the cluster's upgrade window, or that the previous full run
already recorded, is graded a Warning with the reason "predates the upgrade", never an Error; a
symptom with no readable onset falls back to the stored set alone. The first run has no stored set
and grades by onset only, which it says. A cluster is _new_ when absent, _upgraded_ when a version differs or when
`gcloud container operations list` shows an `UPGRADE_MASTER` or `UPGRADE_NODES` operation targeting
it that reached `DONE`, with or without an `error` (a failed or cancelled operation finishes as
`DONE` with `error` set; `ABORTING` is still in progress), with an end time after the last run, and _re-checked_ when it is unchanged but holds a live guard:
only the reads that guard needs run, and a guard whose symptom or shape is gone is cleared; a guard
whose only source was events adds the events read, and a guard whose read failed is held, not
cleared, and emits no candidate (§3.5). A
cluster a successful listing no longer names leaves the ledger and loses its guards, and the report
says so, but only on a _full_ run. The ledger records the fleet's project set, written by the last
full run. The collector cannot tell the roster from a hand-picked project list, so the SOP says
which it is: a full run is one invoked with `--full`, which the SOP passes with the roster's
`--project` set (the management project and every project a Cluster Agent profile names) on the
scheduled route and on the fleet-wide on-demand route, and which refuses `--cluster`. A full run may
change the fleet set: a project that left the roster is pruned with its clusters, a project that
joined is added. A project the roster still names whose listing failed (a deleted project, a
removed binding, the Container API disabled) does not demote the run: its ledger entries and guards
are held unchanged, its clusters are listed under "Reads that failed", and the run stays full and
moves the latest link, the way `fleet_drift.py` and `patch_readiness.py` keep a failed project as
that project's gap rather than the sweep's. In the collector manifest (§3.3) those clusters are
entries with `outcome: unreachable` and the listing error, which the harness's cross-check turns
into the `scope.skipped` requirement on the findings document: no finding on those clusters is
announced resolved, the ledger issue stays open with the gap named, and the guards keep each
finding's id, first-seen date and review count until a run reads the project again; §3.3 says what
the issue body shows meanwhile. Every run without `--full` is _scoped_, whatever its
`--project` set: a run narrowed by `--cluster`, by the question that invoked it, or any hand run,
with or without `--project`. A scoped run reviews only its targets, never
prunes a ledger entry or a guard outside them, never adds a project to the fleet set (a cluster it
reviews outside the fleet is reported and marked "outside the fleet; not recorded"), writes its
report under a `-scoped` name, does not move the latest link, and never calls the fleet-audit
`start` or `finish`, so a question about one clean cluster cannot close the fleet's ledger issue.
`--since` is a hand-run flag that widens the window a scoped run reviews; it never replaces the
ledger's last-run time. A cluster with an operation still `PENDING`, `RUNNING` or `ABORTING` is not reviewed: a drain in progress shows a
budget with no allowance, a Pending replacement and a `NotReady` node, which are the signatures of
entries 1, 2 and 17 on a cluster that is simply not finished; it is listed under Info as upgrading
now and reviewed on the next run (the after-upgrade route's, once the operation ends), §3.3 says
what the manifest and the SOP write for it, and the on-demand route says the same when asked
mid-upgrade. The collector runs where the fleet-audit collectors run, in the agent's terminal.
Projects are `--project` when given, else the active `gcloud` project plus every project
`gcloud projects list` returns: the order `collect.py`, `patch_readiness.py` and `fleet_drift.py`
share, so this collector reads the same fleet as the three beside it. The SOP passes `--project` for
the management project and every project a Cluster Agent profile names, so a scheduled run reads the
reconciler's roster and agrees with the readiness watch design on what "the fleet" is; a run by hand
without the flag reads what the credential can list, and with or without it is a scoped run unless
it passes `--full`.

### 3.3 A collector for the facts, an SOP for the judgement

(A) and (B) are reads and pattern matches; they belong in a script, so that two runs over the same
cluster say the same thing and a unit test can hold each classifier signature. (C) is mostly a
lookup, so the collector renders it too, from a table of the catalogue's twenty entries (title,
before-signal, the reader that covers it today, mitigate before, mitigate after) held in the script;
the SOP's job is to tailor those sentences to the cluster's own objects and to do (D)'s ledger
issue, which needs the forge. A run with no model available still produces a complete report with
generic (C) text, which is the out-of-the-box experience.

The boundary between the collector and the fleet-audit harness is the one every sibling collector
already uses, the collector manifest
([`fleet-audit-collector-manifest.md`](fleet-audit-collector-manifest.md)), not a contract of this
design's own; the harness's rules are used as they stand on `main`, and this paragraph is the one
statement of what the manifest carries. The stream's roster, its `AUDITS` entry, is six check ids,
each decided by one read: `upgrade-operation-failed` by the operations listing;
`upgrade-broke-workload`, `upgrade-symptom-tentative`, `upgrade-symptom-unclassified` and
`symptom-predates-upgrade` by the pods list; `node-broken-after-upgrade` by the nodes list.
`coverage_gaps` measures every `scope.clusters` entry against all six and files a gap for any it
finds in neither `checks_run` nor `checks_not_applicable`, so each `collected` entry carries all
six or names its gap. On a full run the collector writes the manifest beside the report
(`--manifest-file`), one `clusters[]` entry per cluster it enumerated. A reviewed cluster is
`collected`, with a `commands[]` record per check naming the read that decided it. An unchanged
cluster is `collected` too: the operations listing, with the cluster's versions, decided that
nothing upgraded it, which answers every check that asks what the upgrade did, so `commands[]`
carries all six checks backed by that listing, and a check whose read the re-check made (pods for
the four workload checks, nodes for the node check) is backed by that read instead; "only the
reads that guard needs" (§3.2) is a read rule, and the listing is what the checks rest on. A check
whose read ran and failed, in a review or in a re-check, is in `checks_unevaluated[]` with the
read, the read is named in `limitations`, and the check is in neither `commands[]` nor
`checks_run`; a failed operations listing leaves `upgrade-operation-failed` unevaluated on every
cluster of that project, and an unchanged cluster of that project carries only the checks a
re-check read backs, so `coverage_gaps` names the rest. A cluster whose project listing failed or
whose every read failed is `unreachable` or `gate-failed` with the error, and a cluster upgrading
now is `gate-failed` with the operation in flight as its `error`: the run read nothing on it.
`candidates[]` carries one candidate per incident the report files, an Error at `major` and a
Warning at `minor`, with the check id, object, excerpt and the mitigation text, and one per
`failure` guard the run observed again (§3.5), under the check id its finding was first filed with,
graded the Warning requirement 3 makes it and carrying the persistence in its detail: first seen,
and the number of reviews that have seen it. A `risk` guard is Info, filed nowhere, and is no
candidate. The carried keys the manifest reserves for collector-resolved fleet facts hold the
versions, operations and incident kinds the SOP copies. The SOP's `draft` builds `scope.clusters`
from the `collected` entries, with `checks_run` equal to their `commands[]` and the collector's
`limitations` copied, and `scope.skipped` from the rest with their errors, so on a Sunday where
every cluster was read and nothing was upgraded `coverage_gaps` is empty, the run is complete, and
a clean document closes the ledger issue and runs silent (requirements 3(D) and 4).

The hold follows the rule the harness implements and no other. A previous finding is announced
resolved only on a complete run, and only when no candidate carries its `(check, cluster,
namespace, object)` id (`still_flagged_ids` is that set of candidate ids). A guard the re-check
observes again therefore emits its candidate, the unclassified Warning's guard among them (§3.5),
under its original check id, and a guard the re-check clears emits nothing, so the harness resolves
the finding. A persisting finding has no check id of its own: a candidate under a new id would
file a new finding and let the harness announce the old one fixed, the resolved-plus-new pair the
fixed row order (§3.4) exists to prevent. A cluster the run did not read, upgrading now,
unreachable, or with a re-check read that failed, emits no candidate: a candidate with no read
behind it would make the manifest vouch for an observation nobody made, and
`checks_not_applicable` is for a check that cannot apply to a cluster (Autopilot, no such resource
kind), not for one deferred. What `finish` does with such a run is fixed: the run is partial, so
`finish` announces no finding resolved, closes nothing, keeps the ledger issue open and posts a
comment naming the cluster. The issue body does not keep the unread cluster's entries meanwhile.
The body is rewritten on every run from the document and the candidates; the validator refuses a
finding on a cluster in `scope.skipped`; a held row exists only for a candidate
(`collector_held_entries` is the previous body's ids that candidates still carry); and the harness
holds nothing on a cluster the run did not read, so those entries leave the body for the gap. The
stream's memory across the gap is `guards.json`: each guard keeps the finding's id, its first-seen
date and its review count unchanged, since no run observed it, and the next run that reads the
cluster decides from its own reads. A failure still present emits the candidate under the same id
with that persistence in its detail, so the entry returns to the body reading "first seen <date>,
N reviews"; the delta marks it new to the body, because the previous body lacked it, and the
entry's own text says it is not. A failure that is gone clears the guard and emits nothing; no
resolution is announced, since no body carried it, and the report's Info line records the cleared
guard. The after-upgrade route reads an upgrading cluster within the half hour, but a scoped run
files nothing, so the gap in the body lasts until the next full run. The stream is in
`COLLECTOR_AUDITS`, the SOP passes the manifest to `finish`, and `cross_check_manifest` enforces
what this design would otherwise state as prose: an unreachable cluster must be in
`scope.skipped`, a check the collector did not run cannot be claimed, every candidate is published
or disclosed, and the held set is the manifest's. `report.json` and the Markdown remain the human
report; they are not what `finish` reads.

### 3.4 Classification by signature, with the evidence attached

Each symptom carries the catalogue entry it matches, a confidence, and the evidence string. The
signatures:

| Symptom read from the cluster                                                                          | Entry |
| ------------------------------------------------------------------------------------------------------ | ----- |
| `failed calling webhook` in a `FailedCreate` event or a pod's message                                  | 7     |
| `didn't match … node selector` / node affinity on a pod                                                | 12    |
| `OOMKilled` on a cgroup v2 pool with a runtime image older than the catalogue's floor                  | 14    |
| `OOMKilled` where the container runs several processes                                                 | 15    |
| `ImagePullBackOff` / `ErrImagePull` on rebuilt nodes while the same image runs elsewhere               | 20    |
| `PersistentVolume's node affinity`, `FailedAttachVolume`, `FailedMount`                                | 19    |
| `nvidia.com/gpu` in a scheduling message; `nvidia`, `CUDA`, `Error 803` in a container                 | 18    |
| nodes `NotReady` / `NetworkUnavailable` after a node-pool operation                                    | 17    |
| `Insufficient cpu` / `memory` on a Pending pod after a node-pool operation                             | 2     |
| a budget with no allowance left on a drained node; a node operation past an hour per node              | 1     |
| `no matches for kind`; a Job or CronJob pod in `Error` whose spec names a removed API                  | 6     |
| every replica of a multi-replica workload unavailable inside one `UPGRADE_NODES` span, all on its pool | 3     |
| a pool two minors from its control plane, or one pool on two kubelet versions, standing                | 5     |
| more than a day after the last window close or exclusion end                                           |       |

Rows overlap, and a symptom carries exactly one entry, so the rows are tried in a fixed order and
the first that holds wins; the order is the most specific discriminator first, so a finding id (the
manifest derives it from the check id, cluster, namespace and object) never flips between runs on
the same evidence: 7 (a webhook named), 6 (a removed API named), 19 (a PersistentVolume's node
affinity, an attach or mount failure), 18 (a scheduling message that names `nvidia.com/gpu`, or a
driver error text in a container whose image or command names a GPU driver component), 14 (the
pool's cgroup mode and the runtime floor are facts of the pool and the image) before 15 (several
processes), then 20, 17, 3 (an `Available=False` transition inside one `UPGRADE_NODES` span, every
pod on the drained pool or in its zone, on a workload with more than one replica), 2 (`Insufficient
cpu` or `memory` in the clause for the pool the pod targets, after a node-pool operation), 1, 5 (a
version skew standing more than a day past the last window close or exclusion end; it is
cluster-scoped and collides with nothing), and 12 last. The order is tried per symptom, and a
symptom's source already narrows the rows it can reach: a workload's `Available` condition reaches
row 3 alone, a budget reaches row 1 alone, a Pending pod's scheduling message reaches 18, 2 and 12.
An object's incident then carries every symptom on it, each with its one entry, and the finding id
is per object and check, not per entry, so the Deployment whose replacement is Pending (entry 2)
and whose `Available` condition fell inside the span (entry 3) is one finding with two entries, not
two findings, and a budget hold (entry 1) is a finding on the budget. Row 3 keeps the catalogue's
floor, a workload with more than one replica: a single replica displaced by a drain is entry 2 (its
Pending replacement) or entry 1 (the budget that held its drain), never 3, so
`seeded-upgrade/pinned-batch-runner` files entry 1 on its budget (§4) and, while its replacement is
still Pending, a second incident on the Deployment graded by the replacement's scheduling message
(row 2: its selector names a label its pool's nodes carry, and that pool's clause reads
`Insufficient cpu`). Row 12 is the generic one, and its discriminator is the nodes read, not the
message. The scheduler writes a `didn't match Pod's node affinity/selector` clause for every node
group a pinned pod does not target, beside the clause that says why its own pool refused it
(`Insufficient cpu`, `Too many pods`, `were unschedulable`, an untolerated taint), so on a cluster
with more than one pool every pinned Pending pod carries a miss clause and the clause proves
nothing. The collector reads the pod's pins, its `nodeSelector` and required node affinity, against
the labels the nodes carry. When a node carries the labels of one of the pod's terms, the pod's own
pool evaluated it and refused it for the reason in that pool's clause: row 2 when the clause reads
`Insufficient cpu` or `memory`, row 18 for `nvidia.com/gpu`, else unclassified, never 12. Row 12
holds only when no node carries the pinned labels. That still does not say the pool lost the label:
a label the pool never carried, a deleted pool or an autoscaled pool at zero nodes read the same,
and the collector holds no before-state of node labels, so row 12 is `medium`. The repository's
entry-12 reproduction, `bench/upgrade-scenarios/evidence/12/label.txt`, is this case once the pool
is rebuilt: the pool's node carried `team=legacy` before the operation and its replacement does
not, so no node carries the pinned label from then on. Its drain-time message, `1 node(s) were
unschedulable, 2 node(s) didn't match Pod's node affinity/selector`, is one no run grades, since a
cluster with an operation in flight is not reviewed (§3.2); a node cordoned by hand on a settled
cluster that still carries the label makes the pod unclassified, which is right, since the cordon
refused it. The clause set, the comma-separated reasons after `nodes are available:` up to the
first period with the `preemption:` sentence excluded, is quoted as evidence and names the pool's
own reason; it decides no row. A row-12 match is `high` only when the selector names a label in
the collector's `DROPPED_NODE_LABELS` table, label to the minor at which GKE's node image or
kubelet stopped setting it, each entry with its source the way `removed_apis.json` beside
`api_deprecation_scan.py` cites one. The table is empty today. The catalogue's entry 12 records
that no GKE label removal is verified, and the one public case, kubeadm's
`node-role.kubernetes.io/master` lost at 1.24, is a label no GKE node ever carried (the
reproduction's nodes show `ROLES <none>` before and after the rebuild) and is entry 17's evidence,
so it is not an entry. An entry joins the table only with a GKE release note or GKE document that
names the label and the minor, never from a kubeadm or upstream change and never from memory; the
deprecated `beta.kubernetes.io/` and `failure-domain.beta.kubernetes.io/` labels, still set, stay a
before-signal shape and never a drop. Until an entry is sourced every row-12 match is `medium`, and
the `high` branch is reached only by the unit test that injects an entry. An
`OOMKilled` container on a migrated pool with an old runtime that also runs several processes is
entry 14, with entry 15 named in the evidence as a second cause.

A match is _sure_ (`high`) when the signature names the entry's own mechanism and the pool the
object sits on had an operation in the window: a webhook named in the rejection, a selector that
names a label the dropped-label table lists (none today), a runtime image below the catalogue's
floor on a pool migrated to cgroup v2, an image that pulls on untouched nodes and fails on rebuilt
ones, a volume attach error naming a PersistentVolume, a driver error text, a budget with no
allowance on a drained node. On a first run a match whose only onset is a recreated pod's is capped
at `medium` (§3.2). Anything less,
a generic `OOMKilled`, an image pull failure with no untouched node to compare, a Pending pod with
no pool operation, is _tentative_ (`medium`). Text a namespace user can write is never enough for
`high` on its own: an Event's `reason` and `message` and a container's termination message are
tenant-authored, so a signature matched only there (row 7's `failed calling webhook`, row 19's
`FailedMount`, row 18's driver text) is `medium` and marked "from event text" unless a field the
API server sets agrees with it, a pod phase or container state, a node condition, a scheduling
status; and wherever the excerpt travels, the guard, the manifest candidate, the ledger issue, it is
quoted as the object's own text, not stated as the review's finding. A symptom that matches nothing is reported as a
Warning, unclassified, rather than dropped: the report is a record of the upgrade, not only of the
catalogue's part of it. The entries not in the table (4, 8, 9, 10, 11, 13, 16) have no symptom a
single read identifies with confidence; they are the ones the readiness checks have to catch before
the upgrade, and the report says so under (C) when a cluster's symptoms are unclassified. Entries 3
and 5 joined the table from the catalogue's gap review: entry 3 is a Deployment's `Available=False`
transition, or a StatefulSet with no ready replica, on a workload with more than one replica,
inside one `UPGRADE_NODES` span with every pod on that pool (high; medium when the pods were in the
zone it drained), keyed by the operation like a budget hold; entry 5 is read from the cluster
describe and the maintenance policy, medium, and only
once the skew has stood more than a day after the latest window close or exclusion end (while a
window is open or an exclusion active it is an Info line).

### 3.5 Mitigation set up, not only described

(D) is what separates a retrospective from a post-mortem nobody reads. Two mechanisms, both
automatic and both reversible:

- **Guards.** `guards.json` beside the ledger holds one entry per filed incident, classified or
  not, and one per risk shape found on a clean cluster, each marked `failure` or `risk`: cluster,
  entry (`unclassified` for a symptom no row holds for), object (`namespace/kind/name`), evidence,
  source (pod or node state, or events alone), the check id its incident was filed under, first and
  last seen, and the number of reviews that have seen it; the last three are what its re-emitted
  candidate carries (§3.3). An unclassified Warning carries
  a `failure` guard like a classified one, because the guard is what makes the next run re-check
  the cluster for it (§3.2) and re-emit its candidate while a run observes it (§3.3); without one the
  finding is filed once and announced fixed by the first run that does not re-read the cluster. The
  re-check reads what the symptom came from: a symptom read from pod or node state is looked for
  again with those reads and, when absent, cleared, which emits no candidate and lets the harness
  resolve the finding. One whose only source was events, such as §4's probe failures, is looked for
  again through events. The upgrade window's events are gone after the API server's hour, but the
  symptom is not: a probe that still fails writes `Unhealthy` with a fresh `lastTimestamp`, which is
  how §4 counts 31, and a fixed one writes none. So the re-check of a cluster holding such a guard
  adds the events read (one list call), refreshes the guard when an event in the API server's hour
  still names the object or its pods, or the pods read shows the object failing, and clears it when
  both reads answer and neither does. The Sunday run re-checks every cluster that holds a guard, so
  a fix made in the week clears the guard by the next Sunday, before the next upgrade, as
  requirement 2 says; the event hour is the limit, and a symptom that recurs less often than hourly
  can be absent at the read, clear, and be filed again when a review sees it (§8). A re-check read
  that fails holds the guard as it stands: no candidate, no review counted, the check in the
  cluster's `checks_unevaluated[]` and the failed read in its `limitations`, so the partial run
  announces nothing resolved; the finding's entry leaves the ledger body for that run and returns,
  with its first-seen date and review count, when a run reads the cluster and finds it, as §3.3
  states for every gap. The collector merges the file on
  every run (new, seen again, gone when the cluster is reviewed or re-checked and the symptom or
  shape is absent, and dropped with a cluster that left the fleet). The daily readiness watch, once
  it ships, reads the file through its own sandbox hop and adds a line per live guard to its next
  report for that cluster, so the operator planning the next upgrade sees "the last upgrade held
  the drain on `seeded-upgrade/pinned-batch-runner`'s budget; it still allows no disruption".
- **The stream's ledger issue.** Where the install has a repository, the retrospective is a
  fleet-audit stream and keeps what every stream keeps: exactly one open issue, rewritten in full
  on every run, with each finding identified by check id, cluster, namespace and object, held
  across runs by §3.3's rule, and closed by a run that finds nothing. That one issue is the (C)
  checklist for the whole fleet; a per-cluster issue would need a
  ledger mode the skill does not have and the skill forbids opening issues any other way.

Opening a pull request for a manifest change is not (D). The retrospective is a fleet-audit stream,
and that skill, not `submit-suggestion`, owns pull requests for audit findings. Its `finish` step
opens a remediation pull request on its own for every `critical` finding and for a `major` one
whose check id is on its `MAJOR_SWEEP_CHECKS` allowlist. So the stream grades its findings
deliberately: an Error files at `major`, a Warning at `minor`, nothing ever at `critical`, and its
check ids stay off `MAJOR_SWEEP_CHECKS` (a reused `no-pdb` slug would put the budget finding on
it). The grade is the collector's, written on each manifest candidate (§3.3), so the SOP copies it
rather than judging it; the report names the file and the change a user's "apply" would make.

### 3.6 Where the report lives

The collector runs in the agent's shell, so its store is on whichever pod that shell runs in: with
the shell sandbox on, the sandbox pod's own data volume, which is not the gateway's profile volume
(the [fleet-audit report store](fleet-audit-report-store.md) worked this out for the audit
streams). The root is fixed at `/opt/data/upgrade-retrospective/`, overridable by an environment
variable, rather than under `$HERMES_HOME`, because a cron worker and a chat session resolve that
variable to different directories and a store rooted there is written at one path and read from
another. Under the root: `reports/<timestamp>.md`, `upgrade-retro-report.md` beside `reports/` pointing
at the latest full run, the same report as `.json`, `ledger.json` and `guards.json`. The volume survives a
pod restart, every session's tools can read it, and the on-demand route finds the Sunday report
there. Three triggers write the same files, so one run holds an exclusive lock on `.lock` under the
root for its duration, and the SOP runs the collector in the foreground with the terminal tool's
`timeout: 600`, the foreground maximum the fleet-audit SOPs name, never in the background. The
budget is this design's own figure, not a sibling's precedent: the obtainability SOP runs its
collector the same way at the same ceiling, and that collector, which reads a comparable set per
cluster, takes 60–80 seconds on a twelve-cluster fleet. A background run was rejected for the
sibling's reason and one of its own: it outlives the turn, so the SOP would have to poll for its
end before `finish`, and a wake that starts a second run beside it is two writers past the lock.
The lock wait is sized to fit under the budget with the run: a second run, scheduled,
after-upgrade or on demand, waits up to two minutes, long enough for a scoped run of a few clusters
to finish, which is the collision that matters, the Sunday run or a question landing while an
after-upgrade run holds the lock. The reverse costs nothing: an after-upgrade wake locked out by the
Sunday run exits after the wait with one line and writes nothing, the Sunday run reviews the same
operation, and the route's next wake or sweep finds it in the reviewed list and prints nothing; an
on-demand run locked out answers from the latest saved report and says a run is in progress. A
dry run reads without the lock. Reports are named by their finish time in UTC, full
runs as `reports/<timestamp>.md` and scoped runs as `reports/<timestamp>-scoped.md`, so two runs on
one day never replace each other; each ring is pruned to the newest fourteen, the retention the
fleet-audit report store uses, and only a full run moves the latest link. Each file is written to a
temporary name and renamed,
in the order report, guards, ledger, so a run that dies leaves at most a report with no ledger
advance; a ledger that cannot be parsed is set aside under a dated name and the run stops with that
as its line. While a dated crash record sits beside no ledger, every run refuses to start from empty
and repeats that line, so the record is read rather than overwritten; `--reset-ledger`, run by an
operator on purpose, archives the record and lets the next run start as a first run. A run starts
from an empty ledger on its own only when neither a ledger nor a crash record exists, and only on a
route that may: the after-upgrade route with no full run recorded prints its no-baseline line
instead (§3.2). The readiness watch runs on the gateway pod and reaches the file the same way it reaches
`gcloud`, through its sandbox hop. The chat line still carries the counts and the top finding, so
a reader who never opens the file gets the verdict.

### 3.7 What the run may not do

Read only. The collector runs `gcloud … list|describe` and `kubectl get`; the SOP's only writes are
the ledger issue and the chat message. No upgrade, rollback, cordon, delete or patch, and no change
to a cluster's maintenance policy.

## 4. The report

```
# Upgrade retrospective 2026-10-11

## Errors

### Entry 1 on seeded-b: seeded-upgrade/PodDisruptionBudget/pinned-batch-runner
What happened: UPGRADE_MASTER control plane 1.34.11-gke.1209000 -> 1.34.12-gke.1011000,
  2026-10-07 18:27 to 18:34 (6m54s), DONE; UPGRADE_NODES no-surge-pool, 18:34 to 19:37 (62m), DONE.
What failed: drain held, disruptionsAllowed 0 on one replica, catalogue entry 1 (a
  PodDisruptionBudget forbids the eviction), high; evidence: maxUnavailable 0, 1 of 1 pods on the
  drained no-surge-pool, operation past an hour on one node.
Detect and mitigate next time: visible before the upgrade as the budget itself; read today by the
  daily obtainability audit (blocking-pdb) and by fleet-upgrade-verification --readiness when asked.
  Before the next upgrade: allow
  one disruption or add a replica. Now: the pod is back; nothing to repair.
Mitigation set up: guard seeded-b / entry 1 / seeded-upgrade/PodDisruptionBudget/pinned-batch-runner
  (first seen 2026-10-11), reported by the readiness watch until the budget has room; entry in the
  stream's ledger issue <url> where a repository is linked.

## Warnings

### Entry 14 on <cluster>: <namespace>/Deployment/<name>
What happened: UPGRADE_NODES default-pool 1.35.7 -> 1.35.8, <date> (9m), DONE.
What failed: 1 of 1 pods CrashLoopBackOff, container OOMKilled on a cgroup v2 pool, catalogue
  entry 14 (cgroup v2 under a runtime that cannot read it), medium; evidence: image
  eclipse-temurin:8u302-b08-jre, one container.
Detect and mitigate next time: ...
Mitigation set up: guard ...

### Unclassified on <cluster>: <namespace>/Deployment/<name>
What failed: Unhealthy probe events, 31 in the window, matching none of the twenty.
Mitigation set up: guard <cluster> / unclassified / <namespace>/Deployment/<name> (first seen
  <date>), event-only: re-checked through events, cleared by the next Sunday run that finds no live
  symptom on the object; entry in the stream's ledger issue where a repository is linked.

## Info

### seeded-c
What happened: UPGRADE_MASTER 2026-10-06 03:27 (8m), UPGRADE_NODES default-pool 2026-10-07 03:26
  (5m); no failure found.
Next upgrade: at the channel default, 1.35.8-gke.1225000 (cluster is ahead; nothing pending);
  window daily 03:00Z for 4h, next opens 2026-10-12 03:00Z; no exclusion.
Risks present: none of the catalogue shapes checked (budgets, selectors, image hosts, in-tree
  volumes, node agents, local state, GPU pins, webhooks).
Baseline recorded: control plane and 1 pool at 1.35.8-gke.1380001, 14 pods, 2 budgets, 0 shapes;
  no guard written.

### <cluster>
What happened: new (first seen), 1.35.8-gke.1380001.
Next upgrade: at the channel default; window daily 03:00Z for 4h; no exclusion.
Risks present:
| object                                   | shape                                     | entry | confidence |
| <namespace>/Deployment/<name>            | image host k8s.gcr.io                     | 20    | high       |
| <namespace>/PersistentVolume/<name>      | gcePersistentDisk in-tree, CSI add-on off | 19    | high       |
Baseline recorded: 3 pools, 41 pods, 4 budgets, 2 shapes; 2 risk guards written.

- Unchanged: seeded-d, last upgraded <date> (UPGRADE_NODES second-zone-pool), next target
  1.35.8-gke.1225000.
- Upgrading now: <cluster> (UPGRADE_NODES <pool> since <time>); reviewed on the next run.
- Reads that failed: none.
```

The example is illustrative: the one object the seeded fleet has is named
(`seeded-upgrade/pinned-batch-runner` on seeded-b), everything else is a placeholder, and the dates
and verdicts are invented. A real run's content
comes from the collector's JSON, which keeps the per-cluster data and the same three-way grouping.

## 5. Work breakdown

1. **The collector.** `agents/platform/skills/fleet-audit/scripts/upgrade_retrospective.py`: the
   ledger, selection, (A), (B), the signature table, the catalogue table for (C), the guards merge,
   JSON and Markdown output, the collector manifest (`--manifest-file`, §3.3) with the stream's
   check ids and its `COLLECTOR_AUDITS` entry, `--dry-run`, `--full`, `--cluster`, `--since` and
   `--reset-ledger` as §3.2 and §3.6 define them; unit tests on fixtures captured from the test
   fleet, including the manifest against `cross_check_manifest`.
2. **The job and the on-demand route.** `agents/platform/governance/upgrade_retrospective_sop.md`
   (including the freshness rule for a question); the routing lines in `CAPABILITIES.md` and the
   platform `AGENTS.md`; the roster entry
   (`0 18 * * 0`, `skills: ["fleet-audit"]`, the `AUDITS` entry with the six check ids §3.3 lists, so findings file, with check
   ids off `MAJOR_SWEEP_CHECKS`); the SOP's two paths (`start`, the collector and `finish` with its
   manifest where a repository is linked; the collector alone without one) and the first-run stage
   marking this job due without a repository; the `upgrade-retrospective-after-upgrade` roster
   entry (daily 05:00 UTC, the SOP's after-upgrade route, the collector's `--after-upgrade`
   selection) and `agents/platform/scripts/upgrade_retrospective_watch.py` with its `no_agent`
   entry (`*/15 * * * *`), which wakes it (§3.1); the readiness
   watch reading `guards.json` through its sandbox hop once it ships; the cron README section and
   the generated cron reference.
3. **The proof.** The first report over the test fleet; one nightly case per catalogue entry the
   report must classify, starting with the entries that have no passing record yet; the catalogue's
   status table gains the retrospective as the after-the-fact reader.

## 6. Files touched

- `agents/platform/skills/fleet-audit/scripts/upgrade_retrospective.py`, its test and `testdata/`.
- `agents/platform/governance/upgrade_retrospective_sop.md`, `agents/platform/CAPABILITIES.md`,
  `agents/platform/AGENTS.md` (the on-demand route).
- `agents/platform/cron/jobs.json`, `agents/platform/cron/README.md`,
  `agents/platform/skills/fleet-audit/scripts/audit_report.py` (`AUDITS`, `COLLECTOR_AUDITS`).
- `agents/platform/scripts/upgrade_retrospective_watch.py` and its test (the after-upgrade wake),
  and the collector's `--after-upgrade` selection.
- The Chat Agent's first-run stage (`agents/chat/scripts/oobe.py`: the list of audits it starts
  after the inventory scan, and its repository skip).
- The readiness watch's script, once it is on `main` (reads `guards.json` through its sandbox hop).
- `docs/site/src/content/docs/reference/cron-jobs.md` (generated), the autonomous-watchdogs and
  security reference pages where they enumerate the roster.

## 7. Testing

- **Unit.** Each classifier signature against one captured fixture, row 3's against a workload of
  one replica (entry 2 or 1, never 3) and of two, row 12's against the entry-12 reproduction after
  the rebuild (no node carries the pinned label: `medium`), against a pinned pod whose pool's nodes
  carry the label and refuse it for a taint or `Too many pods` (unclassified, never 12), and the
  `high` branch with a table entry the test injects, the shipped table being empty; selection (new,
  upgraded by version, upgraded by operation, unchanged, forced; `--after-upgrade` against the
  window's two bounds, the reviewed list pruned at the window, and the no-baseline line when no
  full run is recorded); the ledger and guards round trips, an unclassified symptom's guard among
  them, an event-only guard refreshed by a re-check whose events read still names the object and
  cleared by one that does not; a persisting guard's candidate under its original check id with
  first seen and the review count, a cleared guard emitting nothing, and a guard whose re-check
  read failed emitting nothing with its check in `checks_unevaluated[]` (the manifest against
  `still_flagged_ids` and `cross_check_manifest`); the manifest entry of an unchanged cluster
  carrying all six checks backed by the operations listing, so `coverage_gaps` is empty; a guard's
  id, first-seen date and review count unchanged across a run that did not read its cluster, and
  the candidate the next reading run emits carrying them; an Error the after-upgrade route
  reported, filed at `major` by the next full run; an unreachable cluster and an upgrading cluster
  recorded without failing the run and without a candidate; the watch marking nothing for a
  paused, disabled or absent job; the report rendering, including an empty section and the
  severity of each fixture incident.
- **Eval.** One case per entry the first report classifies on the test fleet, graded on declared
  lines (`<cluster>/<object>: entry <n>`), red on `main` where the report does not exist, green
  three times on the branch; one case for the on-demand route, a generic question ("did anything
  break in the last upgrades") that must be answered from the report; registered in the nightly
  roster. The out-of-the-box path (collector
  alone, no model) is a test, not an eval.
- **Live.** The first run on a dev install: the first-run stage producing the baseline report; a
  weekend tick after an upgrade producing an incident with all four parts; a guard appearing in the
  next readiness report once the watch ships; the ledger issue where a repository is linked.

## 8. Accepted risks and open questions

- Events expire; a Sunday-evening run sees the week's pod and node state but may miss transient
  events.
  The ledger's version diff still finds the upgrade; (B) says when its evidence is state rather than
  an event. An event-only guard is re-checked through the same hour (§3.5): a symptom that recurs
  less often than hourly can clear, and is filed again when a review sees it.
- Classification is by signature and can be wrong; every finding carries its evidence and a
  confidence, and the SOP may downgrade one. A wrong guard costs one extra line in a readiness
  report until the next run re-checks the cluster and drops it, the next Sunday run for a guard
  whose only source was events.
- A cluster a Sunday run cannot read, upgrading at the tick or with a failed read, keeps the ledger
  issue open and takes its entries out of the issue body for that week; `guards.json` keeps their
  ids, first-seen dates and review counts, and the next full run restores or clears them from its
  own reads (§3.3). Not running `finish` on such a Sunday would keep the body whole but freeze the
  whole fleet's ledger for the week, and was rejected for that.
- Fourteen days for the first run, and a fixed Sunday evening for the fleet-wide run (the
  after-upgrade watch covers the slot after each window) rather than a slot after each
  install's maintenance window, are starting values.
- The 600-second foreground budget and the two-minute lock wait (§3.6) are sized on a fleet of the
  size the sibling collectors are measured on. A fleet whose full run does not fit under the budget
  with the wait is an open question, to be settled by measurement, not by a background run.
- Audit-log reads (eviction 429s, admission rejections) would sharpen (B); `gcloud logging read` is
  on the allowlist, cost and scope to decide.
- Whether a live guard should turn the readiness verdict for that cluster to `blocked`, or only
  annotate it, is the readiness watch's decision once it reads the file.
