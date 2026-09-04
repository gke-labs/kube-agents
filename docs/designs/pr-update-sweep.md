# Getting an Agent's Own Pull Request Unstuck

> **STATUS — design of record; implemented.** The `pr_updates` sweep, the two forge reads it needs,
> the attempt bounds, and the `update-pr` skill all ship as described. §6 records what was
> deliberately left out.

**Scope:** How a pull request the agent opened, and which now cannot merge, gets worked on again
without a human asking. **Owns:** the `pr_updates` sweep in `github_scan_gate.py`, the
`conflict_state` and `failing_checks` forge operations, the `agent-updated` marker and its budget,
and the `update-pr` skill. The trigger machinery underneath — the watcher job, the provider
protocol, the marker scheme — belongs to
[`pr-comment-conversation.md`](pr-comment-conversation.md), and this document extends it rather
than restating it.

---

## 1. The problem

[`pr-comment-conversation.md`](pr-comment-conversation.md) gave the agent a way to be woken by a
reviewer. It did not give it a way to be woken by the branch. A pull request the agent opened goes
stale on its own, without anybody saying anything: `main` moves and the branch conflicts, or a
presubmit that was green when it was pushed goes red on a dependency bump. Nothing watches for
either. The pull request sits there unmergeable until a human notices and asks.

Three conditions block a merge, and a reviewer's unanswered comment — the one already covered — is
only one of them.

## 2. Trigger: a third sweep, not a third job

`github-repo-watcher` already runs every ten minutes as a `no_agent` job, resolving the managed
repositories and preflighting `gh` once per sweep. Adding `pr_updates` to its `SWEEPS` registry
costs three API calls per open agent pull request per tick — one for the merge state, two for CI,
which reports in two registers — and no model turn; adding a cron entry would have cost a turn per
tick to discover there was nothing to do, which is the defect that retired `github-issue-resolver`
(`pr-comment-conversation.md` §2).

That bill scales with how many pull requests the agent has open, not with how many repositories it
watches: the sweep walks every repository `get_managed_github_repos()` returns, and a repository
with no agent-authored pull requests costs one list call. That call is its own — sweeps share the
tick's claim set and card budget, not a provider or a read — so the fixed cost of adding this sweep
is a `gh` preflight, a viewer lookup, and one list call per repository, each duplicating what the
comment sweep does a moment later. Deduplicating them would mean a read cache spanning the tick,
which is a larger change than the calls are worth at this volume.

The conditions are read deterministically, so the gate needs no reasoning:

- **Conflicted** — `mergeable` is false, or `mergeable_state` is `dirty`, on `GET /repos/{repo}/pulls/{n}`.
- **Red** — any completed check run whose conclusion is failing, or any commit status in `failure`
  or `error`, on the head sha.

Neither read is one the model could do cheaply by hand, which is why both live behind the provider
protocol in `forge.py` rather than in the skill.

### Two CI registers, not one

GitHub reports CI in two places that do not overlap. `/commits/{sha}/check-runs` carries the Checks
API — GitHub Actions and most Apps. `/commits/{sha}/status` carries the older commit statuses, and
**Prow reports only there**. This repository is Prow-gated, so a sweep that read check runs alone
would call a pull request green whose merge gate was red. `failing_checks` reads both and flattens
them into one `CheckRun` shape with a `register` field saying which it came from, because the two
need different things to read their logs.

Both endpoints return JSON **objects**, not arrays, and `gh api --paginate` concatenates objects
into a stream `json.loads` rejects. Both reads are therefore single-page at `per_page=100`, which is
a real ceiling: a head commit with more than a hundred checks reports a truncated list. Nothing in
this repository comes close, and the alternative was a paginator for a case that does not exist.

## 3. Ordering: conflicts, then reviewers, then CI

The skill works three stages in that order and pushes one commit per stage.

The order is forced by what each stage would otherwise be operating on. A change a reviewer asked
for, applied to a branch that will not merge, is a change nobody can take. CI on a conflicted branch
is testing a tree that will never exist. And each stage's own fix changes what the next one sees —
resolving a conflict is exactly the kind of edit that reds a check — so the CI stage re-reads the
checks after the first two have landed rather than working from the list the card carried.

One commit per stage rather than one per run, because a reviewer returning to the branch needs the
merge, the change to their file, and the lint fix separated: those three deserve different amounts
of their attention.

### Merge, not rebase

Stage 1 merges `origin/<base>` into the head branch. A rebase would be tidier history and is the
wrong trade here: it rewrites the commits under review, which detaches every inline review comment
from the line it was written against. An extra merge commit on a branch that is about to be
squash-merged costs nothing by comparison.

### Stage 2 delegates rather than reimplements

Reviewer comments already have a skill, a policy layer, and a set of bounds — who may direct the
agent, what counts as addressing it, how many refusals one pull request may draw. Stage 2 is
therefore `pr-conversation`'s procedure run in full, from inside the update run's own workspace
lease. A second implementation of that gate would be a second copy of it that drifts, and a budget
each caller kept its own copy of would be a budget the second caller could spend again.

This creates the one piece of cross-sweep state in the watcher: `pr_updates` runs first and **claims**
the pull requests it cards, and `pr_comments` skips anything claimed. Without the claim, a pull
request that is both conflicted and has an unanswered comment gets two workers pushing to one
branch. Nothing is delayed by it while the update worker lives, because answering those comments is
its own stage 2. The claim is one mutable set of `(repo, number)` pairs threaded through `main`'s
loop and does not survive the tick. It has to be repo-qualified, because one tick sweeps every
managed repository: a bare number would silence `repoB#7`'s reviewer because `repoA#7` was carded,
and it would do it without writing anything anywhere, since the comment sweep's skip is silent.

Where it does cost something is a worker that dies before it records: the sweep re-claims the pull
request on every tick until the card's idempotency key rolls over and a fresh worker is dispatched,
and the reviewer waits that long. §4 is where that bound is set, and it is set by this rather than
by the retry interval it would otherwise have wanted.

## 4. Making the loop terminate

This is the part that is genuinely new, and it is new because this is the first thing in the harness
that **pushes commits without being asked**.

Every other loop here is anchored to a human act. A comment is answered once; the marker naming that
comment closes it; the reviewer is the one who decides whether to say something else. An update run
has no such anchor. It is triggered by the state of the branch, and its own fix commit changes that
state — so a fix that does not work moves the head, the next tick sees a new sha, and the agent
tries again. Forever, on a pull request nobody asked it to touch.

Two bounds, both read back off the thread rather than tracked in a database, for the reason
`pr-comment-conversation.md` §5 gives: the sweep and the worker are separate processes on separate
schedules, and the thread is the only state both can see.

`PR_AGENT_MAX_PER_TICK` (default 3) is a third bound, but it is not this sweep's: it is the tick's,
and both pull-request sweeps draw their cards from the one allowance. Applying it per sweep would
have doubled the ceiling the knob's name states, silently, and on this path a card is a workspace
lease and a push rather than a comment. `pr_updates` runs first, so under pressure the unmergeable
branches are the ones that get the turns — the same ordering, and the same reason, as the claim in
§3. The cap bounds a tick, not a pull request, so on its own it slows the loop rather than ending
it; the two bounds below are what terminate it.

**One attempt per head commit.** `<!-- agent-updated:<sha> -->` in a comment the agent authored
means "this tip has been worked", whatever the outcome. It is keyed on a commit sha rather than a
comment node id — the only marker of the three that is — because what triggered the run was that
commit rather than anything anyone said. A run that could not fix what it found still writes one,
so it is not repeated every ten minutes; what it could not do is in the comment the marker is
attached to.

**`PR_AGENT_MAX_UPDATE_ATTEMPTS` markers in total**, default 5. This is what makes the loop
terminate rather than merely slow down: the per-tip bound never binds on a branch that is being
pushed to, since each failed fix mints a fresh tip. Five covers the ordinary shape — resolve a
conflict, fix the lint it exposed, fix the test that lint fixed wrong — and stops short of the
number at which a reviewer would rather nobody had tried.

The card's idempotency key carries the head sha and an hour bucket, the same one the other two
sweeps use. The sha already does most of the work, so the bucket only ever covers a run that ended
without recording an attempt — a board hiccup, a turn reaped as a protocol violation. A day would
have been the better interval for retrying _that_, since the pull request is one nobody can merge
either way. It is an hour because of the claim in §3: the bucket is also how long a dead worker can
keep a reviewer waiting for an answer, and a day of silence on a maintainer's comment is not worth
a cheaper retry.

The marker is written by `update_pr.py record`, never by the model, and the sha it names is resolved
against the pull request's own commits first. A mistyped sha would produce a marker matching
nothing, which is the same runaway by a slower road.

### Both bounds count markers, so a run has to reach `record`

Neither bound is a counter the sweep increments. Both are read off the thread, and only `record`
writes there — so a run that pushes a commit and then exits before posting has moved the tip
without recording anything. The new tip is not in the marker set, the set has not grown, and the
key carries the sha rather than only the hour, so it mints straight away. Nothing binds, and the
claim in §3 means the reviewer on that branch is not answered either.

`record` therefore writes the marker on every refusal that comes after the push — a `--pushed` sha
that predates the run or will not resolve, a commit nobody declared, an unreadable body — rather
than exiting with the thread untouched. The comment says what went wrong, which is the same thing
§4 above asks of a run that could not fix what it found. `test_update_pr.py` walks the refusal
paths and asserts it, because "every" is the kind of claim a later branch quietly falsifies.

One refusal is outside that rule by construction: an `--attempted-sha` that does not resolve. It is
the anchor the others are measured against, so until it resolves there is no way to tell whether
the branch moved, and no sha to write a marker for.

What that leaves is a turn reaped or crashed before `record` is invoked at all. It stays unbounded,
and the reason it is not fixed the obvious way — writing the marker before the work — is that doing
so is exactly what `updated_head_shas` decided against: an attempt that is counted before it is
made lets one crashed turn park a pull request for good with nothing said to anyone. Which of the
two is worse depends on how often turns are reaped here, which nothing measures yet.

## 5. What the worker may claim

`record` requires either `--pushed <sha>` or `--no-change`, and checks the first. A pushed sha must
be on the pull request **and** must come after the tip the run started from: every commit the agent
ever made is on that branch, including the one that opened it, so membership alone would pass for a
run that changed nothing. This is `pr_conversation.reply`'s `--verify-commit` rule, applied to a
different anchor — that one compares against the request's timestamp, this one against the starting
tip, because an update run is anchored to a commit rather than to a comment.

The reason is the same in both places. The marker closes the tip for good, so a comment claiming the
conflict was resolved, on a branch that still conflicts, is worse than no comment: the next signal
anybody gets is the merge failing.

## 6. Deliberately not built

**Re-running CI and waiting.** The checks outlast the turn. The next sweep sees the result — healthy
means no card, still red means one more attempt from the budget — so waiting would buy a faster
answer at the cost of a turn spent asleep.

**Fixing a check whose logs cannot be read.** A `status` row from an external system may point at a
dashboard the agent has no credential for. The skill reports which check it was and why it could not
see inside, rather than pushing a speculative fix.

**Escalating to chat.** A pull request that spends its attempt budget stops, and the only record is
the last attempt's own comment — which says what that run could not do, not that the budget is now
gone. The sweep names it on stderr rather than in chat, because a line every ten minutes about a
pull request nobody is going to touch is noise. Whether it should reach a room is the same open
question §6 of
[`pr-comment-conversation.md`](pr-comment-conversation.md) has for stale requests, and it should be
answered once for both rather than twice differently.
