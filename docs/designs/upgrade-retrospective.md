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
   checked again for that finding each time. A fix made between two upgrades then clears the
   finding before the next upgrade.
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
     The next review rewrites the checklist. The review closes the issue when it finds nothing.

   An incident is an **Error** when the match is certain and the object belongs to the user, when
   GKE reported the upgrade as failed, or when a node did not become ready after its recreation. An
   incident is a **Warning** when the match is tentative, when the object belongs to the cluster's
   system components, when the symptom matches none of the twenty failures, or when a failure from
   an earlier review is still present with no new evidence. **Info** lists each cluster that was
   upgraded or found with no failure, each cluster with no change, and each item the review could
   not read.

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
   not read gives the reason. "None" appears only when the count is zero.

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
the first Sunday tick.

A third trigger reviews a cluster soon after its upgrade rather than at the weekend. It is a
second roster entry, `upgrade-retrospective-after-upgrade`, whose prompt is the SOP's
after-upgrade route: the collector run with `--after-upgrade`, which selects the clusters whose
`UPGRADE_MASTER` or `UPGRADE_NODES` operation reached `DONE` since the collector's last review and
is not recorded in its ledger as reviewed, as a scoped run (§3.2) that posts its lines and files no
ledger issue. The entry has a daily schedule as a sweep, 05:00 UTC, and is woken early by a
`no_agent` script, `upgrade_retrospective_watch.py`, which runs every fifteen minutes on the gateway
pod, reads the roster projects' operations through the sandbox hop the readiness watch uses
(`sandbox_exec` as its default principal, read only) and, when an operation reached `DONE` at least
fifteen minutes earlier that it has not seen, marks the after-upgrade job due with the same
`trigger_job` call the first-run stage uses, which carries a job id and nothing else; the job finds
its own scope from GKE and the collector's ledger, so nothing has to cross from the watch to the
agent. The job runs as the agent, in the agent's shell, the only principal that may write the
store (§3.6); the hop writes nothing, so a `hermes` caller never touches `/opt/data`, and a model
turn is spent only when an upgrade completed (or once a day for the sweep). A second wake for the
same operation finds it reviewed and prints nothing. The review lands fifteen to twenty-nine minutes
after `DONE` plus the job's start: the replacement pods have had time to settle, and the events of
the operation's last half hour are still inside the API server's hour; events emitted earlier in a
long drain are already gone, which is why (B) reads pod and node state first and treats events as
corroboration. The Sunday run stays the fleet-wide baseline and the only run that files the ledger
issue; it reports an operation the after-upgrade job already reviewed as such. The watch and the
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
the last run, and the symptom set seen at the last full run (owner, category, reason and onset, no
tenant text). The before side of the catalogue's diff is established two ways, and the stronger one
decides. Every full run reads pods and nodes on every fleet cluster, upgraded or not (one list call
each), so the stored set is at most a week old rather than as old as the previous upgrade. And each
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
only the reads that guard needs run, and a guard whose symptom or shape is gone is cleared. A
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
into the `scope.skipped` requirement on the findings document: the stream's previous findings on
those clusters are held rather than resolved, the ledger issue stays open, and nothing is posted as
fixed because a project went unread. Every run without `--full` is _scoped_, whatever its
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
now and reviewed on the next run, and the on-demand route says the same when asked mid-upgrade. The
first run has no ledger and reviews the last fourteen days of operations. The collector runs where the fleet-audit collectors run, in the agent's terminal.
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
design's own. On a full run the collector writes it beside the report (`--manifest-file`): one
`clusters[]` entry per cluster it enumerated, with `outcome: collected` and a `commands[]` record
per check that ran, or `outcome: unreachable` or `gate-failed` with the error for a cluster whose
project listing failed, whose reads failed, or that is upgrading now; `candidates[]` for every
incident the report files, an Error at `major` and a Warning at `minor`, with the check id, object,
excerpt and the mitigation text, including a candidate for every `failure` guard the run still
observes, which is how the harness holds a finding on the ledger (its `still_flagged_ids` are the
candidates the collector still emits); a `risk` guard is Info, filed nowhere, and is no candidate,
so a clean Sunday discloses nothing and runs silent; `checks_unevaluated[]` and `limitations` on a
cluster whose read failed; and, in the carried keys the manifest reserves for
collector-resolved fleet facts, the versions, operations and incident kinds the SOP copies. The
stream is in `COLLECTOR_AUDITS`, the SOP passes the manifest to `finish`, and `cross_check_manifest`
enforces what this design would otherwise state as prose: an unreachable cluster must be in
`scope.skipped`, a check the collector did not run cannot be claimed, every candidate is published
or disclosed, and the held set is the manifest's. `report.json` and the Markdown remain the human
report; they are not what `finish` reads.

### 3.4 Classification by signature, with the evidence attached

Each symptom carries the catalogue entry it matches, a confidence, and the evidence string. The
signatures:

| Symptom read from the cluster                                                             | Entry |
| ----------------------------------------------------------------------------------------- | ----- |
| `failed calling webhook` in a `FailedCreate` event or a pod's message                     | 7     |
| `didn't match … node selector` / node affinity on a pod                                   | 12    |
| `OOMKilled` on a cgroup v2 pool with a runtime image older than the catalogue's floor     | 14    |
| `OOMKilled` where the container runs several processes                                    | 15    |
| `ImagePullBackOff` / `ErrImagePull` on rebuilt nodes while the same image runs elsewhere  | 20    |
| `PersistentVolume's node affinity`, `FailedAttachVolume`, `FailedMount`                   | 19    |
| `nvidia.com/gpu` in a scheduling message; `nvidia`, `CUDA`, `Error 803` in a container    | 18    |
| nodes `NotReady` / `NetworkUnavailable` after a node-pool operation                       | 17    |
| `Insufficient cpu` / `memory` on a Pending pod after a node-pool operation                | 2     |
| a budget with no allowance left on a drained node; a node operation past an hour per node | 1     |
| `no matches for kind`; a Job or CronJob pod in `Error` whose spec names a removed API     | 6     |

Rows overlap, and a symptom carries exactly one entry, so the rows are tried in a fixed order and
the first that holds wins; the order is the most specific discriminator first, so a finding id (the
manifest derives it from the check id, cluster, namespace and object) never flips between runs on
the same evidence: 7 (a webhook named), 6 (a removed API named), 19 (a PersistentVolume's node
affinity, an attach or mount failure) before 12 (any other selector or affinity miss), 18 (a
scheduling message that names `nvidia.com/gpu`, or a driver error text in a container whose image
or command names a GPU driver component) before 14 and 15, 14 (the pool's cgroup mode and the
runtime floor are facts of the pool and the image) before 15 (several processes), then 20, 17, 2
and 1. An `OOMKilled` container on a migrated pool with an old runtime that also runs several
processes is entry 14, with entry 15 named in the evidence as a second cause.

A match is _sure_ (`high`) when the signature names the entry's own mechanism and the pool the
object sits on had an operation in the window: a webhook named in the rejection, a selector that
names a dropped label, a runtime image below the catalogue's floor on a pool migrated to cgroup v2,
an image that pulls on untouched nodes and fails on rebuilt ones, a volume attach error naming a
PersistentVolume, a driver error text, a budget with no allowance on a drained node. On a first run a match whose only onset is a recreated pod's is capped at `medium` (§3.2). Anything less,
a generic `OOMKilled`, an image pull failure with no untouched node to compare, a Pending pod with
no pool operation, is _tentative_ (`medium`). Text a namespace user can write is never enough for
`high` on its own: an Event's `reason` and `message` and a container's termination message are
tenant-authored, so a signature matched only there (row 7's `failed calling webhook`, row 19's
`FailedMount`, row 18's driver text) is `medium` and marked "from event text" unless a field the
API server sets agrees with it, a pod phase or container state, a node condition, a scheduling
status; and wherever the excerpt travels, the guard, the manifest candidate, the ledger issue, it is
quoted as the object's own text, not stated as the review's finding. A symptom that matches nothing is reported as a
Warning, unclassified, rather than dropped: the report is a record of the upgrade, not only of the
catalogue's part of it. The entries not in the table (3, 4,
5, 8, 9, 10, 11, 13, 16) have no symptom a single read identifies with confidence; they are the
ones the readiness checks have to catch before the upgrade, and the report says so under (C) when a
cluster's symptoms are unclassified.

### 3.5 Mitigation set up, not only described

(D) is what separates a retrospective from a post-mortem nobody reads. Two mechanisms, both
automatic and both reversible:

- **Guards.** `guards.json` beside the ledger holds one entry per classified failure and one per
  risk shape found on a clean cluster, each marked `failure` or `risk`: cluster, entry, object
  (`namespace/kind/name`), evidence, first and last seen. The collector merges it on
  every run (new, seen again, gone when the cluster is reviewed or re-checked and the symptom or
  shape is absent, and dropped with a cluster that left the fleet). The daily readiness watch, once
  it ships, reads the file through its own sandbox hop and adds a line per live guard to its next
  report for that cluster, so the operator planning the next upgrade sees "the last upgrade held
  the drain on `seeded-upgrade/pinned-batch-runner`'s budget; it still allows no disruption".
- **The stream's ledger issue.** Where the install has a repository, the retrospective is a
  fleet-audit stream and keeps what every stream keeps: exactly one open issue, rewritten in full
  on every run, with each finding identified by cluster and object and closed by a run that finds
  nothing. That one issue is the (C) checklist for the whole fleet; a per-cluster issue would need a
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
there. Two triggers write the same files, so one run holds an exclusive lock on `.lock` under the
root for its duration and a second run, scheduled or on demand, prints one line and exits without
writing (a dry run reads without the lock). Reports are named by their finish time in UTC, full
runs as `reports/<timestamp>.md` and scoped runs as `reports/<timestamp>-scoped.md`, so two runs on
one day never replace each other; each ring is pruned to the newest fourteen, the retention the
fleet-audit report store uses, and only a full run moves the latest link. Each file is written to a
temporary name and renamed,
in the order report, guards, ledger, so a run that dies leaves at most a report with no ledger
advance; a ledger that cannot be parsed is set aside under a dated name and the run stops with that
as its line. While a dated crash record sits beside no ledger, every run refuses to start from empty
and repeats that line, so the record is read rather than overwritten; `--reset-ledger`, run by an
operator on purpose, archives the record and lets the next run start as a first run. A run starts
from an empty ledger on its own only when neither a ledger nor a crash record exists. The readiness watch runs on the gateway pod and reaches the file the same way it reaches
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
   (`0 18 * * 0`, `skills: ["fleet-audit"]`, the `AUDITS` allowlist so findings file, with check
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

- **Unit.** Each classifier signature against one captured fixture; selection (new, upgraded by
  version, upgraded by operation, unchanged, forced); the ledger and guards round trips; an
  unreachable cluster recorded without failing the run; the report rendering, including an empty
  section and the severity of each fixture incident.
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
  an event.
- Classification is by signature and can be wrong; every finding carries its evidence and a
  confidence, and the SOP may downgrade one. A wrong guard costs one extra line in a readiness
  report until the next run re-checks the cluster and drops it.
- Fourteen days for the first run, and a fixed Sunday evening for the fleet-wide run (the
  after-upgrade watch covers the slot after each window) rather than a slot after each
  install's maintenance window, are starting values.
- Audit-log reads (eviction 429s, admission rejections) would sharpen (B); `gcloud logging read` is
  on the allowlist, cost and scope to decide.
- Whether a live guard should turn the readiness verdict for that cluster to `blocked`, or only
  annotate it, is the readiness watch's decision once it reads the file.
