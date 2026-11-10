# Post-merge verification: did the merged fix work?

**Status:** proposed, 2026-11-06. Part of #2000 and the autonomy epic #4250. Builds on
[`proposal-outcome-readings.md`](proposal-outcome-readings.md) (the broker's proposal sweep),
[`proposal-origin.md`](proposal-origin.md) (the claim and the `proposals_opened` ledger),
`mergeCommit` (#4933), the reconciler reader `reconciler_sync.py` (#5041) and the single-finding
re-check `recheck.py` (#5353).

## 1. The problem

A merged fix is not a working fix. Today nothing checks, after a merge, that the reconciler applied
the change, that the workload stayed healthy, or that the finding went away. The next fleet audit
re-derives the finding at its daily or weekly cadence, so it can judge a fix that is not applied
yet, and it never says a fix worked ([`fleet-audit-issue-ledger.md`](fleet-audit-issue-ledger.md)).
On 2026-11-05 a dev install told an MCP caller that Argo CD had deleted a merged PodDisruptionBudget;
Argo was still at the previous commit.

The first fix type is the missing PDB (`no-pdb`); the ComputeClass node selector
(`untargeted-compute-class-workload`) follows once the audit writes that fix in code. Reconcilers
are Argo CD `Application`, Flux `Kustomization` and Config Sync `RootSync`/`RepoSync`. kube-agents
never merges, approves or reopens a pull request. A later milestone offers auto-merge after several
clean merges, counting this check, so the outcome must be deterministic and hard to forge.

## 2. What it does

1. Record at open. The `pr_opened` hook, which already claims each proposal into the
   `proposals_opened` ledger, writes a verification record to a new Session KV table keyed by the
   proposal URL: repository, number, origin, `opened_at`, and the originating thread's address. It
   stores the address rather than the session id because session metadata expires after 14 idle
   days. Only a claimed proposal gets a record.
2. Detect the merge. No new poller: the broker's readings sweep already lists every proposal's
   state every 240 s (`vcs_readings.py`). The broker keeps that listing and serves it on a new read
   verb, `proposal-states`. On the first tick that sees `merged`, one `proposal-view` fetches
   `mergeCommit`, `sourceRevision` and the merge time. A proposal closed unmerged ends silently.
3. Wait until the GitOps tool has applied the merge commit. #5041's `read_sync` reads the
   reconciler object and answers `applied`, `pending`, `unconfirmed`, `error` or `unknown`. Since
   reconcilers track a branch, a new broker verb backed by each forge's compare API answers whether
   the applied revision contains the merge. `error` counts only after 15 minutes, because an error
   from the previous commit can linger.
4. Health window. At `applied` the driver records each pod's `restartCount` and Ready, waits the
   type's window, then requires a complete rollout, every pod Ready, no new restart and no
   `OOMKilled`, plus the health audit's Prometheus restart query over the window where
   Managed Prometheus is available.
5. Re-check the one finding (#5353). `recheck.py` runs the collector's own check against the target
   only, in the caller's process; only the `kubectl get` of the target namespace goes through the
   sandbox. The read names the finding's own cluster context; a context naming another cluster, or a
   cluster outside the install's declared scope, is refused before any read. `blocking-pdb` and `pdb-overlapping` are narrowed to
   the budgets covering the target's pods, so an unrelated budget cannot fail a correct fix.
6. Report once, as a harness comment on the merged pull request and one line in the originating
   thread. A scheduled audit run has no thread: "verified fixed" goes on the pull request only, the
   other outcomes to the job's chat delivery.

The driver fills the record's subject (finding id, target, reconciler) on its first tick, from the
body's `audit-findings` block and the generated manifest's header. It accepts the subject only if
re-rendering the fix from the live workload reproduces the pull request's changed files byte for
byte; otherwise the record is dropped without a report. A forged pull request can therefore name
only a fix the harness would itself have written. The reconciler comes from the workload's ownership
markers, looked up on the target cluster and then the management cluster, and counts only if its
source is this repository and its path covers a changed file. With no such reconciler, the driver
observes the object directly and the report says so.

## 3. Outcomes

| Outcome                 | When                                                         | The comment says                                                      |
| ----------------------- | ------------------------------------------------------------ | --------------------------------------------------------------------- |
| Verified fixed          | applied, window passed, re-check clean                       | which reconciler applied which commit and when, readings, re-check    |
| Still broken            | applied, and the re-check reproduces or the window failed    | the same, plus the failing evidence                                   |
| Applied, not re-checked | applied, window passed, re-check could not run               | as fixed, without the re-check, and that nothing was verified         |
| Not applied by deadline | never applied, error persisted, or merge commit unidentified | the reading's state and revision, and that the cluster was not judged |

The audit and the driver must not both say a fix failed. A "still broken" comment for a reproducing
finding carries the audit's `audit-persists:<id>` marker, which both check before posting, and the
audit holds its own notice while a merge is younger than the type's deadline plus window. Saying a
fix worked reverses the ledger design's silence on `resolved-merged`; its §4 changes, and the exact
wording is settled, in the pull request that ships the report.

## 4. Per-fix settings

The deadline counts from the merge, the window from `applied`. Both are settings.

| Fix                        | Deadline | Health window                                                                      | Re-check                                                     |
| -------------------------- | -------- | ---------------------------------------------------------------------------------- | ------------------------------------------------------------ |
| Missing PDB                | 60 min   | 5 min; budget at current generation, all pods healthy, `disruptionsAllowed >= 1`   | `no-pdb`, plus narrowed `blocking-pdb` and `pdb-overlapping` |
| ComputeClass node selector | 60 min   | rollout within 30 min, then 15 min with no restart or OOMKill, pods on class nodes | `untargeted-compute-class-workload`                          |

Argo polls every three minutes by default and Config Sync every fifteen seconds, so an hour means a
stuck sync. A pod restarting on its own briefly makes `disruptionsAllowed` 0, so a failed budget
reading is read again after a second window before it is reported.

## 5. Where it runs

The driver is `post_merge_check.py`, a `no_agent` cron entry every 5 minutes with `deliver: chat`
and `risk: high`, on the `stall-watch` pattern (`agents/platform/cron/jobs.json`). Each tick it
advances each open record one step and posts what is due. It uses no model: waiting is most of the
work, every decision has a deterministic answer, the 11-05 misreport was a model reading the wrong
revision, and auto-merge needs the outcome to read the same every time.

Records live in Session KV, on the pod's volume, written over a loopback API with a key the sandbox
does not hold. The audit's report store was rejected because the sandbox writes it, the broker's
cache because it is in memory. Records skip the 14-day idle cleanup and are pruned 30 days after
their last change. No kanban card id is stored (#3654).

Fleet clusters are read as the pod's Google service account; `roles/container.viewer` covers the
reconciler kinds, and a `custom` permission set needs `container.thirdPartyObjects.get` and `.list`.
The job writes only comments, and gets a paragraph in the site's security reference.

## 6. Failure modes and testing

- A reconciler that never applies the merge gets "not applied" at the deadline, and a later sync
  gets no second comment (open question 3). So does an applied revision whose ancestry the forge
  cannot answer.
- Merge commits: Bitbucket Cloud's 12-character id is expanded with one commit read, GitLab's
  `null` after a fast-forward uses `sourceRevision`, and Bitbucket Data Center's `null` is reported
  as unidentified.
- With the sweep off (`CREDENTIAL_PROXY_VCS_READINGS=off`) or no `proposal-states` verb, the driver
  makes one `proposal-view` per waiting record per tick, at most 20.
- A pod restart loses nothing: the window resumes from its persisted end time and baseline. A crash
  after posting is caught by the comment's hidden `post-merge-check:<outcome>` marker, so the worst
  case is one missing chat line. The model posts with the same credential and could pre-place that
  marker; the audit's marker has the same residual today.
- Comments carry fixed text, object names, SHAs, counts and condition reasons, never pod logs,
  event messages or reconciler condition messages, which namespace writers can influence.

Unit tests cover each `read_sync` state, each record transition under a fake clock, the crash
recovery, the shared marker in both orders, the generator comparison and each merge-commit shape.
A bench case runs the driver after the GitOps fix cycle ([`gitops-fix-cycle.md`](gitops-fix-cycle.md))
in three variants: fixed, still broken, and not applied. Live, a planted PDB fix merged under Argo CD
and under Config Sync should each get one "verified fixed".

## 7. Out of scope and later

- A revert pull request on "still broken", in the epic's second milestone; until then the outcome
  is report-only.
- Other fix types, and `submit-suggestion` pull requests, which carry no structured finding.
- Flux `HelmRelease`, whose applied revision is a chart version, and Terraform or Config Connector
  fixes, which have no in-cluster revision.
- An Argo CD hub on a third cluster, or Argo tracking by label; both fall back to observing the
  object.

## 8. Open questions

1. The 11-05 fix came from `submit-suggestion`. Should chat requests for a missing PDB use the
   audit's generator, or should `submit-suggestion` record a subject?
2. Should the revert ship in the first milestone, and is it worth it for a harmless miss such as a
   PDB whose selector matches nothing?
3. When a fix applies after "not applied" was posted, stay silent or post one late comment?
4. Are the deadline and window defaults right? Live runs and the first pilot will say.
5. Will the broker's owner accept `proposal-states`, which reverses the readings design's "no
   answer depends on the cache" invariant? If not, the `proposal-view` fallback is the only path.
6. Should a scheduled audit's "verified fixed" also go to the home channel? Proposed: no.
7. Once kanban cards are gone (#3654), what does the origin resolver read a worker's session from?
