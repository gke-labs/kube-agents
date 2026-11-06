# Post-merge verification: did the merged fix work?

**Status:** proposed, 2026-11-06. Part of gke-labs/kube-agents#2000 (read Argo CD and Flux status
to confirm fixes are live) and milestone 1 of the autonomy epic gke-labs/kube-agents#4250. Builds
on [`proposal-outcome-readings.md`](proposal-outcome-readings.md) (the broker's 240 s proposal
sweep), [`proposal-origin.md`](proposal-origin.md) (the claim, the `proposals_opened` ledger and
the origin of each proposal), `mergeCommit` on the proposal record (#4933, merged) and
`reconciler_sync.py` (#5041, open). Citations are `path:line` on `main` at `6d2b11e3e`.

## 1. The problem

kube-agents opens a fix as a pull request and forgets it. After the merge nothing checks that the
reconciler applied the change, that the workload stayed healthy, or that the finding went away.
The one existing re-check is the next scheduled fleet audit: `derive_finding_state` marks a merged
finding that still reproduces `pr-merged-persists` and comments once on the merged pull request
(`agents/platform/skills/fleet-audit/scripts/audit_report.py:10970-10996`, `:16880-16905`). It runs
at the stream's cadence, daily or weekly, with no regard for when the merge happened, so it can
judge a fix the reconciler has not applied yet. A fix that worked is `resolved-merged`, which by
design is never said (`docs/designs/fleet-audit-issue-ledger.md:391`,
`agents/platform/skills/fleet-audit/references/ledger.md:36-37`).

On 2026-11-05 a dev install was asked, over MCP, whether a merged PodDisruptionBudget fix had
worked. It said Argo CD had deleted the budget. Argo had not yet synced the merge: it was still at
the previous commit. The calling agent caught the error only by comparing Argo's synced revision
with the merge commit. Asked again after the sync, kube-agents answered correctly: synced at the
merge commit, budget live, two disruptions allowed. The check this design adds makes that
comparison before saying anything, and reports without being asked.

## 2. Scope

Milestone 1 covers fixes the fleet audit writes in code, starting with the missing PDB (`no-pdb`,
`agents/platform/skills/fleet-audit/scripts/collect.py:7362`; generator `_pdb_manifest`,
`audit_report.py:17203`) and then the ComputeClass node selector
(`untargeted-compute-class-workload`, `collect.py:4178`, `:7576`). The reconcilers are Argo CD
`Application`, Flux `Kustomization` and Config Sync `RootSync`/`RepoSync`, which the operator
already lets the agent read (`k8s-operator/internal/controller/platformagent_manifests.go:6089-6119`).

Out of scope: Flux `HelmRelease`, which that grant leaves out on purpose (`:6094`) and which would
need a new read permission; fixes delivered by Terraform or Config Connector, which have no
in-cluster revision to compare; pull requests from `submit-suggestion`, which carry no structured
finding (open question 1); and merging anything. kube-agents never merges, approves or reopens a
pull request.

## 3. The flow

### 3.1 Record at open

The `pr_opened` hook already claims every proposal the agent opens with the broker's one-time
token and writes it to the Session KV `proposals_opened` ledger with its origin
(`agents/platform/plugins/pr_opened/__init__.py:1-28`,
`agents/platform/scripts/session_kv_server.py:1827-1834`, `:8291-8312`). In the same step it writes
a verification record, keyed by the proposal URL like the ledger row: repository, number, origin,
the trusted session id the origin was resolved from (empty for a scheduled run), and `opened_at`.
Only a claimed proposal gets a record, so the model cannot create one by printing a line.

The subject is filled on the driver's first tick after the record appears, a few minutes after
the pull request opened, from the forge's copy of the body: the hidden `audit-findings` block and
the stream marker that `render_remediation_pr_body` writes (`audit_report.py:13825-13842`). The
next audit run already trusts that block to learn which findings a pull request was opened for.
The subject is the finding ids and the stream, and from the single-finding re-check (§3.5) each
finding's check id and target (cluster, namespace, kind, name). It also names the reconciler that
delivers the target, read from the target workload's ownership markers: Argo's
`argocd.argoproj.io/tracking-id`, Flux's `kustomize.toolkit.fluxcd.io/name` label, or Config
Sync's annotations (`agents/cluster/skills/gke-load-bearing-change/SKILL.md:32-34`). For a missing
PDB the budget does not exist yet, so the markers are read from the workload it covers. The
reconciler counts only when its source is the pull request's repository and its path covers a
changed file; otherwise the record has no reconciler and §3.3 falls back to observing the object.
The subject is read once and never refreshed, so a later edit to the body does not change what is
checked. A body with no `audit-findings` block gives a record with no subject, which is dropped.

The expected-after state is not stored as text. Each check id has its predicate in code (§4.2).

### 3.2 Detect the merge

No new poller. The broker's readings sweep already lists every proposal the install authored,
with its state, in every managed repository every 240 s (`agents/platform/scripts/vcs_readings.py:44`,
`proposal-outcome-readings.md` §5). The broker keeps that listing's `number → state` map per
repository after each complete walk, replacing it whole, and serves it on a new read verb,
`proposal-states`, which makes no forge call. The driver reads it once per tick. A listing is a
reading, so nothing is missed across a restart: a merged proposal stays merged, and until the
first sweep after a broker restart the verb answers "no reading" and the driver waits a tick.

The listing does not carry the merge commit on GitHub, because the search API's issue shape has
no `merge_commit_sha` (`agents/platform/scripts/providers/github/forge.py:731-752`,
`providers/github/translate.py:202-223`). So on the tick that first sees `merged`, the driver makes
one `proposal-view` for `mergeCommit` and the merge time, and stores both. A proposal closed
without a merge ends its record silently.

Two cases fall back to one `proposal-view` per waiting record per tick, capped per tick: an install
that turned the sweep off with `CREDENTIAL_PROXY_VCS_READINGS=off`, and a repository past the
sweep's walk bound, where an old proposal may be missing from the listing.

### 3.3 Wait until the reconciler has applied the merge

The driver reads the reconciler object through the sandbox and passes it with `mergeCommit` to
`read_sync(obj, merge_commit, is_ancestor)` from #5041, which answers `applied`, `error`, `pending`,
`unconfirmed` or `unknown`. Reconcilers track a branch, so the applied revision is often a later
commit. `is_ancestor` asks the forge whether that commit contains the merge, through a new broker
verb backed by each forge's compare or merge-base API, read with the existing contents permission.
A forge without such an API answers `None`, which reads `unconfirmed`. Bitbucket Cloud abbreviates
`mergeCommit` to 12 characters and is compared as a prefix (#4933).

`applied` moves on to the health window. `pending` and `unconfirmed` keep waiting. `error` must
persist for 15 minutes before it counts, because #5041 makes an error one reading and not a verdict
(an Argo `SyncError` from the previous commit can linger until the next reconcile). A record with no
reconciler observes the object instead: the new PDB exists with the merged spec, or the workload's
pod template carries the merged `nodeSelector`. The report then says it observed the object
directly. Any record still not applied when the type's deadline T passes (§5) is reported as
"not applied" (§3.6).

### 3.4 Health window

After `applied`, the driver holds a per-type window (§5) and reads the target at its start and
end: the rollout is complete (`observedGeneration`, `updatedReplicas == replicas ==
readyReplicas`), every pod is Ready, no container's `restartCount` rose, and no
`lastState.terminated.reason` is `OOMKilled`. A pod that comes up and then crashes reads healthy at
first, which is why the bench soaks after the sync (`bench/kube_agents_bench/gitops.py:44-49`).
Where Managed Prometheus is available, the window also runs the fleet health audit's restart and
availability expressions narrowed to the target, to catch a restart that recovered between the two
reads (`agents/platform/skills/fleet-audit/scripts/fleet_health.py:205-212`, `:236-263`). The PDB
fix restarts no pods, so its window reads only the budget's own status and the workload's Ready
count.

### 3.5 Re-check the one finding

The fleet audit re-derives one finding on demand: its check against its target only, run through
the sandbox from the image's copy of the script as `stall_watch.py` runs `stall_report.py`
(`agents/platform/scripts/stall_watch.py:56-70`). The answer is "reproduces" or not, with the
evidence excerpt the audit already renders, and `derive_finding_state` turns it into
`resolved-merged` or `pr-merged-persists`.

### 3.6 Report once

One outcome per record, posted as a harness comment on the merged pull request and as one line in
the originating thread:

| Outcome          | When                                                                    | The pull request comment says                                                             |
| ---------------- | ----------------------------------------------------------------------- | ----------------------------------------------------------------------------------------- |
| Verified fixed   | applied, window passed, finding no longer reproduces                    | which reconciler applied which commit and when, the window's readings, the clean re-check |
| Still broken     | applied, and the finding reproduces or the window failed                | the same, plus the re-check's evidence or the window reading that failed                  |
| Not applied by T | the reconciler never reached `applied`, or an error persisted, before T | the last reading's detail; it says the cluster was not judged                             |

The thread is found from the record's session id with `_lookup_platform_threads`
(`session_kv_server.py:5355`) and posted the way the event watcher's follow-up is
(`_post_followup`, `:7878`). A scheduled audit run has no thread. Its "verified fixed" goes on the
pull request only, and the other two outcomes go to the job's chat delivery. The exact wording is
decided in the row 8 pull request, because it is a new comment users will see.

The "still broken" comment carries the audit's `audit-persists:<id>` marker for each finding. The
daily audit reads that marker from harness comments only before posting its own persists notice
(`marker_from_harness`, `audit_report.py:11280`, called at `:16893-16899`), so the same news is never posted twice. The audit also stops
posting that notice while a merge is younger than its type's T plus window, which closes the same
race in the daily path that misled the 11-05 answer.

This reverses "a merged fix that worked is said nothing" for fixes this check covers. The audit's
own rendering stays silent. The confirmation comes from the driver, and
`fleet-audit-issue-ledger.md` §4 changes in the pull request that ships it.

### 3.7 Revert when still broken

On "still broken", the driver opens one revert pull request and nothing else. It never merges,
reopens or closes anything. The revert is made in a leased workspace
(`agents/platform/scripts/gitops_workspace.py`) from the target branch's head, reverting
`mergeCommit`. Its body links the original pull request and the evidence. The driver opens no
revert, and says why in the report, when the merge commit is not the whole change (after a rebase
or fast-forward merge `mergeCommit` is the branch tip, #4933) or when a later commit touched the
same files. It opens at most one per record. This is plan row 14 in milestone 2. Until it lands,
"still broken" is report-only. The end-state architecture describes the same corrective loop
(`docs/architecture/04-workflow-model.md:273-293`).

## 4. Where it lives and what runs it

### 4.1 The record goes in Session KV

The record is a new Session KV table beside `proposals_opened`, keyed by URL. The alternatives:

- **The fleet audit's report store** is written from the shell sandbox by the harness, where the
  model's own commands also run, and it serves the audit alone. A record there could be written by
  anything the sandbox runs.
- **The broker's sweep cache** is in memory and emptied by every restart, and the broker keeps no
  cross-request state by design (`proposal-outcome-readings.md` §1).

Session KV is on the pod's volume and survives restarts. It is written over a loopback API with
the pod's key, which the sandbox does not hold, by the hook that already writes the ledger from the
claim. The driver runs in the same pod. Like the ledger, the table is kept out of the 14-day
idle-session cleanup (`session_kv_server.py:1868-1873`) and has its own rule instead: a reported
record is pruned 30 days after its report, which outlasts the 7-day "not undone" window the
auto-merge offer counts. The
operator caps the gateway at one replica (`docs/site/src/content/docs/reference/security-and-iam.md:142`),
so there is one Session KV and one copy of each record.

The key is the proposal URL, with the origin and session id from `proposal-origin.md`. No kanban
card id goes in the record, because cards are being removed (gke-labs/kube-agents#3654).

### 4.2 A scheduled job with no model

The driver is `post_merge_check.py`, a `no_agent` cron entry every 5 minutes with `deliver: chat`,
on the pattern of `stall-watch` (`agents/platform/cron/jobs.json:266-280`,
`stall_watch.py:14-15`). Each tick it reads `proposal-states`, advances each open record by one
step, posts what is due, and prints only faults and the outcomes that go to chat. The predicates
are per check id in code: for `no-pdb`, a budget selecting the workload exists with
`disruptionsAllowed >= 1` and `currentHealthy == desiredHealthy`, and the re-check is clean.

It is not a model turn for three reasons. Waiting is most of the work, and a model turn that finds
nothing to do is what `github_scan_gate.py` exists to avoid (`agents/platform/scripts/github_scan_gate.py:5-21`).
Every decision here (revision contains commit, counters flat, finding reproduces) has a
deterministic answer, and the 11-05 misreport was a model answering one of them from the wrong
reading. And the outcome must read the same every time, because the auto-merge offer counts these
outcomes.

The job reads clusters as the pod's identity and writes only comments and, after row 14, revert
pull requests. It needs a paragraph in the site's security reference beside the `stall-watch` one
(`security-and-iam.md:329`).

## 5. Windows and deadlines

T counts from the merge time. The window starts at `applied`.

| Fix type                   | T (applied by) | Window                                                                                                    | Re-check                                            |
| -------------------------- | -------------- | --------------------------------------------------------------------------------------------------------- | --------------------------------------------------- |
| Missing PDB                | 60 min         | 5 min: budget status as in §4.2, workload Ready count unchanged                                           | `no-pdb` clean; `blocking-pdb` clean on the cluster |
| ComputeClass node selector | 60 min         | rollout complete within 30 min, then 15 min with no restart, no OOMKill, every pod on a node of the class | `untargeted-compute-class-workload` clean           |

The deadlines are settings with these defaults. Argo polls its repository every three minutes by
default and Config Sync every fifteen seconds, so an hour is a stuck sync rather than a slow one. A
customer whose delivery runs on a release train sees "not applied", which is the truth. "Not
applied after T" means only that the reconciler never applied the merge within T. It says nothing
about the cluster. After reporting it the record is closed, and a later sync gets no second
comment (open question 3).

## 6. Failure modes

| What happens                                                   | What the user reads                                                                                                  |
| -------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------- |
| Reconciler still at the old commit (the 11-05 case)            | nothing until it applies; "not applied by T" with the reconciler's detail if it never does                           |
| Reconciler error at the merge for 15 minutes                   | "not applied", with the reconciler's error                                                                           |
| Ancestry cannot be answered (forge without compare, or outage) | `unconfirmed` keeps waiting; at T, "not applied: could not confirm the applied revision contains the merge"          |
| Target owned by no reconciler the agent can read               | the object is observed directly and the report says so                                                               |
| Cluster unreadable on a tick                                   | nothing; the tick retries. Unreadable for the whole window ends in "not applied" or a failed window, with the reason |
| Re-check cannot run (audit failure, stream disabled)           | "applied and healthy; the finding could not be re-checked", never "fixed"                                            |
| Broker restarted                                               | nothing; one tick later the sweep's listing is back                                                                  |
| Pod restarted mid-window                                       | the window restarts from the persisted `applied` time                                                                |
| Pull request reverted by a person before the check finishes    | the re-check reports what it sees; the comment states the observation and makes no claim about cause                 |

Idempotence: each record moves `open → merged → applied → reporting → reported`, and the comment
carries a hidden `post-merge-check:<outcome>` marker. A driver that dies between posting and
writing `reported` finds the record in `reporting`, reads the pull request's harness comments for
the marker, and posts only if it is absent. The thread line is posted after the comment and marked
on the record separately, so a crash costs at most one missing chat line and never a second
comment.

Privacy: the comment is built from fixed text, object names, commit SHAs, counts and the
reconciler's clipped detail (`MAX_DETAIL_CHARS`, 240, in #5041). It never includes pod logs,
termination messages or event messages. Those are text anyone who can write to the namespace
chooses, the rule `stall-watch` follows (`security-and-iam.md:329`). Reconciler messages can quote
the repository's own content, and the pull request's readers already have the repository, so the
detail may appear in the comment. The chat line carries only the outcome, the object and a link to
the comment, because a channel's readers are not necessarily the repository's. Nothing from a
record goes into `status.usage`.

## 7. Testing

- **Unit tests:** every `read_sync` state mapped to the driver's next step; each record transition
  under a fake clock, including T expiring in each state and an error clearing before 15 minutes;
  the crash-in-`reporting` path; the marker shared with the audit; the sweep-off fallback; a body
  with no `audit-findings` block.
- **Bench:** the GitOps fix cycle (`bench/kube_agents_bench/gitops.py:209`, called from
  `harness.py:2958`; [`gitops-fix-cycle.md`](gitops-fix-cycle.md)) already opens a pull request,
  merges it, and waits for Argo to sync it. A new case runs the driver after `await_fix_cycle` and
  grades the comment, in three variants: fixed, still broken (the planted fix misses the selector),
  and not applied (Argo automated sync off). Grading a comment needs a new verifier. Running GitOps
  cases nightly needs a repository and credentials in the pool projects, which is plan row 19.
- **Live:** on a dev install, a planted PDB finding merged once with Argo CD and once with Config
  Sync. Pass: one "verified fixed" comment each. Then a fix that misses its selector. Pass: one
  "still broken". Then the same with the Application's automated sync off. Pass: "not applied".

## 8. Implementation sequence

1. Row 4: the Session KV table, the hook's write at claim, and the read endpoint for the driver.
2. Row 5: #5041's reader, the forge compare verb behind `is_ancestor`, and the reconciler lookup
   from ownership markers.
3. Row 6: the health window, reusing `fleet_health.py`'s expressions.
4. Row 7: the single-finding re-check, and the audit holding its persists notice for young merges.
5. Row 8: `post_merge_check.py`, the cron entry, `proposal-states` on the broker, the report, the
   security-reference paragraph and the ledger design's §4 update.
6. Row 14 (milestone 2): the revert pull request.

Rows 5, 6 and 7 can be built in parallel once row 4 lands.

## 9. Open questions (undecided)

1. The 11-05 fix came from `submit-suggestion`, which this design does not cover. Should a chat
   request to fix a missing PDB go through the fleet audit's generator, so it gets a subject?
   Or should `submit-suggestion` record one?
2. Should the revert pull request ship in milestone 1 for human-merged installs, or only with
   auto-merge? And for a still-broken fix that does no harm, such as a PDB whose selector misses,
   is a revert worth the noise?
3. When a fix is applied after "not applied by T" was posted, should the check stay silent, or post
   one late comment?
4. Are the defaults for T and the window right? The live runs and the first pilot will say.
5. Will the broker's owner accept `proposal-states`, an answer served from the sweep's last
   listing? If not, the fallback in §3.2 becomes the only path: one `proposal-view` per waiting
   record per tick.
6. Should a scheduled audit's "verified fixed" also go to the home channel, or stay on the pull
   request?
7. Once kanban cards are gone (#3654), what links a worker's pull request to the session it reports
   to?
