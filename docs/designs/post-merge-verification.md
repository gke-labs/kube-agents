# Post-merge verification: did the merged fix work?

**Status:** proposed, 2026-11-06. Part of gke-labs/kube-agents#2000 (read Argo CD and Flux status
to confirm fixes are live) and of the autonomy epic gke-labs/kube-agents#4250, whose first
milestone is knowing what happened to each fix. Builds on
[`proposal-outcome-readings.md`](proposal-outcome-readings.md) (the broker's 240 s proposal
sweep), [`proposal-origin.md`](proposal-origin.md) (the claim, the `proposals_opened` ledger and
the origin of each proposal), `mergeCommit` on the proposal record (#4933) and
`agents/platform/scripts/reconciler_sync.py` (#5041). Citations are `path:line` on `main` at
`6d2b11e3e`.

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
comparison before it judges the cluster, and reports without being asked. That particular fix came
from a chat request, a path this design does not yet cover (open question 1); the same PDB fix
from the fleet audit is the first case it does.

## 2. Scope

The first fix type is the missing PDB (`no-pdb`,
`agents/platform/skills/fleet-audit/scripts/collect.py:7362`), which the fleet audit writes in code
(`_pdb_manifest`, `audit_report.py:17203`). The second is the ComputeClass node selector
(`untargeted-compute-class-workload`, `collect.py:4178`, `:7576`), once the audit writes that fix
in code too; today the model writes it. The reconcilers are Argo CD `Application`, Flux
`Kustomization` and Config Sync `RootSync`/`RepoSync`. The driver reads fleet clusters as the
pod's Google service account through `get-credentials`, as `stall-watch` does, so what it may read
there is set by the install's permission set: `roles/container.viewer` in the default sets carries
`container.thirdPartyObjects.get` and `.list`
(`docs/site/src/content/docs/reference/security-and-iam.md:72-74`), which covers these kinds. A
`custom` set needs those two permissions or an equivalent Kubernetes grant. On the management
cluster the agent's role already grants them
(`k8s-operator/internal/controller/platformagent_manifests.go:6089-6119`).

Out of scope: Flux `HelmRelease`, whose applied revision is a chart version rather than a Git
commit, so whether it contains the merge cannot be asked of the forge (#5041 reads it `unknown`);
fixes delivered by Terraform or Config Connector, which have no in-cluster revision to compare;
pull requests from `submit-suggestion`, which carry no structured finding (open question 1); and
merging anything. kube-agents never merges, approves or reopens a pull request.

A later milestone of the epic offers auto-merge for a fix type after several clean merges, where
clean includes passing this check and not being reverted within seven days. That is why the
outcome must be deterministic and hard to forge (§3.1, §4.2).

## 3. The flow

### 3.1 Record at open

The `pr_opened` hook already claims every proposal the agent opens with the broker's one-time
token and writes it to the Session KV `proposals_opened` ledger with its origin
(`agents/platform/plugins/pr_opened/__init__.py:1-28`,
`agents/platform/scripts/session_kv_server.py:1827-1834`, `:8291-8312`). In the same step it writes
a verification record, keyed by the proposal URL like the ledger row: repository, number, origin,
`opened_at`, and the address of the thread the work reports to. Today's origin resolver returns only
the origin and platform (`pr_opened/__init__.py:386-402`), so it gains the trusted session id it
resolved from, and the hook stores that session's `(platform, chat_id, thread_id)` from
`_lookup_platform_threads` (`session_kv_server.py:5355`) at claim time. Storing the address rather
than the session id matters because session metadata is deleted after 14 idle days
(`session_kv_server.py:160`), and a pull request can merge later than that. A scheduled run has no
thread. Only a claimed proposal gets a record, so the model cannot create one by printing a line.

The subject is filled on the driver's first tick after the record appears, a few minutes after
the pull request opened, and never refreshed, so a later edit to the body changes nothing. It is
the finding ids in the body's hidden `audit-findings` block (`render_remediation_pr_body`,
`audit_report.py:13825-13842`) and the stream, read from the `audit:<stream>` label
(`remediation_labels`, `audit_report.py:15932`), or from the body's stream marker on a forge with
no labels. A finding id is `<check>.<cluster>.<namespace>.<object>` (`audit_report.py:21068`), so it
names the check and the target directly.

The body is not trusted for this. The model opens pull requests through the same `proposal-create`
verb the audit uses, and the audit already treats a marker read from a body as forgery surface
(`audit_report.py:11293-11296`). So the driver accepts the subject only when the proposal's changed
files are exactly what the fix type's generator writes for those finding ids, re-rendered by the
driver and compared byte for byte. A proposal that fails the comparison, or whose body has no
`audit-findings` block, keeps a record with no subject, which is dropped without a report. A
forged body can then name only a fix the harness would itself have written, for a target the audit
already reads.

The subject also names the reconciler that delivers the target, read from the target workload's
ownership markers: Argo's `argocd.argoproj.io/tracking-id`, Flux's `kustomize.toolkit.fluxcd.io/name`
label, or Config Sync's annotations (`agents/cluster/skills/gke-load-bearing-change/SKILL.md:32-34`).
For a missing PDB the budget does not exist yet, so the markers come from the workload it covers.
The driver looks for an Argo Application on the target cluster first and then on the management
cluster, which covers a central Argo CD there. The reconciler counts only when its source is the
pull request's repository and its path covers a changed file. Otherwise, including Argo releases
that track by label and leave no `tracking-id`, the record has no reconciler and §3.3 observes the
object instead.

The expected-after state is not stored as text. Each check id has its predicate in code (§4.2).

### 3.2 Detect the merge

No new poller. The broker's readings sweep already lists every proposal the install authored,
with its state, in every managed repository every 240 s (`agents/platform/scripts/vcs_readings.py:44`,
`proposal-outcome-readings.md` §5). This design keeps that listing's `number → state` map per
repository after each complete walk, replaced whole each time, and serves it on a new read verb,
`proposal-states`, which makes no forge call. That reverses an invariant the readings design
states: "no answer the broker gives depends on the cache this keeps" (`vcs_readings.py:15-16`). The
case for reversing it is that the listing is already paid for and is a reading, not an event log:
a merged proposal stays merged, so a broker restart loses nothing, and until the first sweep after
one the verb answers "no reading" and the driver waits a tick. A repository past the sweep's walk
bound is marked as such in the answer.

The driver reads `proposal-states` once per tick. On the tick that first sees `merged`, it makes one
`proposal-view` for `mergeCommit`, `sourceRevision` and the merge time, because the GitHub listing
comes from the search API, whose shape has no `merge_commit_sha`
(`agents/platform/scripts/providers/github/forge.py:731-752`, `providers/github/translate.py:202-223`).
A proposal closed without a merge ends its record silently.

Three cases fall back to one `proposal-view` per waiting record per tick, at most 20 per tick: an
install that turned the sweep off with `CREDENTIAL_PROXY_VCS_READINGS=off`, a repository past the
walk bound, and a broker that does not serve the verb.

`mergeCommit` needs care before it is used. #5041's `read_sync` takes only a full 40- or
64-character commit id. Bitbucket Cloud gives 12 characters (#4933), so the driver expands it with
one commit read before the first `read_sync` call. GitLab gives `null` after a fast-forward merge
and Bitbucket Data Center may give `null`. After a fast-forward the proposal's head commit is
itself on the target branch, so the driver uses `sourceRevision` instead. With neither, the record
is reported "merged; the merge commit could not be identified, so the cluster was not judged".

### 3.3 Wait until the reconciler has applied the merge

The driver reads the reconciler object through the sandbox and passes it with the merge commit to
`read_sync(obj, merge_commit, is_ancestor)`, which answers `applied`, `error`, `pending`,
`unconfirmed` or `unknown`. Reconcilers track a branch, so the applied revision is often a later
commit. `is_ancestor` asks the forge whether that commit contains the merge, through a new broker
verb backed by each forge's compare or merge-base API. A forge without such an API answers `None`,
which reads `unconfirmed`.

`applied` moves on to the health window. `pending` and `unconfirmed` keep waiting. `error` must
persist for 15 minutes before it counts, because #5041 makes an error one reading and not a verdict
(an Argo `SyncError` from the previous commit can linger until the next reconcile). A record with no
reconciler observes the object instead: the new PDB exists with the merged spec, or the workload's
pod template carries the merged `nodeSelector`, and the report says it observed the object
directly. A record still not applied when the type's deadline T passes (§5) is reported as "not
applied" (§3.6).

### 3.4 Health window

At `applied` the driver records a baseline of the target's pods: names, `restartCount` per
container, and Ready. It holds a per-type window (§5) and reads again at the end: the rollout is
complete (`observedGeneration`, `updatedReplicas == replicas == readyReplicas`), every pod is
Ready, no container's `restartCount` rose above the baseline, and no `lastState.terminated.reason`
is `OOMKilled`. A pod that comes up and then crashes reads healthy at first; the bench's optional
soak exists for the same reason (`bench/kube_agents_bench/gitops.py:44-49`). Where Managed
Prometheus is available, the window also runs the fleet health audit's restart and availability
expressions (`agents/platform/skills/fleet-audit/scripts/fleet_health.py:205-212`, `:236-263`),
re-parameterised: the range is the window itself and the restart threshold is 1, where the audit
uses one hour and 3 (`fleet_health.py:179-180`). That catches a restart that recovered between the
two reads. The PDB fix restarts no pods, so its window reads only the budget and the workload's
Ready count.

### 3.5 Re-check the one finding

The fleet audit re-derives one finding on demand: its check against its target only, run through
the sandbox from the image's copy of the script, as `stall_watch.py` runs `stall_report.py`
(`agents/platform/scripts/stall_watch.py:56-70`). The answer is "reproduces" or not, with the
evidence excerpt the audit already renders, and `derive_finding_state` turns it into
`resolved-merged` or `pr-merged-persists`. A check that judges the whole cluster is narrowed to the
target: `blocking-pdb` is read for the new budget only, so an unrelated budget elsewhere cannot fail
a correct fix.

### 3.6 Report once

One outcome per record, posted as a harness comment on the merged pull request and as one line in
the originating thread:

| Outcome                 | When                                                                | The pull request comment says                                                             |
| ----------------------- | ------------------------------------------------------------------- | ----------------------------------------------------------------------------------------- |
| Verified fixed          | applied, window passed, finding no longer reproduces                | which reconciler applied which commit and when, the window's readings, the clean re-check |
| Still broken            | applied, and the finding reproduces or the window failed            | the same, plus the re-check's evidence or the window reading that failed                  |
| Applied, not re-checked | applied and the window passed, but the re-check could not run       | the same as fixed, without the re-check, and that nothing was verified                    |
| Not applied by T        | never `applied`, an error persisted, or the merge commit is unknown | the reading's state and revision; it says the cluster was not judged                      |

The line goes to the stored thread address through a new Session KV endpoint for the driver, which
delivers it the way the event watcher's follow-up is (`_post_followup`, `session_kv_server.py:7878`).
A scheduled audit run has no thread. Its "verified fixed" goes on the pull request only, and the
other outcomes go to the job's chat delivery. The exact wording is decided in the pull request that
ships the report, because it is a new comment users will see.

The daily audit and the driver must not both say a fix did not take. When the finding reproduces,
the "still broken" comment carries the audit's `audit-persists:<id>` marker for it. A window failure
alone carries no such marker, since the finding itself was not seen to reproduce. The audit checks
for that marker, in harness comments only, before posting its own notice (`marker_from_harness`,
`audit_report.py:11280`, called at `:16893-16899`). The driver checks for it too before posting, in
case the audit posted first. The audit also stops posting that notice while a merge is younger than
its type's T plus window, which closes the same race in the daily path that misled the 11-05 answer.

This reverses "a merged fix that worked is said nothing" for fixes this check covers. The audit's
own rendering stays silent. The confirmation comes from the driver, and
`fleet-audit-issue-ledger.md` §4 changes in the pull request that ships it.

### 3.7 Revert when still broken

On "still broken", the driver opens one revert pull request and nothing else. It never merges,
reopens or closes anything. The revert is made in a leased workspace
(`agents/platform/scripts/gitops_workspace.py`) from the target branch's head, reverting the merge
commit, and its body links the original pull request and the evidence. The driver opens no revert,
and says why in the report, when the merge commit is not the whole change (after a rebase or
fast-forward merge it is the branch tip, #4933) or when a later commit touched the same files. It
opens at most one per record. The revert ships after the report, in the epic's second milestone,
and until then "still broken" is report-only. The end-state architecture's reconcile-failure loop
(`docs/architecture/04-workflow-model.md:273-293`) is the nearest relative: it also opens a
corrective or revert pull request, but for a change the pipeline failed to apply.

## 4. Where it lives and what runs it

### 4.1 The record goes in Session KV

The record is a new Session KV table beside `proposals_opened`, keyed by URL. The fleet audit's
report store was the other candidate. It is written from the shell sandbox, where the model's own
commands also run, and it serves the audit alone, so a record there could be written by anything
the sandbox runs. The broker's sweep cache was the third. It is in memory, so every restart empties
it, and a record must outlive a restart by days.

Session KV is on the pod's volume and survives restarts. It is written over a loopback API with
the pod's key, which the sandbox does not hold, by the hook that already writes the ledger from the
claim. The driver runs in the same pod. Like the ledger, the table is kept out of the 14-day
idle-session cleanup (`session_kv_server.py:1868-1873`) and has its own rule: a record is pruned 30
days after its last change (reported, closed unmerged, or dropped without a subject), which outlasts
the seven-day revert window §2 mentions. The operator caps the gateway at one replica
(`docs/site/src/content/docs/reference/security-and-iam.md:142`), so there is one Session KV and one
copy of each record.

The key is the proposal URL, with the origin from `proposal-origin.md`. No kanban card id goes in
the record, because cards are being removed (gke-labs/kube-agents#3654).

### 4.2 A scheduled job with no model

The driver is `post_merge_check.py`, a `no_agent` cron entry every 5 minutes with `deliver: chat`
and `risk: high`, on the pattern of `stall-watch` (`agents/platform/cron/jobs.json:266-280`,
`stall_watch.py:14-15`). Each tick it reads `proposal-states`, advances each open record by one
step, posts what is due, and prints only faults and the outcomes that go to chat. The predicates
are per check id in code. For `no-pdb`: a budget selecting the workload exists at its current
generation (`status.observedGeneration == metadata.generation`), every pod is healthy
(`currentHealthy == expectedPods`), `disruptionsAllowed >= 1`, and the narrowed re-check is clean.

It is not a model turn for three reasons. Waiting is most of the work, and a model turn that finds
nothing to do is what `github_scan_gate.py` exists to avoid (`agents/platform/scripts/github_scan_gate.py:5-21`).
Every decision here (revision contains commit, counters flat, finding reproduces) has a
deterministic answer, and the 11-05 misreport was a model answering one of them from the wrong
reading. And the outcome must read the same every time, because the auto-merge offer counts it.

The job reads clusters as the pod's identity and writes only comments and, once the revert ships,
revert pull requests. It needs a paragraph in the site's security reference beside the
`stall-watch` one (`security-and-iam.md:329`).

## 5. Windows and deadlines

T counts from the merge time. The window starts at `applied`.

| Fix type                   | T (applied by) | Window                                                                                                    | Re-check                                                |
| -------------------------- | -------------- | --------------------------------------------------------------------------------------------------------- | ------------------------------------------------------- |
| Missing PDB                | 60 min         | 5 min: the budget predicate in §4.2, workload Ready count unchanged                                       | `no-pdb` clean; `blocking-pdb` clean for the new budget |
| ComputeClass node selector | 60 min         | rollout complete within 30 min, then 15 min with no restart, no OOMKill, every pod on a node of the class | `untargeted-compute-class-workload` clean               |

The deadlines are settings with these defaults. Argo polls its repository every three minutes by
default and Config Sync every fifteen seconds, so an hour is a stuck sync rather than a slow one. A
customer whose delivery runs on a release train sees "not applied", which is the truth. "Not
applied by T" means only that the reconciler never applied the merge within T. It says nothing
about the cluster. After reporting it the record is closed, and a later sync gets no second
comment (open question 3).

## 6. Failure modes

| What happens                                                   | What the user reads                                                                                                  |
| -------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------- |
| Reconciler still at the old commit (the 11-05 case)            | nothing until it applies; "not applied by T" if it never does                                                        |
| Reconciler error at the merge for 15 minutes                   | "not applied", with the reconciler's error reason                                                                    |
| Ancestry cannot be answered (forge without compare, or outage) | `unconfirmed` keeps waiting; at T, "not applied: could not confirm the applied revision contains the merge"          |
| Merge commit unknown (`null` with no fast-forward head)        | "merged; the merge commit could not be identified, so the cluster was not judged"                                    |
| Target owned by no reconciler the agent can read               | the object is observed directly and the report says so                                                               |
| Cluster unreadable on a tick                                   | nothing; the tick retries. Unreadable for the whole window ends in "not applied" or a failed window, with the reason |
| Re-check cannot run (audit failure, stream disabled)           | "applied, not re-checked", never "fixed"                                                                             |
| Broker restarted                                               | nothing; one tick later the sweep's listing is back                                                                  |
| Agent pod restarted mid-window                                 | nothing; the window ends at its persisted end time and compares against the persisted baseline                       |
| Pull request reverted by a person before the check finishes    | the re-check reports what it sees; the comment states the observation and makes no claim about cause                 |

Each record moves through `open`, `merged`, `applied` and `reporting` to `reported`, and can leave
early as `closed` (closed unmerged) or `dropped` (no subject). Any state can go straight to
`reporting` when its deadline passes. The comment carries a hidden `post-merge-check:<outcome>`
marker. A driver that dies between posting and writing `reported` finds the record in `reporting`,
reads the pull request's harness comments for the marker, and posts only if it is absent. The
thread line is posted after the comment and marked on the record separately, so a crash costs at
most one missing chat line and never a second comment. `marker_from_harness` counts any comment
posted with the install's credential, and the model posts with that credential through `vcs.py`
(`audit_report.py:11298-11309`), so a model could pre-place a marker and silence a report. The
audit's `audit-persists` marker has the same residual today.

Privacy: the comment is built from fixed text, object names, commit SHAs, counts, and each
reading's state and condition reason. It never includes message text: not pod logs, termination
messages or event messages, and not the reconciler condition messages that #5041 clips into its
detail. Those messages can quote admission-webhook denials and other cluster-side text, which
anyone who can write to the namespace influences, the rule `stall-watch` follows
(`security-and-iam.md:329`), and the repository may be more public than the cluster. The chat line
carries only the outcome, the object and a link to the comment. Nothing from a record goes into
`status.usage`.

## 7. Testing

Unit tests cover every `read_sync` state mapped to the driver's next step, and each record
transition under a fake clock, including T expiring in each state and an error clearing before 15
minutes. They also cover the crash in `reporting`, the marker shared with the audit in both orders,
the generator comparison accepting the harness's own fix and refusing an edited one, a short and a
`null` merge commit, and the fallback when the sweep is off.

The bench's GitOps fix cycle already opens a pull request, merges it, and waits for Argo to sync
it (`bench/kube_agents_bench/gitops.py:209`, called from `harness.py:2958`;
[`gitops-fix-cycle.md`](gitops-fix-cycle.md)). A new case runs the driver after `await_fix_cycle`
and grades the comment, in three variants: fixed, still broken (the planted fix misses the
selector), and not applied (Argo automated sync off). Grading a comment needs a new verifier, and
running GitOps cases nightly needs a repository and credentials in the evaluation pool.

Live, on a dev install: a planted PDB finding merged once with Argo CD and once with Config Sync
should each get one "verified fixed" comment. A fix that misses its selector should get one "still
broken", and the same fix with the Application's automated sync off should get "not applied".

## 8. Implementation sequence

1. The record: the Session KV table, the origin resolver's session id, the hook's write at claim,
   and the driver's read and thread-post endpoints.
2. The reconciler reading: #5041's reader, the forge compare verb behind `is_ancestor`, the merge
   commit expansion, and the reconciler lookup from ownership markers.
3. The health window, reusing `fleet_health.py`'s expressions with the window's own range.
4. The single-finding re-check, narrowed checks, and the audit holding its persists notice for
   young merges.
5. The driver: `post_merge_check.py`, the cron entry, `proposal-states` on the broker, the generator
   comparison, the report, the security-reference paragraph and the ledger design's §4 update.
6. The revert pull request, in the second milestone.

Steps 2, 3 and 4 can be built in parallel once step 1 lands.

## 9. Open questions (undecided)

1. The 11-05 fix came from `submit-suggestion`, which this design does not cover. Should a chat
   request to fix a missing PDB go through the fleet audit's generator, so it gets a subject?
   Or should `submit-suggestion` record one?
2. Should the revert pull request ship in the first milestone for human-merged installs, or only
   with auto-merge? And for a still-broken fix that does no harm, such as a PDB whose selector
   misses, is a revert worth the noise?
3. When a fix is applied after "not applied by T" was posted, should the check stay silent, or post
   one late comment?
4. Are the defaults for T and the window right? The live runs and the first pilot will say.
5. Will the broker's owner accept `proposal-states` and the reversed invariant? If not, the
   fallback in §3.2 becomes the only path: one `proposal-view` per waiting record per tick.
6. Should a scheduled audit's "verified fixed" also go to the home channel, or stay on the pull
   request?
7. Once kanban cards are gone (#3654), what does the origin resolver read a worker's session from?
