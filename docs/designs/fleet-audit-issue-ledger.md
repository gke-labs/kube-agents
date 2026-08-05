# Fleet Audit — Issue Ledger and Remediation Pull Requests

> **STATUS — design of record; implemented.** The behaviour described here is the behaviour the
> harness ships: a GitHub **issue** per audit stream plus narrow, per-finding remediation PRs linked
> back to it. It replaces the model in which each audit stream owned one continuously-rewritten Pull
> Request.

**Scope:** How the five autonomous audit watchdogs publish findings and propose fixes.
**Supersedes:** the PR-as-report model introduced in `424a345`.

---

## 1. Why the Pull Request is the wrong container

The superseded `fleet-audit` path gives every audit stream exactly one open PR, force-pushed and
rewritten on each run. The idempotency and delta machinery around that is sound; the _object_ is
not.

- **The code manufactures a diff to get a comment thread.**
  `audit_pr.py` commits with `--allow-empty` unconditionally, and logs "No manifest remediations;
  committing an empty report commit." Two of the five streams — RBAC posture and upgrade readiness —
  produce mostly `gcloud` and `manual` remediations, so their PRs are routinely prose wearing a
  commit. Needing a fake commit to obtain a durable, labelled, commentable object is the signal that
  the object should have been an issue.

- **The diff is all-or-nothing.** Every manifest for a run lands on one branch. A reviewer who
  agrees with five findings and rejects two has no move except "request changes" on a branch that is
  force-pushed out from under them on the next run.

- **Force-push orphans review.** Line comments on the previous diff detach every run.

- **Closing on a clean run reads as rejection.** A closed PR means _declined_; a closed issue means
  _done_. Same API call, opposite meaning to a human scanning notifications.

- **The model cannot express "fix merged, problem persists."** Once a PR merges, the story ends. An
  audit that keeps reproducing a finding after its fix shipped is exactly the signal a platform admin
  most needs, and today it is invisible.

## 2. The target model

Two tiers. The issue is the only always-on object.

### Tier 1 — the ledger issue

One open GitHub issue per audit stream, rewritten in place on every run.

- Title, body, labels, and every timestamp are generated. The agent never hand-writes them.
- The run-over-run delta mechanism is carried over unchanged: the hidden
  `<!-- audit-findings: [...] -->` marker moves from the PR body to the issue body and
  `parse_delta_block` / `compute_delta` are reused verbatim. What the marker _lists_ is the set of
  findings the body actually rendered, which under the size budget of §7.1 may be a strict subset of
  the run's findings.
- Findings render as rows in a findings table with per-finding anchors, each row naming its
  remediation state and, where one exists, its remediation PR.
- A clean run closes the issue **as completed** and closes any remediation PRs still open for that
  stream.
- `[SILENT]` has one rule and `finish` computes it, returning the answer as `silent_ok` (§7.5). It
  is true only when the run moved nothing an operator needs to hear about: `new == 0`,
  `resolved == 0`, no coverage gap, and no remediation PR opened or closed. If any of those fails
  the agent reports the ledger issue URL and a one-line summary — a run that resolved five findings
  and found nothing new is _news_, the audit reporting that the fleet got better. And a run that
  could not read the whole fleet is never silent even when both counters are zero, because "I found
  nothing" and "I could not look" are different statements and only one of them is reassuring
  (§7.4). `silent_ok` is the _scheduled_ verdict; an operator-dispatched run reports regardless.

### Tier 2 — remediation pull requests

Narrow PRs, each proposing the fix for one finding (or one group of findings whose manifest paths
collide), based on `main`, linked to the ledger issue with `Part of #<issue>`.

- Branch: `platform-agent/fix-<audit-id>-<slug>-<digest>`, where the digest is the first ten hex
  characters of a SHA-256 over the group's **sorted set of remediation paths** and the slug is a
  readable fragment of the first path's stem. **The branch name is the source of truth for the
  finding↔PR link.** It survives anyone editing the issue body, and a single
  `gh pr list --label audit:<audit-id> --label audit:remediation --state all
--json number,headRefName,state,mergedAt,closedAt,url,body,labels` reconstructs the whole mapping in
  one API call. No body marker is needed and none is added. Two of those fields are load-bearing
  rather than incidental. `labels` carries §3.3's harness-close-versus-human-close discriminator,
  `audit:stale-closed`, so dropping it from the projection would silently make every close look
  final. `closedAt` is what §3.1's superseded rule compares a `/remediate` timestamp against;
  without it every stale command wins by default, which is the failure that rule exists to prevent.
- **The branch is keyed on the files, not on the finding ids.** An earlier draft of this section
  named the branch after the lowest-sorted member id, which does not survive contact with the way
  the model actually works: ids are regenerated from scratch every run, so the day an SOP heading is
  reworded — or the day one finding in a two-finding group is fixed and the survivor becomes the new
  lowest id — the branch name changes, the `headRefName` lookup misses, and the harness opens a
  second pull request proposing a fix that is already sitting in review. The set of paths a fix
  touches is the stable thing, so that is what the name is derived from.
- **The finding id is still constrained**, to `^[a-z0-9]([a-z0-9._-]{0,98}[a-z0-9])?$` with no `..`
  segment and no `.lock` suffix — even though the path digest took it out of the branch name. The
  original justification was that it was a git ref component; that is no longer true, and a rule
  whose stated reason has evaporated is a rule someone deletes. Two live reasons replace it. The id
  is the **join key** of the ledger's hidden delta block and of the `audit-persists:<id>` marker,
  both matched by line-anchored regexes that whitespace or a stray newline would silently break —
  and a silent break here means the delta reports every finding as new. And it is **typed by a
  human** in `/remediate <id>`, which rules out case variation and shell metacharacters. The git
  constraints are kept as a superset rather than relaxed: they cost nothing, the SOP-generated
  shapes already satisfy them, and a future change that puts an id back in a ref then finds the
  gate already in place. The
  SOPs already build ids deterministically from lowercased slugs; the rule makes that a hard gate
  rather than a convention, and `hack/check-docs-terminology.sh` now extracts the pattern from
  `FINDING_ID_RE` and fails the build if any document quotes a different one.
- Body carries only that finding: evidence, impact, the recommendation, and the diff.
- Labels: `agent:audit`, `audit:<audit-id>`, `audit:remediation`, `severity:<highest>`.

## 3. Decisions

Recorded with rationale so a later reader does not re-litigate them.

### 3.1 Gating — hybrid: auto for critical manifests, pull-based for everything else

A remediation PR opens automatically **iff** the finding satisfies all of:

1. `severity == "critical"`, and
2. `remediation.kind == "manifest"`, and
3. there is no **live** pull request on its branch.

Every other finding stays prose in the ledger until a human asks for it. Rationale: the highest-risk
findings that have a mergeable diff should arrive ready to merge; the long tail must not turn five
streams into a notification firehose. At most five auto-promotions per run (§13 Q4); the surplus is
named in the ledger.

"Live" rather than "in any state" is condition 3's whole point, and the distinction is between two
kinds of closed PR. One the harness closed itself as stale carries the `audit:stale-closed` label,
and if the finding comes back, re-proposing the fix is exactly right. One a **human** closed is a
considered rejection, and re-opening it every morning would be the harness overruling a person on a
schedule. A merged PR is likewise not re-promoted — that finding is `pr-merged-persists` (§4), which
is a different problem and gets a different treatment.

**The human trigger is an issue comment command:** `/remediate <finding-id>`, or `/remediate all` to
promote every eligible finding in the stream. On its next run the audit parses the ledger issue's
comments, promotes the named findings, and replies once with the PR links. The command is honoured
only from a commenter with write access to the repo — `authorAssociation` of `OWNER`, `MEMBER`, or
`COLLABORATOR` (§13 Q5).

Only `manifest` remediations are promotable. `/remediate` naming a `gcloud` or `manual` finding is
refused with a comment explaining that its remediation is a command to run, not a file to merge — a
PR with no diff is precisely what this redesign exists to eliminate.

_Rejected alternative:_ checkboxes in the issue task list. A checked box conventionally means "done",
not "please open a PR", and the semantics fight the body being rewritten every run. The comment
command is explicit, auditable, repeatable, and needs no state that the body rewrite could clobber.

_Idempotency:_ commands are never marked as processed — the comment is never edited, reacted to, or
otherwise mutated, and that is deliberate: a repo writer who closes a remediation PR must be able to
re-issue `/remediate` and have it take effect. For a **promoted** finding this needs no state at all;
the finding already has a branch and a PR discoverable by name, so re-reading the same command on a
later run is a no-op by construction.

_But a command that is never marked processed is a command that is read again every morning_, and
that turns the escape hatch of §4 into a way around the close button. A `/remediate` posted in March
would re-open, in April, the pull request a human closed in April — and again in May, and every
morning after, which is the exact loop the harness/human close split exists to prevent, re-entered
through the door left open for changing one's mind. So an explicit request overrules a human close
only when the request was written **strictly after** it. An older one is not honoured and not
silently dropped: it is reported as `superseded`, naming the close that answered it and the fact
that a fresh `/remediate` would be honoured. Unknown timestamps on either side lose the comparison,
because the two failures are not symmetric — an unrequestable finding costs one more comment, an
uncloseable pull request costs the reader's belief that the close button does anything.

The actions that must happen _exactly once_ have no such natural key, so each gets a **hidden
marker**, the same technique the delta block already uses. Every one of them is written into the
comment the harness posts to do the thing, never into a body:

- `<!-- audit-persists:<finding-id> -->` in the persistence comment on the merged remediation PR.
  Present means that comment has already been posted for that finding; absent means post it.
- `<!-- audit-refused:<comment-node-id> -->` in the refusal reply on the ledger. Present means that
  specific `/remediate` comment has already been refused; absent means reply and record it.
- `<!-- audit-acked:<comment-node-id> -->`, likewise, in the acknowledgement of a `/remediate` that
  was **accepted**. A request that is honoured needs answering exactly as much as one that is
  refused — the requester is owed the PR links — and a standing comment on a long-lived ledger would
  otherwise be re-answered every morning for as long as the issue is open.
- `<!-- audit-stale-closed:<pr-number> -->` in the stale-close comment. It records that the comment
  was posted, and nothing more; the harness-versus-human discriminator of §3.3 is the
  `audit:stale-closed` **label**, which is the thing the state derivation actually reads.

**The marker goes in a comment, not in the body it is about, and that is the load-bearing half.**
The ledger body is regenerated from scratch on every run, so a marker written there is erased by the
next morning's edit and the "exactly once" guarantee lasts one day. A comment is append-only: the
harness can add to the thread but never rewrites what is already in it, including its own earlier
replies. So the readers scan the comment thread, and — for a pull request, where a human might have
pasted one — the body as well, before concluding an action is still owed.

Keying the `/remediate` markers on the **comment node id**, not the finding id, is what lets a later
`/remediate` for the same finding be answered again: the second comment is a different comment, and
a person asking twice is asking twice.

Idempotency lives in comments the harness itself wrote, never in the command comment a human wrote.

### 3.2 A first-class `recommendation` field

`remediation.note` is a one-liner and cannot carry the argument a reviewer needs. Findings gain a
required `recommendation` object:

```json
"recommendation": {
  "action": "Apply a default-deny NetworkPolicy to the payments namespace.",
  "rationale": "Namespace-scoped default-deny is the smallest change that closes east-west exposure without touching the mesh config; a mesh AuthorizationPolicy would also work but takes effect only for injected pods.",
  "risk": "Any unlabelled cross-namespace traffic into payments breaks on apply. Verify with the traffic query in the SOP first."
}
```

- All three sub-fields are required, non-empty strings, for **every** finding — not only promotable
  ones. Making it conditional would let the agent defer the hard thinking to promotion time, when the
  evidence is no longer in front of it.
- Rendered in the ledger under each finding, and as the PR body's "Why this fix" section.
- Cost: a validator change plus a prose section in all five governance SOPs.
- **Size cost, measured.** An SOP-shaped finding renders at roughly 968 characters today; the
  required `recommendation` (`action` + `rationale` + `risk`) takes that to roughly 1,439. Against
  GitHub's 65,536-character body limit, overflow therefore moves from N≈67 findings to **N≈45**.
  This is the reason §7.1 specifies a size budget. It is not an argument against the field: the
  reviewer's argument is worth more than the forty-fifth minor finding, so the field stays required
  and the renderer learns to truncate.

### 3.3 Stale remediation PRs are auto-closed — over complete coverage

When a **complete** run no longer reproduces a finding that has an open remediation PR, the PR is
closed with a generated comment naming the date and each finding it was opened for. Over a partial
run nothing is closed (§7.4): retiring a fix asserts that its finding is gone, and an audit that
could not read the cluster has no standing to assert it.

The comment does **not** print the command that no longer reproduces, or its output, and an earlier
draft of this section promising both was wrong about what is knowable at that moment. A resolved
finding is by definition absent from the current document, so its evidence is not in hand; the only
place it survives is the previous ledger body, and recovering it would mean parsing rendered
Markdown back into fields. The renderer emits the command only on the rare path where a caller
supplies the finding — and says nothing rather than print an empty code fence.

Accepted risk: this can close a PR a human was mid-review on. Mitigations, all three required:

- The closing comment states plainly that the PR may be reopened, and says **exactly** what happens
  if the finding returns: a `critical` manifest finding is re-proposed automatically on this same
  branch, at most five per run, and anything else is listed on the ledger as awaiting
  `/remediate <finding-id>`. The comment is not allowed to promise a fresh pull request to every
  reader, because auto-promotion does not open one for every reader — and the findings it silently
  would not re-propose are precisely the low-severity ones nobody is watching for.
- **The branch is not deleted on close.** Branches are cheap and any human fixup pushed to the branch
  survives. A branch is reset only when the same finding is promoted again.
- **The close is labelled `audit:stale-closed`.** Without a marker, next month's run cannot tell its
  own close from a maintainer's rejection, and it must treat those oppositely (§3.1). The label is
  the harness signing its own work; removing it makes the close permanent.

Two orderings inside that sequence are load-bearing, and both are the kind of thing that only shows
up when a `gh` call fails:

- **Label first, and refuse to close without the label.** An _unlabelled_ close is worse than no
  close at all — it is indistinguishable from a human rejection, so the finding is retired
  permanently and never re-proposed. A labelled pull request that is still open costs one line of
  log noise and is finished on the next run. So a failed label edit skips the close entirely.
- **Announce at most once; close as many times as it takes.** The `audit-stale-closed` marker
  records that the comment was posted, not that the close succeeded. Every pull request reaching
  this step is open, so a marker already on the record means an earlier run commented and _then_
  failed to close; treating it as proof of the close leaves the PR open forever while the ledger and
  the run summary both report it closed. The retry re-issues the close without repeating the
  comment. For the same reason a close whose `gh` call fails is never added to `prs_closed` — a run
  summary that describes work that did not happen is worse than one that admits it.

### 3.4 Replace the PR-report path outright

No flag, no deprecation window, no dual mode. `audit_pr.py` becomes `audit_report.py`, the
PR-as-report rendering is deleted, and all five SOPs plus the site docs are rewritten in the same
change.

**No legacy reconciliation, and none is needed.** `finish` does not hunt for an open report PR on
the legacy `platform-agent/audit-<audit-id>` head branch, because no such PR can exist: the
PR-report path lives only on an unmerged feature branch, `main` carries no fleet-audit crons, and no
released image has ever opened a report PR. A guard keyed on the legacy branch name would be dead
code from birth — untriggerable in every environment that can run this skill, and therefore
untestable except against a fixture invented to justify it. The replacement is a replacement; there
is no fleet state left over from the model it replaces.

## 4. Finding lifecycle

The ledger renders each finding in exactly one state. Transitions are computed per run, never stored.

| State                | Condition                                             | Rendered as                           | Action taken                                            |
| -------------------- | ----------------------------------------------------- | ------------------------------------- | ------------------------------------------------------- |
| `open`               | reproduces; no PR on its branch                       | `open`                                | none, unless it qualifies for auto-promotion            |
| `pr-open`            | reproduces; branch has an open PR                     | `fix proposed` + link                 | **nothing** — the PR is left exactly as it is           |
| `pr-merged-persists` | reproduces; branch PR is merged                       | `⚠ fix merged, still reproduces`      | comment once on the merged PR; never reopen it          |
| `refused`            | reproduces; branch PR closed unmerged by a **person** | `fix refused` + link                  | none — the close stands until someone says `/remediate` |
| `withdrawn`          | reproduces; branch PR closed unmerged by the harness  | `fix withdrawn, awaiting re-proposal` | eligible for promotion again, exactly as if it had none |
| `resolved`           | no longer reproduces; PR open or absent               | not rendered — see below              | close any open PR (§3.3), keep the branch               |
| `resolved-merged`    | no longer reproduces; branch PR is merged             | not rendered — see below              | none; a merged fix that worked is the expected ending   |

**The last two rows have no rendering, and the "Rendered as" column cannot be made to give them
one.** A finding that no longer reproduces is absent from the run's document, so there is no row in
the findings table to carry a state — `derive_finding_state` is only ever called with
`reproduces=True` in production, and the two resolved labels never reach a reader. What the reader
sees instead is the delta comment, which names the resolution by id and by the title recovered from
the previous body. The distinction between the two states survives only in the code, where it
decides whether a pull request is closed as stale or left alone because it already merged.

Three of the rendered rows are easy to misread, and two of them were wrong in an earlier draft:

- **`pr-open` is not refreshed.** The draft said "refresh the PR body if the evidence changed",
  which would have the harness force-push over a reviewer's own commits every morning. An open
  remediation PR is left alone; the ledger links it, and the diff is whatever a human last made it.
- **`refused` is a human decision, not a rejected command.** It is what a finding looks like when
  someone closed its fix without merging — a considered "no" the harness must not overrule by
  re-proposing the same fix tomorrow. (A refused `/remediate` is a _reply_, not a finding state;
  there is no finding to put in a state, which is why the draft's condition for this row did not
  correspond to anything the renderer could compute.)
- **`withdrawn` is the other half of that row**, and splitting the two is not cosmetic. A closed
  unmerged pull request is two different events: one the harness closed itself under §3.3 because
  the finding had stopped reproducing, and one a person closed because they did not want the fix.
  The discriminator is the `audit:stale-closed` label the harness applies as it closes. Rendering
  the first as `fix refused` states that a person declined a fix when no person was involved, and
  the reader who believes it leaves alone the one case the harness is waiting to re-propose — a
  flapping finding would be fixable exactly once, and never again after its first quiet day. So a
  `withdrawn` finding is treated as having no pull request at all: auto-promotion picks it up on the
  usual terms (`critical`, `manifest`, under the cap), and `/remediate` reaches it without the
  after-the-close age test a `refused` finding imposes.
  A `refused` one is reachable only by `/remediate <id>` from someone with write access, and only by
  a command written _after_ the close — an older one is reported as `superseded` rather than
  honoured, because a comment nobody can edit away would otherwise re-open a human's close every
  morning forever.

`pr-merged-persists` is the state the current design cannot express and is a primary reason for the
change. It must be visually distinct in the ledger.

The two "once" obligations in the last column — the comment on a merged PR and the reply to a
refused command — are enforced by the hidden `audit-persists` / `audit-refused` markers of §3.1,
carried in the harness's own comments, not by mutating anything a human wrote.

## 5. Grouping

The promotion unit is a **non-overlapping remediation group**, not a finding. Findings whose
`remediation.path` values intersect must share one PR, or their branches conflict on merge. In
practice groups are almost always singletons.

- Group key: the sorted tuple of manifest paths, unioned transitively across findings that share any
  path.
- Branch name for a group: `platform-agent/fix-<audit-id>-<slug>-<digest>` over that same sorted
  path tuple (§2), with every member finding named in the PR body and each linking back to the same
  PR from the ledger. Deriving the name from the group key rather than from a member id is what
  makes it stable across runs: the key is the set of files, and the group survives its members being
  renumbered or partially fixed.
- Promoting any member of a group promotes the whole group. The reply comment says so.

## 6. Script surface

`agents/platform/skills/fleet-audit/scripts/audit_report.py` — three subcommands.

### `start --audit <id>`

Resolves the repo, refreshes credentials, establishes the GitOps clone, ensures labels, locates the
stream's open ledger issue, and returns the scratch path for `findings.json`. Emits:

```json
{
  "issue": 128,
  "repo": "acme/fleet",
  "workspace": "/opt/data/gitops/compliance-audit/acme__fleet",
  "findings_path": "/opt/data/scratch/findings_compliance-audit.json",
  "pending_remediation_requests": ["netpol-missing-payments"]
}
```

`pending_remediation_requests` is the parsed set of `/remediate` targets from the issue's comments,
surfaced early so the agent knows which findings need a manifest written during inspection.

`workspace` is the clone, and it is not decoration. The audit cron starts in the agent's profile
directory, which is not a working tree — so there is nothing to `git add` into and nothing for
`git config --get remote.origin.url` to answer. The harness therefore clones lazily on the way in,
and **every `remediation.path` is resolved against this directory**. A manifest written anywhere
else is a file the harness will never find. The clone is keyed by audit id so the five streams do
not share a working tree; [`gitops-workspace-leases.md`](gitops-workspace-leases.md) owns that
layout.

That also fixes an ordering problem worth recording: the GitHub App token is repo-scoped, so it
cannot be minted before the repo is known, and the repo used to be derived from the clone the token
was needed to create. The repo now comes from the `Git Repo:` line of `/opt/data/SETTINGS.md`, which
the operator writes at provisioning time and which is present from the pod's first second, with the
git remote kept only as a fallback. This resolves §13 Q1 in passing: `github-issue-resolver` already
read that same line, so the two skills now agree by construction rather than by coincidence.

`start` is also the **only** subcommand that scrubs the working tree — see the note under
`remediate`. Note the removed behaviour: it no longer resets a report branch. There is no report
branch.

### `finish --audit <id> --findings-file <path> [--dry-run]`

1. Validate the document (existing validator plus `recommendation`, the finding-id charset rule of
   §2, and the scope rules of §7.2).
2. Reconcile: one `gh pr list` call builds the finding→PR state map from head branch names.
3. Compute the delta against the ledger issue's `<!-- audit-findings -->` marker, unless its
   `<!-- audit-id-scheme -->` stamp names a scheme this run cannot join against — then `resolved`
   is withheld for the one run it takes to rewrite the block.
4. Compute coverage gaps (§7.4). A gap does not stop the run; it narrows what the run may conclude.
5. Clean run → answer every unanswered `/remediate` on the ledger, then close the ledger issue as
   completed, close every open remediation PR for the stream, print `CLEAN`. **Unless the run is
   partial**, in which case the status is still `CLEAN` but the issue stays open with a comment
   naming the gaps and no PR is retired.

   The answers come **before** the close, and that ordering is the whole of the rule. "Every
   `/remediate` gets exactly one answer" cannot have the clean run as its exception: this is the one
   morning the issue disappears, taking with it the thread the requester would have re-asked on, so
   it is the one morning silence costs the most. The answer says the finding no longer reproduces,
   and whether the ledger is closing or staying open on partial coverage. Authorization is not
   consulted — nothing is being acted on for anybody, and the answer is equally true and equally
   useful to a commenter without write access.

6. Otherwise → render and create-or-edit the ledger issue, apply the severity label, post the delta
   comment when the delta is non-empty.
7. Auto-promote every eligible critical manifest finding (§3.1) — at most five per run, the surplus
   named in the ledger as awaiting `/remediate` (§13 Q4) — and every authorised `/remediate` target,
   which is uncapped, by invoking the same code path as `remediate`.
8. Close stale PRs (§3.3), unless partial; comment once on `pr-merged-persists` PRs; answer every
   `/remediate` exactly once, with an acknowledgement or a refusal. Each "once" guard reads the
   hidden markers of §3.1.

The acknowledgement names a per-target outcome rather than a count, because "3 requests processed"
is indistinguishable from "3 requests silently dropped": opened with its URL, refreshed, already
open and deliberately not force-pushed over, `superseded` by a human close written after the request
(§3.1), or not published this run and queued for a retry. A **refusal** is likewise never silence.
Naming a `gcloud` or `manual` finding, naming an id that is not in the document, lacking write
access, writing `/remediate` mid-sentence where the line-anchored parser will not honour it, or
writing it with no target at all each get exactly one reply.

Four of those five replies carry the ids that would have worked — capped at ten, then "and N more",
since a refusal is help and not a second copy of the report. **The write-access refusal deliberately
carries neither the id list nor the syntax.** Every other refusal is a correction to someone who may
retry and succeed; that one is a "no" to someone who cannot, and handing them a menu of fixes they
are not permitted to request reads as an invitation rather than an answer. It says what the rule is
and stops. The one deliberate silence is a mid-sentence mention from someone without write access:
their correctly-typed command would have been refused anyway, and two replies to one comment that
was probably never a command is a bot picking an argument. A `/remediate` the harness itself renders
into a comment is always inside a code span, and inline code is stripped before the mention search
runs — otherwise the ledger reads its own replies back on the next run and answers itself forever.

Exit contract — nine keys, always all nine:

- `{"status":"OPENED","issue_url":"…","new":7,"resolved":0,"prs_opened":["…"],"prs_closed":[],"partial":false,"coverage_gaps":[],"silent_ok":false}`
- `{"status":"UPDATED","issue_url":"…","new":2,"resolved":3,"prs_opened":[],"prs_closed":["…"],"partial":false,"coverage_gaps":[],"silent_ok":false}`
- `{"status":"CLEAN","issue_url":"…","new":0,"resolved":5,"prs_opened":[],"prs_closed":["…"],"partial":false,"coverage_gaps":[],"silent_ok":false}`
- `{"status":"CLEAN","issue_url":"…","new":0,"resolved":0,"prs_opened":[],"prs_closed":[],"partial":true,"coverage_gaps":["prod-eu-1: API server unreachable"],"silent_ok":false}`
- `{"status":"UPDATED","issue_url":"…","new":0,"resolved":0,"prs_opened":[],"prs_closed":[],"partial":false,"coverage_gaps":[],"silent_ok":true}`

`--dry-run` renders the issue body and every PR body it _would_ open to stdout with zero git or gh
**side effects**: nothing is cloned, staged, committed, pushed, created, edited, commented, or
closed. It is not "zero subprocesses" and must not be described as such — resolving the repository
can fall back to reading `git config --get remote.origin.url`, and locating the clone to resolve
`remediation.path` against is a filesystem read. Those are the same read-only lookups a real run
performs before it does anything, which is the point: it applies the **same** grouping, promotion,
budget, and coverage-gap degradation as a real run, against the **same** workspace clone, so a dry
run that looks right is evidence the real one will be. A dry run that took a shorter path through
the code — or resolved paths against the current directory instead of the clone — would be worth
very little, because "the manifest is missing" is exactly the answer it exists to give early.

One asymmetry with the real run is deliberate: a dry run **warns** about a `remediation.path` that
is missing or fails containment, and does not rewrite the finding to `manual` the way `finish` does.
Degrading a document it is only previewing would show the reader a body the real run never produces.

### `remediate --audit <id> --findings-file <path> --finding <id>...`

The promotion primitive, callable directly and reused internally by `finish`. For each group: reset
the branch onto `main`, stage only the group's manifest paths (the existing wildcard-pathspec refusal
is retained), commit with a generated Conventional Commit subject, push, and create or edit the PR.

**It promotes what was named and nothing else.** The auto-promotion sweep of §3.1 belongs to
`finish`, where the whole fleet is being reported on anyway, and does not ride along on this
subcommand. Sharing one code path between the two is right; sharing the sweep is not — it would
make `remediate --finding one-id` open up to six pull requests, five of them for findings the
operator never mentioned and cannot tell apart from the one they did, on the command whose entire
purpose is to act on a specific request.

Manifest files must already exist under the workspace, but **a missing one is no longer a hard
error**. That finding degrades to `kind: manual`, keeps its evidence and recommendation, says in the
ledger that a fix was named but not written, and the report publishes. Killing a nine-critical
security report because one of the nine manifests was not written is the wrong shape of failure: it
throws away eight findings to punish one.

`remediate` degrades the same way, for the same reason at a smaller scale. A named target whose fix
is not a readable file inside the clone is refused **by name** — logged, and returned in the
`refused` key of its exit JSON so the acknowledgement comment can say so — while the rest of the
batch opens. "Not a readable file inside the clone" covers two different failures and the message
must not collapse them into "not on disk": either nothing was written at that path, or the path does
not resolve inside the repository at all (§9), and only the second is a security event. A
`SECURITY:` line in the log distinguishes them; telling an operator to write a file they already
wrote sends them looking in the wrong place. This matters because
`/remediate all` expands to every manifest-remediation id in the document: refusing the batch would answer a request for
thirty fixes with zero, and leave the operator to work out which one was to blame. Only the case
where _every_ named target is refused is an error, since there is then no partial success to report
and an exit 0 with an empty list would read as "done".

Neither `finish` nor `remediate` scrubs the working tree, and that is load-bearing rather than
incidental. The agent writes its remediation manifests into the clone _between_ `start` and
`finish`, and they are untracked until a remediation branch stages them — so a `git clean -fd` on
the way into `finish` would delete every fix the audit just produced and then report each one as a
file the model forgot to write. Only `start` may reset, because at `start` there is nothing yet to
lose and anything present is debris from a run that did not finish.

## 7. Rendering

| Artifact             | Contents                                                                                                                                                                                                                                                                                                                                                   |
| -------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Ledger issue title   | `[audit] <human name> — <n> findings (<c> critical)`, singular `1 finding`. Names from `AUDITS`, still asserted against `cron/jobs.json` by test.                                                                                                                                                                                                          |
| Ledger issue body    | Scope, findings table with state column and a link from each id to its detail, then per-finding detail: evidence, impact, its own id, recommendation, remediation, PR link. Hidden `<!-- audit-findings -->` marker last, listing the ids the body rendered, followed by the `<!-- audit-id-scheme -->` stamp that says which identity scheme minted them. |
| Scope                | Clusters covered with their `n/applicable` checks-run count (suffixed `(m n/a)` where checks were declared inapplicable) and optional per-cluster `limitations`, `skipped` with reasons, partial-coverage banner. Both tables cap at 60 rows. See §7.2.                                                                                                    |
| Size budget          | 60,000 characters, against GitHub's hard limit of 65,536. See §7.1.                                                                                                                                                                                                                                                                                        |
| Delta comment        | Two lists — new (severity-first) and resolved (by id) — plus a truncation note when the body could not carry everything. Reuses `render_delta_comment`.                                                                                                                                                                                                    |
| Clean-close comment  | Date and the clusters covered, then either "closing as completed" or the coverage gaps that keep the ledger open. Reuses `render_clean_comment`.                                                                                                                                                                                                           |
| Remediation PR title | `fix(<audit-id>): <finding title>`                                                                                                                                                                                                                                                                                                                         |
| Remediation PR body  | `Part of #<issue>`, the single finding's evidence, impact, **Why this fix** (the recommendation), and the risk note. For a group, one section per member.                                                                                                                                                                                                  |
| Stale-close comment  | Date, each finding the pull request was opened for, the `audit:stale-closed` label, and an accurate reopen note. Not the evidence — see §3.3.                                                                                                                                                                                                              |

### 7.1 Size budget

GitHub caps an issue body at **65,536 characters**; issue bodies and PR bodies carry the identical
limit, so nothing about moving from PR to issue relaxes it. The renderer targets **60,000**, leaving
headroom for the trailing marker and for anything a later section appends.

- **Per-finding trims.** Excerpts already trim to 40 lines / 2,000 characters. **Commands now trim
  to 2,000 characters too.** The SOPs mandate pasting the confirm command verbatim into
  `evidence.command`, which makes it the dominant per-finding term, and it was previously unbounded.
  So do the remaining free-text fields — title 300, impact and each `recommendation` sub-field
  1,500, `remediation.note` 2,000 — and `cluster`, `namespace` and `object` at 320, which clears
  `Kind/name` at its longest (a namespace is a 63-character DNS label, a resource name a
  253-character DNS subdomain, a GKE cluster name 40) while still capping a hostile value. Every one
  of these matters for the same reason: the selection loop renders the **first** finding whatever it
  costs, so a single oversized field on a single finding overflows the body and publishes nothing at
  all, every morning, for as long as that finding reproduces.
- **Table caps.** The scope and skipped tables cap at 60 rows each, with a trailing "…and N more"
  row. Without the cap a body with _zero findings_ overflows: 1,200 clusters plus 1,200 skipped
  entries renders 148,627 characters of pure scope.
- **Order of measurement.** Header, scope, and footer are rendered and measured first; whatever
  remains of the 60,000 is the findings budget. Findings are selected **severity-first**, so
  truncation only ever eats the least-severe end and criticals are structurally safe — a fleet with
  five criticals and three hundred minors publishes all five criticals no matter what.
- **Truncation is stated, counts are not.** When findings are omitted the body says so explicitly,
  and the title's counts remain the **true totals**. The reader is never told there are fewer
  findings than there are.
- **The delta marker describes what was rendered.** The hidden marker lists exactly the findings the
  body contains, not the full finding set. Otherwise the next run would see a truncated finding
  absent from the previous marker, or present in it and absent from the body, and report a finding
  that is very much still reproducing as _resolved_. The marker is itself a size term and was
  unbounded: 1,250 finding ids render 80,526 characters of marker alone, over the limit before a
  single word of prose. That figure came from an earlier reading of the obtainability SOP's
  roll-up rule; that SOP now caps a check at 25 findings per cluster
  ([obtainability_audit_sop.md:78](../../agents/platform/governance/obtainability_audit_sop.md)),
  so no single documented rule licenses a run that large today. Bounding the marker is still right:
  the cap is per check per cluster, five streams run against a fleet of unknown size, and a size
  term that grows with the fleet and is invisible in the rendered body is the worst kind to leave
  unbounded.
- **The two halves of the delta are measured against different sets**, because "appeared" and "was
  fixed" are different claims and truncation breaks them apart. `new` is _rendered minus previous_:
  the previous marker records what the last body rendered, so comparing it to anything wider
  announces every budget-dropped finding as new, every morning, forever. `resolved` is _previous
  minus **every** current finding, rendered or not_: a finding cut for space still reproduces, and
  calling it resolved puts a fix that never happened in writing, on the one finding nobody can see
  to contradict it. One yardstick for both halves is wrong in one direction or the other whichever
  one is chosen.
- **The delta comment is capped and ordered by severity.** Both of its lists cap at 50 rows, and the
  `new` list is sorted severity-first before the cap applies — an alphabetical cut decides what a
  reader sees by the first letter of a finding id, which is how a critical ends up under "…and 40
  more". The overflow line says the remainder is lower severity, so the reader knows the cut was not
  arbitrary. The same 50-row cap governs the **findings state index** at the top of the body — the
  one-row-per-finding table of id, severity, cluster, and state. There is no delta table in the
  body; the index needs no separate ordering rule because the body is already severity-first.
- The clean-close comment is measured against the same budget, for the same reason: a clean run on a
  fleet with 900 skipped clusters must still be postable.

### 7.2 Scope, skipped, and limitations

`scope.clusters[]` gains an optional `limitations` string. It exists because "I read this cluster
successfully, but some checks did not run or do not apply" had nowhere to live and collided with
`scope.skipped` — and that collision produced **false all-clears**. One SOP line tells the agent to
put an Autopilot cluster in `scope.skipped` because a node-level check cannot apply there; another
tells it not to flag anything on a skipped cluster. Together they suppress every real finding on a
cluster the agent was explicitly told to audit.

The SOPs now state one question, and the schema has one answer for each branch of it:

> A cluster appears in exactly one scope list. Could you read it? Yes → `scope.clusters`; if some
> checks did not run there, name them in that cluster's `limitations`. No → `scope.skipped`. Nothing
> goes in both, and nothing in `scope.skipped` may appear in a finding.

The validator enforces both halves: the two lists must be disjoint, and a finding whose `cluster`
names a skipped entry is rejected. The rendered scope table carries a `limitations` column only when
at least one cluster has one, so the common case stays two columns wide.

`limitations` closed the false all-clear that came from mis-filing a cluster. It did not close the
one that comes from not checking it, because it is optional and an audit that ran nothing has no
particular reason to volunteer that it ran nothing. A document naming three clusters, carrying no
`limitations`, and reporting `findings: []` validated, published as `CLEAN`, and closed the ledger —
and it was byte-for-byte indistinguishable from a complete run over a healthy fleet. It happened:
four of five streams finished in about fourteen seconds each having issued no inspection command at
all, because the SOP's checks sit past the point a partial read of the file stops.

So `scope.clusters[]` also gains a **mandatory** `checks_run`: one entry per check that actually ran
against that cluster, as `{check, command}` — the backticked slug from the SOP heading that defines
the check, and the literal invocation that ran it. `AUDITS` carries the roster per stream as an
`AuditSpec`, which makes four things enforceable that were not: an unknown or duplicated slug is
rejected; an absent field is rejected outright; an entry whose `command` is missing, is a call back
into this harness, or names none of `kubectl`/`gcloud`/`gsutil`/`bq`/`helm`/`curl` is rejected; and
an empty list is rejected unless that cluster's `limitations` says why nothing ran. The last of those
is a concession, not an oversight — a drift cohort below the comparability floor legitimately
compares nothing on a cluster it read perfectly well, and a hard non-empty rule would force the agent
to invent a slug to get published. An explained zero is still a coverage gap, so it stays partial and
the ledger stays open; only the false all-clear is foreclosed.

The same command may back several checks. The consistency audit reads nine facets out of one
`describe`, and rejecting the repeat would force it to invent nine distinct invocations — precisely
the fabrication the field exists to discourage.

The roster lives in code and the checks live in prose, so the two will drift. A test re-derives each
roster from its SOP's `####` headings and fails on any difference: a check added to an SOP but not to
`AUDITS` is a check no run is ever obliged to perform, which is the same silent hole one level up.

Unlike `limitations`, the `Checks` column renders on **every** run as `n/total`, suffixed `⚠` where
it falls short. A column that appears only when something went wrong is a column nobody learns to
read on the days it matters.

#### "Did not run" and "cannot run" are different claims

`checks_run` collapsed them, and the collapse had a cost that only shows on a real fleet. Four of
this stream's ten upgrade checks read a node pool, and an Autopilot cluster has none to read. The
SOP's answer was a `limitations` note — which is a coverage gap, so those clusters rendered `6/10 ⚠`
on every run, forever. `partial` was therefore `true` on every run, forever, and everything keyed to
`partial` followed: `resolved` pinned at `0`, the ledger unable to close, no stale remediation PR
ever retired. On the fleet this was found on, two of three clusters are Autopilot. The audit was
permanently unable to report good news about the majority of the fleet it audits, and the flag that
was supposed to mean "I could not look here" had come to mean "there is nothing here to look at" —
which is the opposite claim, published in the same column.

So `scope.clusters[]` gains an optional `checks_not_applicable`: a list of `{check, reason}` using
the same slugs as `checks_run`. Those checks leave the denominator rather than counting as missing,
so `6/10 ⚠` becomes `6/6 (4 n/a)`. `coverage_gaps` computes `applicable = roster - not_applicable`
and reports a shortfall against that, which is what lets a fully-covered Autopilot fleet close its
ledger.

The `reason` is required and must be at least sixteen characters. That is a deliberately crude
proxy: it cannot tell a real reason from a padded one, but it does stop `"N/A"`, `"n/a"`, and
`"autopilot"`, which is the whole distance between a field that documents a decision and a field
that launders one. The validator also rejects a slug that appears in both lists, since a check
either ran or could not, and an unknown or duplicated slug — the same rules `checks_run` gets, and
with the same discipline about not naming the roster in the message.

The new field is a second way to inflate coverage, and the design treats it the way it treats the
first: not by verifying it, which is impossible from a subprocess, but by publishing it. Every
exclusion and its reason render in the ledger under _Not applicable_, alongside the commands table,
where a reviewer who knows the cluster can contest one. Padding `checks_not_applicable` is the same
lie as padding `checks_run`, told by shrinking the denominator instead of inflating the numerator,
and it is exposed the same way.

#### The guard's first failure, and what it cost

`checks_run` shipped as a bare list of slugs, and the validator was helpful about rejecting one:
`Known checks: {the roster}`. That message inverted the guard. A run that inspected nothing could
submit guesses, read the real slugs off the `exit 2`, and resubmit the same empty document with the
right words in it — and on 2026-08-03 four of the five streams did exactly that. One of them never
re-read its SOP between the two attempts, which is how we know where the slugs came from. The
harness had turned itself into an answer key for the one claim it could not verify.

Four changes close it, and none of them pretend to verify anything:

- **No rejection names a slug.** Every `checks_run` message now points at the SOP filename that
  defines the roster (`AuditSpec.sop`, the same string a test pins against an independent map)
  instead of listing it. A test asserts that no rejection on any path contains any roster slug.
- **`start` hands the roster over.** The roster is in the SOP and the SOP is required reading, but
  "it will read far enough" is not a mechanism: Hermes's `read_file` defaults to 500 lines and every
  audit SOP fits inside that, and the run still asked for 100 lines of each, on files whose checks
  start past line 60. Printing `checks` and `sop` at `start` is free and removes the failure mode.
  Safe there in a way it is never safe in a rejection: `start` is the instruction, issued before any
  work; a rejection is a hint, issued after a failed attempt.
- **Each claim carries its command.** Typing ten slugs is free — the roster is a fixed, guessable
  list. Ten distinct plausible per-cluster invocations are not, and they have to be redone per
  cluster.
- **The commands are published.** The ledger's last section, _How this run checked the fleet_, is a
  collapsed table of every entry, rendered against whatever body budget the findings left and
  dropped whole rather than half if it does not fit.

One residual risk is worth naming because the design cannot remove it: the harness runs as a
subprocess of the agent, so it never observes the commands the agent issued — only the document
handed to it. An inflated `checks_run` still converts a partial audit into a false all-clear. What
changed is the cost and the aftermath: fabrication is no longer a ten-word line, and every
fabrication is published verbatim where the next run, or a reader, can re-run it. The mitigation is
the record, not the validator. Every SOP says so at the point of writing the field.

#### A clean run with nothing to publish

There was one more silence. With zero findings and no open ledger, `finish` logged "nothing to do"
and exited: no issue, no comment, no artefact. A stream could report a clean fleet every morning for
weeks while never having looked at it, and leave nothing behind to notice. That is why the incident
surfaced through issue #27 alone — it was the one stream with a ledger open from the day before.

So zero findings **plus a coverage gap** now opens a ledger of its own, titled
`coverage incomplete (n gaps, 0 findings)` rather than the all-clear phrasing, carrying the gap list
and the evidence table. Zero findings with complete coverage and no ledger still does nothing, which
is correct: there is genuinely nothing to say.

### 7.3 Untrusted text in a rendered body

Every string the renderer interpolates arrives from a model-authored document describing a cluster
nobody controls, so all of it is untrusted input to a Markdown renderer that will happily obey it.

- **Fences are computed, never literal.** A block's delimiter is a run of backticks one longer than
  the longest run inside its own content, so an excerpt containing ` ``` ` cannot break out of the
  block that is quoting it. The helper returns the **whole** block — opener, body, closer — because
  the earlier version returned a bare delimiter and two callers emitted a stray ` ``` ` into a
  comment while dropping the command they meant to show.
- **Fence detection follows CommonMark, including the indentation bound.** `strip_fenced_blocks`
  exists so a `/remediate` quoted inside evidence never fires, and quoting the command to discuss it
  is the single most likely thing anyone writes in one of these issues. A non-greedy ` ```…``` `
  regex pairs the first fence with the second and leaves the third dangling, so text every renderer
  puts _inside_ a block survives stripping. The rule is: open on a run of three or more backticks or
  tildes indented at most three spaces, close on a run of the same character at least as long,
  likewise indented at most three, nothing else on the line, unterminated fences run to the end.
  Dropping the indentation bound — stripping each line before comparing — makes four-space
  ` ``` `, which CommonMark and GitHub both render as literal text, read as a closer.
- **Table cells escape `|` and flatten newlines**; identifiers additionally replace a backtick,
  because one backtick closes the inline code span they sit in and the rest of the value renders as
  live Markdown.
- **Redaction runs before every clip**, not after, so a secret cannot survive by being past the
  truncation point (§9).

### 7.4 Partial coverage

`coverage_gaps(data)` folds the three representations of "did not look" — every `scope.skipped`
entry, every cluster `limitations` note, and every cluster whose `checks_run` falls short of the
checks that _apply_ to it — into one list of human-readable strings, and a run with a non-empty list
is **partial**. A cluster contributes at most one line however many of the three apply to it, so a
partly-checked cluster that also carries a limitation reads as one sentence with two reasons rather
than as two separate gaps. The denominator is the stream's roster minus that cluster's
`checks_not_applicable`, which is what keeps a check the cluster's shape forbids from reading as a
check nobody ran.

Routing the roster shortfall through `coverage_gaps` rather than gating it separately is the whole
economy of the change. Everything below already keys off `partial`, so an incomplete run inherits
the full set of withheld conclusions — no resolved claims, no stale-closes, no ledger closure, not
`[SILENT]` — without a second mechanism to keep in step with the first.

The reason this needs a name is that the whole ledger rests on one inference: _a finding that was in
yesterday's document and is not in today's has been fixed._ That inference is sound only over a
fleet the audit actually read. Absence of evidence from a cluster nobody could reach is not evidence
of absence, and acting on it does real damage — it announces fixes that did not happen, and it
closes the pull request that was going to make them happen.

So over a partial run the harness withholds exactly the conclusions that depend on complete
coverage, and nothing else:

- `resolved` is reported as `0` and no resolved-delta is posted. Findings that genuinely were fixed
  are simply reported the next time the fleet is fully readable.
- No remediation PR is stale-closed. Every open fix survives to the next complete run.
- Zero findings does not close the ledger. `status` is still `CLEAN` — the audit found nothing, and
  saying otherwise would be its own lie — but the issue stays open and gains a comment naming the
  gaps, so the stream self-heals the day the unreadable clusters come back.
- The run is never `[SILENT]`: `finish` returns `silent_ok: false` (§7.5). "I found nothing" and "I
  could not look" must not arrive in chat as the same silence.

What a gap does **not** do is suppress the report. Findings from the clusters that _were_ read are
published normally, and new fixes are still proposed for them. A partial audit is a partial audit,
not a failed one.

`partial` is `true` if and only if `coverage_gaps` is non-empty, on both `finish` branches. The
tempting generalisation — also raising it when the body budget (§7.1) dropped findings from the
description — was implemented and then removed, because the two are not the same kind of incomplete
and the flag has one job. A coverage gap means the audit did not look, which is precisely why it
suppresses the resolved count. Truncation means it looked, found everything, counted it all in the
title, and could not print the tail; resolution accounting is untouched, because the delta block
already carries only the ids the body rendered (§2). Folding them together produced
`partial: true` with an empty `coverage_gaps` — a flag five SOPs instruct the agent to explain to a
human, with nothing to explain it with. Truncation is surfaced where it belongs: a line in the body
naming the count it dropped, and a `WARNING` in the run log.

### 7.5 `silent_ok`, and who is listening

The silence rule was correct and the agent still got it wrong, which is the interesting part. Stated
in full it is a four-clause conjunction over four fields on two `finish` branches, and the SOPs
stated it four different ways because each one had a different set of cases worth spelling out. On
2026-08-03 a dispatched `security-patch-orchestrator` run rewrote its ledger over a fleet where two
of three clusters were `6/10 ⚠`, concluded `[SILENT]`, and suppressed its own delivery. The operator
who had asked for it got a card that said "successfully updated the existing ledger issue" and no
URL. An earlier run of the same job, the same morning, had got the same rule right. A rule an agent
applies correctly most of the time is a rule the harness should be applying.

So `finish` computes it and returns `silent_ok` on both branches. It is `true` only when the run
moved nothing an operator needs to hear about — nothing new, nothing resolved, no coverage gap, no
remediation PR opened or closed — and it is computed from the numbers `finish` is about to _report_,
not the ones it privately knows. A partial run reports `resolved: 0`; an unreadable previous body
makes the delta unknowable and reports `new: 0`. `silent_ok` follows what was published, so the flag
and the report can never disagree. The PR counters are in the conjunction because opening a fix is
news even on a run that found nothing new: the ids were already in the ledger, so `new` is zero,
while a pull request now exists that did not before.

`silent_ok` is the **scheduled** verdict. It answers "would a channel want this?", and it has no way
to know a person is waiting: `finish` sees a findings document, not the provenance of the run. So
the second half of the rule lives with the agent and cannot be moved into the harness — **an
on-demand run is never silent.** A run dispatched from a kanban card, from chat, or from
`cronjob(action='run')` reports its outcome and its ledger URL whatever `silent_ok` says. Two places
say so: every SOP's close section, and the Platform Agent's `AGENTS.md`, which additionally requires
the dispatching session to relay the run's report on the card — from the `response` the dispatch
returns, or from the `output_file` it names when the run answers `[SILENT]` — because the card
summary is what reaches Slack, and the worker's own transcript reaches nothing.

## 8. Labels

`ensure_labels` gains two entries; the rest are unchanged.

| Label                | Applies to  | Purpose                                      |
| -------------------- | ----------- | -------------------------------------------- |
| `agent:audit`        | issue + PRs | Everything this skill owns                   |
| `audit:<audit-id>`   | issue + PRs | Stream identity; how the ledger is found     |
| `audit:remediation`  | PRs only    | Distinguishes a fix PR from the ledger issue |
| `audit:stale-closed` | PRs only    | The harness closed this, not a human (§3.3)  |
| `severity:*`         | issue + PRs | Highest live severity, mutually exclusive    |

## 9. Red lines (carried forward, plus new)

Unchanged: read-only against clusters; never `git add .` or `-A`; never force-push a protected
branch; never hand-write a body, title, commit message, or timestamp.

Amended: a `manifest` path **should** exist under the workspace before publishing, and the agent is
still told to write it — but a missing one degrades that finding to `manual` rather than killing the
run (§6, `remediate`).

New:

- **Never open a second ledger issue for a stream.** The agent never calls `gh issue create`;
  `finish` owns it.
- **Never open a remediation PR for a non-`manifest` finding.**
- **Never reopen a merged remediation PR.** A persisting finding gets a comment and a ledger state,
  not a resurrection.
- **Never delete a remediation branch on close.**
- **Never touch a file outside the checkout.** `remediation.path` is validated as a string during
  `finish` — repo-relative, no `..`, no glob metacharacter, no leading `:` — but a string check is
  all that can run at validation time, because the harness does not yet know where the clone is. On
  a real filesystem an unimpeachable relative path escapes the moment a directory component is a
  symlink: `manifests/vendor/x.yaml` is beyond reproach until `manifests/vendor` points at `/etc`,
  and then the existence check passes, the snapshot reads `/etc/x.yaml`, and its contents are
  committed to a public pull request. So every path is re-resolved against the checkout root before
  it is read or staged, under two independent tests — **no component may be a symlink**, and the
  fully resolved path must sit under the fully resolved root. Either alone has a hole: the walk
  misses a root reached through a link, and the resolve-and-compare accepts a link whose target is
  inside the repo today and moves tomorrow. Nothing is read from a path that fails either test: the
  finding degrades to `manual` under §6's missing-manifest rule, the run logs a `SECURITY:` line
  naming the path, and no pull request can open for it while the path stays uncontained.
- **Never report a cluster the audit could not read as clean.** An unreadable cluster goes in
  `scope.skipped`, a cluster read with a check missing gets a `limitations` note, and either way the
  run is partial (§7.4). Reporting an all-clear for a cluster nobody looked at is the one failure
  mode that makes the whole ledger untrustworthy.
- **Never report a cluster the audit did not check as clean.** `checks_run` records what actually
  ran, per cluster, and anything short of the checks that apply to it is a gap (§7.2). Naming a check
  that did not run, or parking one in `checks_not_applicable` that the cluster's shape does not
  actually forbid, are the two ways left to defeat every protection above, which is why each SOP says
  so where the fields are written.
- **Never put a credential in `evidence.excerpt`.** No Secret `data:` or `stringData:` block, no
  token, no password, no private key. The SOPs say this to the model; `audit_report.py` also
  enforces it on the way out, replacing a `data:` block, a secret-named field, a self-identifying
  token prefix, a PEM header, or an `Authorization:` value with `[redacted by audit_report.py]`. The
  backstop exists because the ledger is a public artefact and one leaked excerpt cannot be recalled;
  it is a backstop and not a licence, since it cannot recognise bare base64.

## 10. Work breakdown

Sequenced so each phase is independently reviewable. One PR per phase.

**Phase 1 — schema and pure helpers.** Add `recommendation`, the finding-id charset rule (§2), and
scope disjointness with `limitations` (§7.2) to the validator. Add group computation, branch naming,
state derivation, size budgeting, and `/remediate` command parsing as pure functions. Extend the
existing test module. No I/O, no behaviour change yet. The one exception to "no behaviour change" is
the `github-issue-resolver` exclusion (§13 Q3): it lands here so that it is never absent while a
ledger issue exists.

**Phase 2 — the ledger issue.** Port `find_existing_pr` → `find_existing_issue`, `render_body` →
`render_issue_body`, and the create/edit/comment/close paths from `gh pr` to `gh issue`. Delete the
report branch, the `--allow-empty` commit, and the force-push from `finish`. At the end of this phase
the skill publishes issues and opens no PRs at all.

**Phase 3 — remediation PRs.** The `remediate` subcommand, auto-promotion, the reconciliation query,
stale-close, and the `pr-merged-persists` comment.

**Phase 4 — migration and docs.** Rename `audit_pr.py` → `audit_report.py` and the test module to
match. Rewrite `SKILL.md`, the five governance SOPs, and the site pages — including the four stale
GitHub App permission lines of §13 Q2. There is no legacy reconciliation step (§3.4).

## 11. Files touched

Twenty-two existing files reference the audit PR path today:

```
agents/platform/CAPABILITIES.md
agents/platform/AGENTS.md                                   (cron dispatch and handover)
agents/platform/SOUL.md                                     (§3.2 — GitOps write paths; §0 — card summaries)
agents/platform/cron/jobs.json
agents/platform/governance/compliance_audit_sop.md
agents/platform/governance/fleet_consistency_drift_sop.md
agents/platform/governance/fleet_wide_cost_analysis_sop.md
agents/platform/governance/obtainability_audit_sop.md
agents/platform/governance/security_patch_orchestrator_sop.md
agents/platform/skills/fleet-audit/SKILL.md
agents/platform/skills/fleet-audit/scripts/audit_pr.py      → audit_report.py
agents/platform/skills/fleet-audit/scripts/test_audit_pr.py → test_audit_report.py
docs/README.md
docs/site/src/content/docs/concepts/autonomous-watchdogs.md
docs/site/src/content/docs/concepts/declarative-workflow.md   (stale App permissions)
docs/site/src/content/docs/concepts/governance-sops.md
docs/site/src/content/docs/overview/architecture.mdx
docs/site/src/content/docs/overview/proactive-autonomy.md
docs/site/src/content/docs/reference/cron-jobs.md
docs/site/src/content/docs/reference/security-and-iam.md
docs/site/src/content/docs/skills/index.mdx
scripts/generate_docs.py
```

Four more are prerequisites of the ledger rather than references to the PR path:

```
agents/platform/skills/github-issue-resolver/SKILL.md            (§13 Q3 — red line gains `agent:audit`)
agents/platform/skills/github-issue-resolver/scripts/resolver.py (§13 Q3 — poll query gains `-label:agent:audit`)
docs/site/src/content/docs/deploy/token-minter.md                (§13 Q2 — stale App permissions, two places)
docs/site/src/content/docs/install/prerequisites.md              (§13 Q2 — stale App permissions)
```

## 12. Testing

The module this design started from was 60 tests over 980 lines; most pure-helper coverage ported
unchanged. As shipped it is **346**. New cases:

- `recommendation` validation: each sub-field missing, empty, wrong type.
- Finding-id charset: an id containing `:`, a space, `..`, or `*`, one ending `.lock`, one starting
  or ending in `.`, `_`, or `-`, and one over 100 characters are each rejected; the SOP-generated
  shape is accepted.
- Scope: `clusters` and `skipped` must be disjoint; a finding naming a skipped cluster is rejected; a
  cluster with `limitations` is accepted and renders the extra column, and the column is absent when
  no cluster carries one.
- `checks_run`: absent is rejected; a non-list is rejected; a bare slug in place of an entry object
  is rejected; an unknown slug and a duplicate are each rejected; empty is rejected without a
  `limitations` string and accepted with one; the full roster validates and is complete coverage, a
  subset validates and is not; the rejection fires for every stream against its own roster; and the
  roster in `AUDITS` matches the slugs its SOP's `####` headings declare.
- **No rejection names a roster slug**, on any of the five paths that reject the field. This is the
  test that would have caught the 2026-08-03 answer-key inversion, and it is written as the negative
  of the test it replaces. The `checks_not_applicable` rejections are held to the same rule.
- `checks_not_applicable`: absent is accepted; a non-list, a bare slug in place of an entry object,
  an unknown slug, a duplicate, and a slug that also appears in that cluster's `checks_run` are each
  rejected; a `reason` that is absent, empty, or under sixteen characters is rejected, with `"N/A"`
  and `"not applicable"` named as the cases the length bound exists to stop. A declared check leaves
  the coverage denominator: a cluster running every applicable check is not a gap and does not set
  `partial`, the scope table renders `n/applicable (m n/a)`, and the exclusions render with their
  reasons in the evidence section.
- `command`: absent, empty, shorter than eight characters, over `MAX_COMMAND_CHARS`, a call back into
  `audit_report.py`, an `echo`/`cat`/`printf`/`python3 -c`/`true`, and prose naming no inspection
  binary are each rejected; a real invocation is accepted; one command backing three checks is
  accepted; and the accepted commands appear in the rendered body.
- `start` prints `checks` and `sop` for every stream, and `AuditSpec.sop` agrees with an independently
  spelled-out filename map and with a file that exists.
- Zero findings with a coverage gap and no ledger opens one, titled `coverage incomplete` and
  labelled `agent:audit` + `audit:<id>`; zero findings with complete coverage and no ledger opens
  nothing.
- Grouping: disjoint paths, two findings one path, transitive union across three findings.
- Promotion eligibility: critical+manifest auto; critical+gcloud not; major+manifest only on request;
  already-has-PR is a no-op in every state; the sixth eligible critical in a run is withheld and
  named in the ledger, while six explicit `/remediate` targets all open.
- Command parsing: `/remediate <id>`, `/remediate all`, unknown id, non-manifest id, the command
  appearing inside a fenced code block (must not match), and a command from an `authorAssociation`
  of `NONE` or `CONTRIBUTOR` (refused, replied to once).
- State derivation across all **seven** rows of the §4 table, including `pr-merged-persists` and
  `withdrawn`; that every state has a distinct label, asserted as a set equality against
  `STATE_LABELS` so a new state cannot be added without one; and that `withdrawn` and `refused` do
  not share a label, since rendering them alike is the specific mistake §4 exists to prevent.
- Idempotency markers: a merged PR already carrying `audit-persists` gets no second comment; a ledger
  already carrying `audit-refused` for a comment node id does not re-refuse it; a different node id
  for the same finding does.
- Clean run closes the issue and every open remediation PR.
- `--dry-run` issues no command through the harness runner at all (assert the recorder is empty),
  prints every pull request body it would open, separated from the ledger body by a machine-findable
  line so a size check can measure each one against the limit rather than the concatenation, and
  resolves `remediation.path` against the workspace clone rather than the process's working
  directory — the one where a dry run run from the wrong place would have reported every manifest as
  written when none were, or as missing when all were.

**Publication cases.** The suite's one structural blind spot: every other assertion checked either
the _arguments_ handed to `gh` or the _return value_ of a renderer, and nothing checked the wire
between them. Replacing the temp-file writer's payload with an empty string — blanking the ledger,
every comment and every pull request — left the whole suite green. The recorder now reads each
call's `--body-file` (or `-F`) at call time, before the harness unlinks it, and:

- the created ledger carries the findings section and the hidden `audit-findings` block;
- a refreshed ledger carries this run's findings and not the last run's;
- the delta comment names both the new and the resolved id;
- the clean comment is published, not merely rendered;
- a promoted pull request carries its file list, its `Part of #N` link, and its own hidden block;
- and, as a blanket rule, no body published by any run is empty.

**Command-syntax cases.** Both ways of getting `/remediate` wrong used to produce exactly the
observable behaviour of an audit that had not run yet, so the requester waited and asked again the
same wrong way. A mid-sentence mention and a bare `/remediate` are each answered once, with the
syntax and the promotable ids; a mistyped id and a non-`manifest` target likewise name the ids that
would have worked; a mention inside a code span is not an attempt; a mention from an author without
write access is left alone; and no comment the harness itself writes reads as a request — which is
what keeps it from answering its own replies on every run.

**Answered-anyway cases.** The two ways a request can be dropped without anybody noticing, because
in both the harness is doing something else at the time:

- A **clean run** answers every unanswered `/remediate` before it closes the ledger — asserted on
  the published comment bodies, not on the render, since the bug being guarded against is a comment
  that is composed and then never posted. Covered: a request answered on a closing run, one answered
  on a partial run that keeps the ledger open, one already carrying an `audit-acked` or
  `audit-refused` marker that is not answered twice, and a `/remediate` inside a code fence that is
  not answered at all.
- The `remediate` subcommand opens pull requests for **only** the ids it was given, asserted on the
  paths staged rather than on branch names — branch names are derived from the group's path set, so
  an assertion keyed on a finding id would fail for the wrong reason and be "fixed" by weakening it.

**Size-cap cases** (§7.1), asserting on rendered length rather than on shape. Every one of them
measures against the literal 65,536 GitHub enforces, never against `MAX_BODY_CHARS`: a test that
asserts a body fits under the harness's own belief about the limit passes just as happily when the
belief is wrong, so raising the constant to 200,000 would keep them all green while every publish
422s. One test — and only one — asserts the constant equals the literal.

- A run of 250 findings renders a body at or under the limit.
- The hidden delta block contains exactly the ids the body rendered — no more, no fewer.
- 5 critical plus 300 minor findings keeps all 5 criticals.
- 10 findings render untruncated, with no "omitted" notice and no trimmed command.
- The clean-run comment stays under the limit with 900 skipped clusters.
- A truncated body reports `partial: false` with empty `coverage_gaps`, and warns in the log —
  truncation is not a coverage gap (§7.4).
- `partial == bool(coverage_gaps)` over both `finish` branches and every way a gap arises: clean and
  complete, clean over a `skipped` entry, findings over a `skipped` entry, findings over a cluster
  carrying only a `limitations` note, and a cluster whose `checks_run` is short of its applicable
  checks with no limitation at all. A cluster that is both short and limited yields one gap line, not two.
- The clean-run comment announces a close only when coverage is complete. Over a gap it says the
  ledger stays open, and it treats a `limitations` note as a gap — the earlier version rendered
  `scope.skipped` alone and posted "closed as completed" onto an issue that stayed open.

**Failure-path cases.** The suite this one grew from had **zero** of these: its mock command recorder
returned exit 0 unconditionally, so not one test exercised a failing `gh` or `git` call. That was not
a gap in coverage of unlikely code, it hid a live defect — `find_existing_issue` and
`find_existing_pr` returned `(None, None)` on transport failure, which makes a GitHub outage
indistinguishable from "no issue exists". The run then opens a **duplicate ledger**, or, on a clean
run, prints `CLEAN` having closed nothing. So:

- The recorder gains fault injection: a per-command exit code, stderr, and payload.
- A failing `gh issue list` must not be read as "no ledger exists".
- A failing `gh pr list` must not be read as "no PR on this branch" and must not re-promote.
- A clean run that cannot reach GitHub does not print `CLEAN`.

**Coverage-gap cases** (§7.4): a `scope.skipped` entry, a cluster `limitations` note, and a
`checks_run` short of the applicable checks each set `partial`, while a shortfall fully accounted
for by `checks_not_applicable` does not; a partial clean run leaves the ledger open, posts the gap
comment, closes no PR, and reports `resolved: 0`; a complete clean run does all four of the opposite
things.

**Silence cases** (§7.5): `silent_ok` is `true` on a quiet clean run and a quiet `UPDATED` run, and
`false` whenever the run reported a new finding, a resolved finding, a coverage gap, or a
remediation PR opened or closed — asserted on both `finish` branches, since each prints its own
JSON. Three prose tests pin the handover the flag cannot cover on its own: the `AGENTS.md` cron
bullet names both `kanban_complete` and `[SILENT]`, `SOUL.md` requires the artifact URL in the card
summary before its first numbered section, and every one of the five SOPs contains both `silent_ok`
and "on-demand".

**Workspace cases.** Exactly **one** of these runs real git against a real bare origin rather than
the recorded runner, and it is the one whose defect is invisible to a mock: `ensure_workspace`
executes `git clean -fd`, and a mock happily records the command without deleting anything, so the
untracked manifest the fixture wrote is still on disk and the assertion passes on code that would
wipe the tree in production. That test asserts an untracked manifest written between `start` and
`finish` survives the reattach. Its two companions — that `reset=True` does remove it, and that
`finish` issues neither `git clean` nor `git reset --hard` — are assertions about which commands
were issued, which the recorder answers correctly and more cheaply.
This is worth the cost of a real repository in the suite: the bug it guards against silently emptied
Tier 2 — every manifest the audit wrote would have been deleted moments before the harness looked
for it, and every finding would have been published as "the fix was named but never written".

**Repo-resolution cases** (§13 Q1): the `Git Repo:` line parsed from `https://`, `git@`, and bare
`owner/name` forms; the literal `None` the operator writes for an unset CR field; a missing
`SETTINGS.md` falling back to the git remote; SETTINGS winning when both are present; and an error
that names both sources when neither works. Plus ordering: the repo is resolved before the token is
minted, the token is minted for the resolved repo, and both happen before the clone.

**One hazard the suite has to defend against itself.** Two test classes were both named
`TestStaleClose`. Python rebinds the name at import, long before `unittest` collects anything, so the
first class simply ceased to exist and its four tests never ran — while the suite reported them as
passing, because a test that does not exist cannot fail. Nothing in `unittest`, and nothing in the
count printed at the end of a run, distinguishes "four tests passed" from "four tests were shadowed
away". The classes are now `TestStaleCloseEligibility` and `TestStaleCloseLabelling`, and the
docstring on each says why: any new stale-close class needs its own name. A duplicate class name is
the one test defect that hides itself.

Plus the existing gates: `make docs-generate`, `make docs-check`, `make validate`, `prettier`,
`astro build`, and a Docker build to prove in-image script paths.

## 13. Questions, resolved

**Q1. Does the ledger issue live in the GitOps repo?** `resolve_repo()` derives it from the working
directory's `origin`, which is the GitOps repo — correct for the PRs, but a platform admin may expect
audit issues in an ops/tracking repo instead. If they must differ, `start` needs an explicit
issue-repo argument and the App token needs scope on both.

_Resolved: the GitOps repo._ No new argument, no second token scope, no second place to look.

The divergence this question surfaced — `audit_report.py` deriving the repo from the working
directory's `origin` remote while `github-issue-resolver/scripts/resolver.py` derives it from the
`Git Repo:` line of `/opt/data/SETTINGS.md` — was first recorded as cosmetic and deferred. It was
not cosmetic. Reading the remote requires a working tree, the audit cron does not start in one, and
the clone that would create one needs a repo-scoped token that cannot be minted until the repo is
known. The old resolution was circular and would have failed on the first real run. `resolve_repo()`
now reads `SETTINGS.md` first and falls back to the remote, so the two skills agree by construction
and the repo is knowable before anything has been cloned (§6, `start`).

**Q2. Does the App token already carry `issues: write`?** `github-issue-resolver/scripts/resolver.py`
creates labels, comments, and closes issues with the same token, so issue write is established — but
issue _creation_ has not been exercised. Confirm before Phase 2.

_Resolved: already granted._ The design guessed; source settles it.
`k8s-operator/config/integrations/github/configmap.yaml.template:19` puts `issues: 'write'` in the
`platform-agent-scope` rule, and that directory's `README.md:24` names `Issues: Read & write` among
the App's permissions. Nothing to add.

The published documentation, however, was **stale in four places**, each listing only `contents` and
`pull_requests`: two in `docs/site/src/content/docs/deploy/token-minter.md` (the App-creation step
and the scope description), one in `docs/site/src/content/docs/install/prerequisites.md`, and one in
`docs/site/src/content/docs/concepts/declarative-workflow.md`. An operator who followed them
created a GitHub App without issue permission, which makes Minty's scope request unsatisfiable and
fails the ledger at runtime with a 403 — a class of failure the operator cannot debug from the
documentation that caused it. Corrected as part of this change.

**Q3. Interaction with `github-issue-resolver`.** That skill autonomously polls, claims, and resolves
open issues. It must be taught to skip `agent:audit` issues, or it will try to "resolve" every ledger
the audits publish. This is a hard prerequisite, not a follow-up.

_Resolved: yes, and it is a one-token fix._ The `resolver.py` poll query filtered only `status:in-progress`,
`status:escalation-needed`, `agent:ignore`, and `status:resolved`. A ledger issue matched that poll
query on sight: it would be claimed, investigated, and closed as `status:resolved`, so the resolver
would silently eat every ledger the audits publish. `-label:agent:audit` is added to the poll query,
and `agent:audit` is added to that skill's inviolable red line so the exclusion survives a later
rewrite of the query. It lands in Phase 1 (§10), so the exclusion is never absent while ledger issues
exist.

**Q4. Volume ceiling.** Hybrid gating bounds auto-opened PRs to critical manifest findings, but a
genuinely bad fleet day could still open many at once. Consider a per-run cap with the withheld set
named in the ledger.

_Resolved: auto-promotion is capped at five PRs per `finish` run._ Withheld findings are named in the
ledger as awaiting `/remediate`, so nothing is lost, only deferred to a human's judgement about which
five matter first. An explicit `/remediate` is **uncapped** — a human asked for it, and a cap there
would just make them ask again.

**Q5. Who may issue `/remediate`?** §3.1 says what may be promoted but not who may ask. An
unqualified comment trigger is an unauthenticated write path: on a public or widely-collaborated
repo, a comment from a stranger would open branches and PRs in the GitOps repo.

_Resolved: honour the command only from an author whose `authorAssociation` is `OWNER`, `MEMBER`, or
`COLLABORATOR`._ Anyone else gets a single reply saying the command requires write access, recorded
by the `audit-refused` marker of §3.1 so they are not told twice. `gh issue view --json comments`
exposes `authorAssociation` on each comment, so this costs no extra API call.

## 14. Accepted risks

Two findings from the adversarial review of this feature were deliberately not fixed. They are
recorded here rather than dropped, because an unrecorded decision not to act is indistinguishable
from not having noticed.

**Mutation coverage is not measured, and the suite is not known to be strong.** A 71-mutation corpus
run against the suite as it stood at 167 tests left 40 mutants alive — a 56% survival rate. Notable
survivors: inverting the severity label on a finding, closing the pull requests whose findings still
reproduce rather than the resolved ones, and dropping the withheld-findings notice from the ledger.
The single largest cause was the missing publication seam — no test opened a `--body-file` and read
it — which is fixed (`Recorder.bodies_for`, `TestPublishedBodies`), and re-running the blanking
mutation now fails eight tests where it previously failed none. The rest of the corpus has not been
re-run, and no mutation gate is wired into CI.

_Why it is accepted:_ a mutation harness is a testing-infrastructure project of its own, it needs a
tool the container does not have and cannot fetch (there is no network in the build), and its output
is a backlog of individually cheap test cases rather than a defect. The three named survivors are
each now covered by a direct test. _What it costs:_ the suite's true strength remains an estimate.
_What would change it:_ if a defect ships that a mutation would plainly have caught, wire the gate
in before fixing the defect.

**A dead cron is indistinguishable from a healthy fleet.** A clean stream with no pre-existing
ledger publishes no artifact at all and reports `[SILENT]`. That is byte-for-byte the same
observable state as a cron that never fired, a pod that crash-looped, or a CronJob somebody
suspended. Silence means "nothing to say" and "nothing said anything" at once, and only the first is
good news.

_Why it is accepted:_ every fix crosses a boundary this design deliberately holds. A heartbeat issue
per stream reintroduces exactly the always-open, always-noisy artifact §1 exists to remove; a
liveness check needs state outside GitHub, and §2's whole premise is that the branch name is the
only join key; a metrics export needs a monitoring stack this repository does not own. _What it
costs:_ a stream that stops running is invisible until somebody notices the absence, and nobody
notices absence. _What would change it:_ the right home for this is CronJob-level alerting in the
operator — `PlatformAgent` already reconciles the schedule and knows the expected cadence, so it can
see a missed run without the audit publishing anything. Track it there, not here.
