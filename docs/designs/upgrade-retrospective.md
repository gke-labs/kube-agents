# Upgrade retrospective: what the last upgrade did, and the fix set up before the next one

**Status:** design. Nothing here is built on `main`. The collector it specifies is the first of
three changes; the schedule and the agent's part follow it. The
[upgrade failure catalogue](upgrade-failure-catalogue.md) is the list of failures it classifies
against, and the [readiness checks](upgrade-readiness-checks.md) are the before-the-upgrade half it
feeds.

## Requirements, in plain English

A cluster is a group of rented computers that run an application in small pieces. Google upgrades
those computers on a schedule: it replaces the control program with a newer edition and rebuilds
every computer one at a time. Most of the time nothing breaks. Sometimes a piece of the application
does not come back, and nobody notices until someone complains, because Google reports the upgrade
as finished whether or not the application survived it. Today the assistant can tell you what
_would_ break before an upgrade, when you ask. Nothing looks back afterwards and writes down what
_did_ break, so the same surprise waits for the next cluster.

This feature is a scheduled review called the **upgrade retrospective**. It must:

1. **Run on its own, and answer when asked.** Once when the assistant is first installed, so a new
   install starts with a review of every cluster it finds, and then at the end of every weekend;
   nobody has to ask. And anyone who does ask, in their own words, gets the same report: "how did
   the last upgrades go", "did anything break when the clusters were upgraded", "any upgrade
   problems I should know about", "is there anything to fix before the next one". The question does
   not have to name the feature, a cluster or a version. The assistant answers from the latest
   saved report when nothing has been upgraded since, and runs the review first when something
   has.
2. **Look only at what changed.** A cluster is reviewed when it is new to the assistant or when it
   was upgraded since the last review. A cluster nothing happened to gets one line saying so, with
   one exception: a cluster that still carries a finding from an earlier review is re-checked for
   that finding every time, so a fix made between upgrades clears it without waiting for the next
   upgrade.
3. **Write a report, the `upgrade-retro-report`,** in three sections, **Errors**, **Warnings**
   and **Info**, any of which may be empty. An entry under Errors or Warnings is one incident:
   one application object on one cluster, with four parts:
   - **(A) What happened.** Which upgrades ran on that cluster, on which part of it, from which
     version to which, when they started and finished, how long they took, and whether Google
     reported them as finished or failed.
   - **(B) What failed.** How the object was down, stuck or restarting after the upgrade, which of
     the twenty known upgrade failures it matches, how sure the match is, and the evidence.
   - **(C) How to see it coming and how to fix it next time.** The sign that was visible before the
     upgrade, whether anything the assistant already runs reads that sign, and the fix, both the
     one to make before the next upgrade and the one that repairs the cluster now. Written about
     this object, not in general terms.
   - **(D) The fix, set up.** Two things happen without being asked. The daily readiness check
     learns about the failure, so the next pre-upgrade report says "the last upgrade broke this,
     and it is still here" while it is. And where the install has a GitHub repository, the
     review's one tracked issue carries the checklist of fixes, one entry per cluster and object,
     rewritten by the next review rather than duplicated and closed when a review finds nothing.

   An incident is an **Error** when the match is sure and the object is one of the user's, or when
   Google reported the upgrade itself as failed, or a computer stayed broken after its rebuild. It
   is a **Warning** when the match is tentative, when the object belongs to the cluster's own
   plumbing, when the symptom matches none of the twenty, or when a failure from an earlier review
   is still present with nothing new. **Info** lists each cluster that was upgraded or found with
   nothing wrong, the clusters nothing happened to, and anything the review could not read.

   A clean cluster is not an empty entry. For a reviewed cluster with no failure, part (C) is a
   pre-flight for its next upgrade: the version its channel will move it to and when, and the known
   failure shapes present on the cluster today although nothing has failed yet (a protection rule
   with no allowance, a placement rule on a label the next version drops, an image from a retired
   download site, a disk attached the old way, an agent tied to the machine's network or runtime,
   data kept on the machine, a single copy behind a protection rule, a GPU program pinned to a
   driver version, a gatekeeper with nobody behind it), each with its fix. Part (D) records the
   baseline for the next review to compare against and marks each shape as a risk the readiness
   check names before the next upgrade. The lines for unchanged clusters say when each was last
   upgraded and what it will move to next; the lines for clusters the review could not read say
   why. "None" appears only when the count is really zero.

4. **Keep the report where the assistant can read it back,** on the storage its own tools use,
   with the latest one always at the same path, and post one line per reviewed cluster in chat
   with the counts and the most important finding. A quiet weekend posts nothing. A GitHub
   repository is not required for any of this; it only adds the tracked issue.
5. **Be testable on the failures we already know how to produce.** The test fleet carries planted
   examples of the catalogue's failures; the first report over that fleet has to classify those
   correctly, and every one of them becomes a nightly test.

What it does not do: it does not upgrade, roll back, or change anything on a cluster. Opening a
pull request that fixes a manifest stays a user's "apply", through the same route the assistant
uses for every other proposed change.

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
upgrade-retrospective (Platform Agent roster: Sunday 18:00 UTC; and once, from the
                       Chat Agent's first-run stage after the inventory scan settles)
  └─ collector  upgrade_retrospective.py                          deterministic, no model
       ├─ ledger: versions per cluster, last run                   /opt/data/upgrade-retrospective/ledger.json (the shell's volume)
       ├─ select: new (not in ledger) or upgraded (version differs, or an
       │          UPGRADE_MASTER / UPGRADE_NODES operation since the last run)
       ├─ (A) operations in the window, versions before and after
       ├─ (B) symptoms read from the cluster, classified against the catalogue
       ├─ (C) detect-and-mitigate rows from the catalogue table, per finding
       ├─ (D) guards.json merged: new, seen again, gone
       └─ report.json + the Markdown report (A, B, C, D per cluster)
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

### 3.1 Trigger: first run and the end of each weekend, not a fixed day of the month

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
and the report on the volume is the record, so the SOP runs the collector first and calls `start`
and `finish` only when a repository is configured and the run is full, and the first-run stage marks this job due
without a repository. That is the "chat-only mode" the first-run design lists as an open question,
scoped to this one stream. An install that onboarded before the job existed gets its baseline from
the first Sunday tick.

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
symptom carries its own onset, the earliest evidence the objects hold. A pod is the wrong place to
read it when the pod is owned: a node-pool upgrade drains every node, every pod on it is deleted
and recreated, and the replacement's creation, start and `Ready=False` transition all fall inside
the window by construction. So an owned pod's onset is read at the owner first, and only from evidence that says something
failed: a Deployment's `Available=False` or `Progressing=False` condition transition when one of
them is `False`, or the current ReplicaSet's creation when it falls inside the window (a rollout
during the upgrade is when a ReplicaSet dates a failure; an older ReplicaSet dates nothing). A
Deployment whose conditions never flipped, which a partial failure under its `maxUnavailable`
leaves `Available=True`, and whose ReplicaSet predates the window has no owner evidence, so its pod
is read like any other. Where the owner carries no dated condition (StatefulSet, DaemonSet,
Job) or the pod is bare, the pod's own evidence is used: a Pending pod's start, a crash-looping or
not-ready pod's `Ready=False` transition time (falling back to its start; a container's last
termination is the latest crash, not the first, and is never the onset), a node condition's
transition, an event's first observation. A pod-sourced onset on a pod created after the drain of
its node began is marked as such, and on a first run (no stored set) a symptom whose only onset is
that one is graded `medium`: a Warning with the reason "the pod was recreated by the upgrade; the
failure may predate it", never an Error; the next full run, keyed by owner, settles it. A symptom whose onset is
earlier than the first operation of the cluster's upgrade window, or that the previous full run
already recorded, is graded a Warning with the reason "predates the upgrade", never an Error; a
symptom with no readable onset falls back to the stored set alone. The first run has no stored set
and grades by onset only, which it says. A cluster is _new_ when absent, _upgraded_ when a version differs or when
`gcloud container operations list` shows an `UPGRADE_MASTER` or `UPGRADE_NODES` operation targeting
it that reached a terminal status (`DONE`, `ABORTING`, an error) with an end time after the last
run, and _re-checked_ when it is unchanged but holds a live guard:
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
that project's gap rather than the sweep's. The findings document the SOP hands to `finish` lists
every ledger-known cluster of that project under `scope.skipped` with the listing error as the
reason, the harness's own mechanism for a cluster a run could not read: the stream's previous
findings on those clusters are held rather than resolved, the ledger issue stays open, and nothing
is posted as fixed because a project went unread. Every run without `--full` is _scoped_, whatever its
`--project` set: a run narrowed by `--cluster`, by the question that invoked it, or any hand run,
with or without `--project`. A scoped run reviews only its targets, never
prunes a ledger entry or a guard outside them, never adds a project to the fleet set (a cluster it
reviews outside the fleet is reported and marked "outside the fleet; not recorded"), writes its
report under a `-scoped` name, does not move the latest link, and never calls the fleet-audit
`start` or `finish`, so a question about one clean cluster cannot close the fleet's ledger issue.
`--since` is a hand-run flag that widens the window a scoped run reviews; it never replaces the
ledger's last-run time. A cluster with an operation still `RUNNING` is not reviewed: a drain in progress shows a
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

A match is _sure_ (`high`) when the signature names the entry's own mechanism and the pool the
object sits on had an operation in the window: a webhook named in the rejection, a selector that
names a dropped label, a runtime image below the catalogue's floor on a pool migrated to cgroup v2,
an image that pulls on untouched nodes and fails on rebuilt ones, a volume attach error naming a
PersistentVolume, a driver error text, a budget with no allowance on a drained node. On a first run a match whose only onset is a recreated pod's is capped at `medium` (§3.2). Anything less,
a generic `OOMKilled`, an image pull failure with no untouched node to compare, a Pending pod with
no pool operation, is _tentative_ (`medium`). A symptom that matches nothing is reported as a
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
it). Both halves together are the guard, and the SOP states them as rules rather than leaving the
grade to the author's judgement; the report names the file and the change a user's "apply" would
make.

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
   JSON and Markdown output, `--dry-run`, `--full`, `--cluster`, `--since` and `--reset-ledger` as §3.2 and
   §3.6 define them; unit tests on fixtures captured
   from the test fleet.
2. **The job and the on-demand route.** `agents/platform/governance/upgrade_retrospective_sop.md`
   (including the freshness rule for a question); the routing lines in `CAPABILITIES.md` and the
   platform `AGENTS.md`; the roster entry
   (`0 18 * * 0`, `skills: ["fleet-audit"]`, the `AUDITS` allowlist so findings file, with check
   ids off `MAJOR_SWEEP_CHECKS`); the SOP's no-repository path (collector first, `start`/`finish`
   only with a repository) and the first-run stage marking this job due without one; the readiness
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
  `agents/platform/skills/fleet-audit/scripts/audit_report.py` (`AUDITS`).
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
- Fourteen days for the first run, and a fixed Sunday evening rather than a slot after each
  install's maintenance window, are starting values.
- Audit-log reads (eviction 429s, admission rejections) would sharpen (B); `gcloud logging read` is
  on the allowlist, cost and scope to decide.
- Whether a live guard should turn the readiness verdict for that cluster to `blocked`, or only
  annotate it, is the readiness watch's decision once it reads the file.
