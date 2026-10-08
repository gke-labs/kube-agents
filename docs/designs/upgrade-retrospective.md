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

1. **Run on its own.** Once when the assistant is first installed, so a new install starts with a
   review of every cluster it finds, and then every weekend. Nobody has to ask.
2. **Look only at what changed.** A cluster is reviewed when it is new to the assistant or when it
   was upgraded since the last review. A cluster nothing happened to gets one line saying so.
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
     and it is still here" while it is. And where the install has a GitHub repository, one tracked
     issue per reviewed cluster carries the checklist of fixes and is updated by the next review
     rather than duplicated.

   An incident is an **Error** when the match is sure and the object is one of the user's, or when
   Google reported the upgrade itself as failed, or a computer stayed broken after its rebuild. It
   is a **Warning** when the match is tentative, when the object belongs to the cluster's own
   plumbing, when the symptom matches none of the twenty, or when a failure from an earlier review
   is still present with nothing new. **Info** lists each cluster that was upgraded or found with
   nothing wrong, the clusters nothing happened to, and anything the review could not read.

4. **Keep the report where the install keeps its records,** on the assistant's own storage, with
   the latest one always at the same path, and post one line per reviewed cluster in chat with the
   counts and the most important finding. A quiet weekend posts nothing.
5. **Be testable on the failures we already know how to produce.** The test fleet carries planted
   examples of the catalogue's failures and one cluster that was upgraded with defects planted; the
   first report over that fleet has to classify those correctly, and every one of them becomes a
   nightly test.

What it does not do: it does not upgrade, roll back, or change anything on a cluster. Opening a
pull request that fixes a manifest stays a user's "apply", through the same route the assistant
uses for every other proposed change.

## 1. Why

The [readiness checks](upgrade-readiness-checks.md) end with "a diff after the upgrade", and the
[catalogue](upgrade-failure-catalogue.md) says post-upgrade detection is one mechanism for every
entry: watch the operation, then compare pod health against the same measurements taken before.
Neither is built. The reproductions behind the catalogue measured why it matters: on a surge
upgrade GKE force-killed a pod its budget protected and reported the operation `DONE` twenty
minutes before the pod was Ready again, a 24-minute outage recorded as success. The symptoms of the
twenty failures sit in pod status, node conditions and `Warning` events that nothing scheduled
reads, and what one cluster's upgrade broke never reaches the next cluster's readiness report.

Two things already on the roster shape this design. The fleet audits pair a deterministic
collector (`collect.py`, `patch_readiness.py`) with a governance SOP the Platform Agent follows,
which is the split used here. The daily readiness watch (`upgrade-readiness-watch`) produces the
before-the-upgrade report and keeps its state on the profile volume, which is where this design's
(D) lands its guards.

## 2. Target model

```
upgrade-retrospective (Platform Agent roster: Saturday 09:00 UTC; and once, from the
                       Chat Agent's first-run stage after the inventory scan settles)
  └─ collector  upgrade_retrospective.py                          deterministic, no model
       ├─ ledger: versions per cluster, last run                   <agent home>/upgrade-retrospective/ledger.json
       ├─ select: new (not in ledger) or upgraded (version differs, or an
       │          UPGRADE_MASTER / UPGRADE_NODES operation since the last run)
       ├─ (A) operations in the window, versions before and after
       ├─ (B) symptoms read from the cluster, classified against the catalogue
       ├─ (C) detect-and-mitigate rows from the catalogue table, per finding
       ├─ (D) guards.json merged: new, seen again, gone
       └─ report.json + the Markdown report (A, B, C, D per cluster)
  └─ Platform Agent, following governance/upgrade_retrospective_sop.md
       ├─ reads the collector's output, tailors (C) to the cluster's objects
       ├─ (D) one ledger issue per reviewed cluster where a repository is linked
       └─ posts one line per cluster to chat; names the file on the gateway pod
  └─ upgrade-readiness-watch (daily)
       └─ reads guards.json and prints each live guard in its next report
```

## 3. Decisions

### 3.1 Trigger: first run and weekends, not a fixed day of the month

A retrospective is only useful soon after the upgrades it reviews. GKE's automatic upgrades land
inside maintenance windows that most fleets set at night or at weekends, so a Saturday-morning run
reviews the week's upgrades while their events are still in the cluster (events expire after an
hour by default on GKE, so (B) leans on pod and node state and uses events as corroboration, not
as the only source). The first run comes from the Chat Agent's first-run stage, the same place the
four fleet audits start once the onboarding scan settles, so a fresh install's first report is a
baseline of every cluster it manages rather than a blank.

### 3.2 Scope: new or upgraded since the last run

The ledger holds, per cluster, the control-plane version, every node pool's version, and the time
of the last run. A cluster is _new_ when absent, _upgraded_ when a version differs or when
`gcloud container operations list` shows an `UPGRADE_MASTER` or `UPGRADE_NODES` operation targeting
it that started after the last run. The first run has no ledger and reviews the last fourteen days
of operations. Projects resolve as the readiness watch resolves them: an explicit list, otherwise the
management project and every project a Cluster Agent profile's identity names.

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
| `OOMKilled` on a cgroup v2 pool, single-container pod                                     | 14    |
| `OOMKilled` where the container runs several processes                                    | 15    |
| `ImagePullBackOff` / `ErrImagePull`, on rebuilt nodes only                                | 20    |
| `PersistentVolume's node affinity`, `FailedAttachVolume`, `FailedMount`                   | 19    |
| `nvidia.com/gpu` in a scheduling message; `nvidia`, `CUDA`, `Error 803` in a container    | 18    |
| nodes `NotReady` / `NetworkUnavailable` after a node-pool operation                       | 17    |
| `Insufficient cpu` / `memory` on a Pending pod after a node-pool operation                | 2     |
| a budget with no allowance left on a drained node; a node operation past an hour per node | 1     |
| `no matches for kind`; a Job or CronJob pod in `Error` whose spec names a removed API     | 6     |

A symptom that matches nothing is reported as a Warning, unclassified, rather than dropped: the
report is a record of the upgrade, not only of the catalogue's part of it. The entries not in the table (3, 4,
5, 8, 9, 10, 11, 13, 16) have no symptom a single read identifies with confidence; they are the
ones the readiness checks have to catch before the upgrade, and the report says so under (C) when a
cluster's symptoms are unclassified.

### 3.5 Mitigation set up, not only described

(D) is what separates a retrospective from a post-mortem nobody reads. Two mechanisms, both
automatic and both reversible:

- **Guards.** `guards.json` beside the ledger holds one entry per classified failure: cluster,
  entry, object (`namespace/kind/name`), evidence, first and last seen. The collector merges it on
  every run (new, seen again, gone when the cluster is reviewed and the symptom is absent). The
  daily readiness watch reads the file and adds a line per live guard to its next report for that
  cluster, so the operator planning the next upgrade sees "the last upgrade broke
  `seeded-shapes/legacy-registry-pull` this way; it is still there".
- **A ledger issue.** Where the install has a repository, the SOP files one issue per reviewed
  cluster with the (C) checklist, labelled for the stream, rewritten in place on the next review
  and closed when every guard is gone, the way the fleet audits keep one ledger issue per stream.

Opening a pull request for a manifest change is not (D): the report names the file and the change,
and the user's "apply" goes through `submit-suggestion`.

### 3.6 Where the report lives

Under the Platform Agent profile's data directory, `upgrade-retrospective/reports/<date>.md`, with
`upgrade-retro-report.md` beside it pointing at the latest, the same report as `.json`, the ledger
and `guards.json`. The directory is on the data volume, survives a pod restart, and needs no
repository. The agent's own tools run in the sandbox and cannot open that path, so the chat line
carries the counts and the top finding rather than only the path, and the SOP reads the collector's
output from the sandbox-side copy the run produces.

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
  2026-10-07 18:27 to 18:34 (6m54s), DONE; UPGRADE_NODES default-pool, 18:34 to 18:38 (3m43s), DONE.
What failed: drain held, disruptionsAllowed 0 on one replica, catalogue entry 1 (a
  PodDisruptionBudget forbids the eviction), high; evidence: maxUnavailable 0, 1 of 1 pods on the
  drained pool.
Detect and mitigate next time: visible before the upgrade as the budget itself; read today by the
  daily obtainability audit (blocking-pdb) and the readiness report. Before the next upgrade: allow
  one disruption or add a replica. Now: the pod is back; nothing to repair.
Mitigation set up: guard seeded-b / entry 1 / seeded-upgrade/PodDisruptionBudget/pinned-batch-runner
  (first seen 2026-10-11), reported by the readiness watch until the budget has room; ledger issue
  <url> where a repository is linked.

## Warnings

### Entry 6 on gemma-gpu-upgraded: kubeagents-system/CronJob/legacy-flowcontrol-tuner
What happened: no upgrade operation in the window; control plane 1.32.13, default-pool 1.31.14.
What failed: 6 of 6 Job pods in Error, catalogue entry 6 (a served API version is removed),
  medium; evidence: the spec names flowcontrol, a name rather than an observed API call.
Detect and mitigate next time: ...
Mitigation set up: guard ...

### Unclassified on platform-agent-host: kubeagents-system/Deployment/platform-agent-gateway
What failed: Unhealthy probe events, 31 in the window, matching none of the twenty.

## Info

- seeded-c: UPGRADE_MASTER 2026-10-06 03:27 (8m), UPGRADE_NODES default-pool 2026-10-07 03:26 (5m);
  no failure found.
- seeded-a: new (first seen), 1.35.8-gke.1380001; no failure found.
- Unchanged: gemma-gpu (no operation since 2026-09-24).
- Reads that failed: none.
```

The example's facts are from the test fleet; a real run's content comes from the collector's JSON,
which keeps the per-cluster data and the same three-way grouping.

## 5. Work breakdown

1. **The collector.** `agents/platform/skills/fleet-audit/scripts/upgrade_retrospective.py`: the
   ledger, selection, (A), (B), the signature table, the catalogue table for (C), the guards merge,
   JSON and Markdown output, `--dry-run`, `--since`, `--cluster`; unit tests on fixtures captured
   from the test fleet.
2. **The job.** `agents/platform/governance/upgrade_retrospective_sop.md`; the roster entry
   (`0 9 * * 6`, `skills: ["fleet-audit"]`, the `AUDITS` allowlist so findings file); the first-run
   hook in the Chat Agent's first-run stage; the readiness watch reading `guards.json`; the cron
   README section and the generated cron reference.
3. **The proof.** The first report over the test fleet; one nightly case per catalogue entry the
   report must classify, starting with the entries that have no passing record yet; the catalogue's
   status table gains the retrospective as the after-the-fact reader.

## 6. Files touched

- `agents/platform/skills/fleet-audit/scripts/upgrade_retrospective.py`, its test and `testdata/`.
- `agents/platform/governance/upgrade_retrospective_sop.md`.
- `agents/platform/cron/jobs.json`, `agents/platform/cron/README.md`,
  `agents/platform/skills/fleet-audit/scripts/audit_report.py` (`AUDITS`).
- The Chat Agent's first-run stage (the list of audits it starts after the inventory scan).
- `agents/platform/scripts/upgrade_readiness_watch.py` (reads `guards.json`).
- `docs/site/src/content/docs/reference/cron-jobs.md` (generated), the autonomous-watchdogs and
  security reference pages where they enumerate the roster.

## 7. Testing

- **Unit.** Each classifier signature against one captured fixture; selection (new, upgraded by
  version, upgraded by operation, unchanged, forced); the ledger and guards round trips; an
  unreachable cluster recorded without failing the run; the report rendering, including an empty
  section and the severity of each fixture incident.
- **Eval.** One case per entry the first report classifies on the test fleet, graded on declared
  lines (`<cluster>/<object>: entry <n>`), red on `main` where the report does not exist, green
  three times on the branch; registered in the nightly roster. The out-of-the-box path (collector
  alone, no model) is a test, not an eval.
- **Live.** The first run on a dev install: the first-run stage producing the baseline report; a
  weekend tick after an upgrade producing a cluster section with all four parts; a guard appearing
  in the next readiness report; the ledger issue where a repository is linked.

## 8. Accepted risks and open questions

- Events expire; a Saturday run sees the week's pod and node state but may miss transient events.
  The ledger's version diff still finds the upgrade; (B) says when its evidence is state rather than
  an event.
- Classification is by signature and can be wrong; every finding carries its evidence and a
  confidence, and the SOP may downgrade one. A wrong guard costs one extra line in a readiness
  report until the next retrospective drops it.
- Fourteen days for the first run, and a fixed Saturday rather than a slot after each install's
  maintenance window, are starting values.
- Audit-log reads (eviction 429s, admission rejections) would sharpen (B); `gcloud logging read` is
  on the allowlist, cost and scope to decide.
- Whether a live guard should turn the readiness verdict for that cluster to `blocked`, or only
  annotate it, is the readiness watch's decision once it reads the file.
