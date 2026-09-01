---
description: Review one or more PRs in parallel isolated worktrees, verify every claim, save first-pass-clean reviews
argument-hint: <pr-number> [pr-number ...]
---

Review these pull requests in `gke-labs/kube-agents`: **$ARGUMENTS**

PRs always live in that repo. Everything else — remote names, the base branch, the checkout
location — is discovered at runtime, so this command works for any teammate in any clone regardless
of what they called their remotes or where they cloned to.

Run **Phase −1 in the main loop first**, for every PR number, and wait for my answer. Then spawn
**one subagent per PR number that survives it**, all in a single message so they run concurrently.
Each subagent owns exactly one PR end to end and reports back a short structured result. Do not
review any PR yourself in the main loop — your job is to pre-flight, fan out, then relay.

Give each subagent everything under **[Subagent instructions (per PR)](#subagent-instructions-per-pr)**
verbatim, with `<N>` replaced by its PR number, plus the PR's pre-flight verdict, the review mode I
chose for it, and the SHA that mode starts from when it is a narrowed one. Phase −1 is yours and
stops at that heading: it asks me a question, which a subagent cannot do, so handing it on would
either hang the subagent or have it answer on my behalf.

---

## Phase −1 — Pre-flight: is this review already covered? (main loop)

Every PR here is read by `kube-agents-bot` on the way in, and the repo requires the author to have
run `review-adversarial` over their own diff and written the disposition into the body before
opening. When both of those happened and neither has gone stale, a third hostile read usually buys
nothing — so find that out before spending it.

This phase runs **in the main loop, before any subagent exists**, for two reasons. Its output is a
question for me, and a subagent has no way to ask one. And it is pure GitHub API — no worktree, no
fetch, no diff read — so it costs two calls per PR, plus one per merge sitting after the bot's
review, which is nearly always none or one.

### The two queries

The repo is named literally in both: Phase −1 runs before Phase 0 defines `$REPO`.

```bash
# Signal 1, in one call: the head SHA, the bot's reviews with the commit each one
# actually read, the commit graph, and the open threads. The filter reports the
# commits *after* the review's commit, which is what the currency test needs.
gh api graphql -f query='
query($pr:Int!){repository(owner:"gke-labs",name:"kube-agents"){pullRequest(number:$pr){
  headRefOid isDraft state author{login}
  reviews(last:20){nodes{author{login} state submittedAt body commit{oid}}}
  commits(last:100){nodes{commit{oid messageHeadline parents(first:2){totalCount nodes{oid}}}}}
  reviewThreads(first:100){nodes{isResolved path}}
}}}' -F pr=<N> --jq '.data.repository.pullRequest as $p
| ([$p.reviews.nodes[] | select(.author.login == "kube-agents-bot")] | last) as $r
| ($p.commits.nodes | map(.commit)) as $cs
| ($cs | map(.oid) | index($r.commit.oid // "")) as $i
| "head=\($p.headRefOid) state=\($p.state) draft=\($p.isDraft) commits=\($cs|length) unresolved=\([$p.reviewThreads.nodes[]|select(.isResolved|not)]|length)",
  (if $r == null then "lastbot: NONE"
   else "lastbot at=\($r.submittedAt) commit=\($r.commit.oid)",
        ($r.body | split("\n") | [.[0], (.[] | select(startswith("### Findings outside") or startswith("#### ") or startswith("_This was a")))] | join("\n")),
        (if $i == null then "since: review commit is not among those \($cs|length) commits"
         elif $i == ($cs|length) - 1 then "since: nothing, the review is at the tip"
         else "since:\n  " + ($cs[$i+1:] | map("\(.oid[0:7]) parents=\(.parents.totalCount)\(if .parents.totalCount > 1 then " p2=" + .parents.nodes[1].oid[0:7] else "" end) \(.messageHeadline)") | join("\n  "))
         end)
   end)'

# Signal 2: the PR description. Read it yourself — see below.
gh pr view <N> --repo gke-labs/kube-agents --json body -q .body
```

Use GraphQL for the reviews rather than `gh api repos/$REPO/pulls/<N>/reviews`: the REST endpoint
has been observed returning an empty body against this repo while `/pulls/<N>/comments` worked.
(REST does carry the review's `commit_id`, so that is not the reason — availability is.)

Keep the filter's `.[0]` / `startswith` shape if you edit it. `gh --jq` is gojq, but the same
program gets piped through real `jq` often enough that it has to run in both, and jq rejects a field
access applied straight to a function call — `capture("…").s` compiles under gojq and is a syntax
error under jq 1.6.

All three page sizes are caps rather than promises:

- `reviews(last: 20)` and `reviewThreads(first: 100)` — on a long-lived PR, say you looked at the
  last twenty reviews rather than reporting it clear off a truncated list.
- `commits(last: 100)` — a branch can outrun that, and a review older than the window is then
  indistinguishable from one whose commit was force-pushed away. Both print the same
  `since: … not among those N commits`. That lands on stale either way, which is the safe direction,
  but when `commits=100` say "could not confirm currency" rather than "force-pushed".

### Signal 1 — a current, clean bot review

Three things must hold.

**Clean.** The **last** bot review's body opens with either of the two clean verdicts. GraphQL
reports the login as `kube-agents-bot`, without the `[bot]` suffix the REST API adds. Take the last
one: after a `/review` the earlier review is still sitting there, and reading it back looks exactly
like the new one.

- `**No findings.**` — nothing raised anywhere.
- `**No findings in the code.**` — the bot cleared the diff and raised something outside it, a note
  on the description usually. It counts, but quote the note in the evidence rather than letting the
  word "clean" swallow it.

Where that note lives decides whether you have it. Sometimes it is in the first line, as on #684.
Sometimes the body carries a whole `### Findings outside this diff` section — findings the bot could
not anchor to a changed line — and on #709 that section ran to a 🔴 High and fifteen hundred words.
The filter keeps the section heading and each finding's `####` title line, not the argument under
them, which is enough to see that one exists and to quote it in a line. When the heading does
appear, re-run the same query with `--jq '…| last | .body'` and read it before you put `covered` in
front of me — one more call, on the rare PR that needs it. An unanchored High is a live finding on
the pull request, and it must not vanish between the review and the evidence I am shown.

Note the width too. The footer reads `_This was a strict pass: only what I am certain of…_` or
`_This was a wider pass: as well as what I am certain of…_`. A strict-pass clean covers less ground
than a wide-pass clean, and I may want the difference.

**Current.** Either the review's commit is the head, or everything after it is a merge **from the
base branch**. Merging the base branch in is not new work to review — this is the API-only twin of
the `git log <sha>..HEAD --no-merges --not "$BASE_REF"` rule in Phase 2.

`parents.totalCount > 1` is necessary and nowhere near sufficient. A sibling feature branch, or a
colleague's fork branch, merges with two parents exactly like `main` does, and it brings an entire
branch of code no review has read; `messageHeadline` is no backstop, since `Merge branch 'main'` is
a string anyone can type. The git rule catches this and the parent count does not, which is why the
filter prints the second parent as `p2=`. Confirm it is on the base branch before calling the review
current:

```bash
# One call per merge in the tail. "identical" or "behind" means <p2> was already on
# the base branch, so the merge brought in nothing unreviewed. "ahead" or "diverged"
# means it brought in a branch of its own: the review is stale, not current.
gh api repos/gke-labs/kube-agents/compare/<base-branch>...<p2> --jq .status
```

On #675 that is `behind` for `p2=5bc8165`, which is what makes its 8-commit tail a base merge. An
octopus merge has parents past the second; check each of them the same way, or call the review stale
and say why. Two ways this is still softer than the git rule, both worth saying out loud rather than
papering over:

- A conflicted merge carries hand-written resolution that no review has seen, and over the API it
  looks exactly like a clean one — `compare` reports on the parent, not on what the author did with
  it. When the tail is merges, call them presumed base merges and offer the delta pass; only a
  worktree can tell the two apart, and Phase −1 has no worktree by design. Phase 3 does that check;
  it does not use `git show --cc`, for a reason worth reading before you assume it would have.
- A review commit missing from the list is stale, but see the `commits(last: 100)` caveat above for
  which kind of stale.

**Nothing left open.** No unresolved review thread. An open thread is outstanding work by the repo's
own merge rules, however clean the latest review reads.

### Signal 2 — the author reviewed and tested it themselves

Read the body. Two of its sections are what AGENTS.md's "Pull Request Hygiene" requires before a
pull request is opened at all:

- **`## Self-Review`** — the disposition list from the author's own pre-PR passes, merged:
  `review-adversarial` and `review-docs-drift`, both on every change. What they looked for, what
  kind of context each pass ran in, what it found, and for each finding whether
  they fixed it or decided not to and why. This is the signal that matters most here, because it is
  the only one that says somebody already read this diff hostilely.
- **`### Live validation`** (and the `## Testing` section around it) — that the change was actually
  exercised. `Not live-tested` with a stated reason is a filled section.

Judge them by reading, not by measuring. A section holding only the template's HTML comment,
whitespace, or a bare `-` is unfilled — but so is a paragraph that says "reviewed it, looks fine",
because the bar AGENTS.md sets is that "no findings" counts **only alongside what was looked for**.
Length settles neither question. Watch for the section heading appearing in prose elsewhere in the
body, which is why this is a read rather than a regex: a PR that discusses the `## Self-Review`
section is not a PR that filled one in.

When the section is missing or unanswered, Signal 2 fails — say so plainly, since that is the first
thing the review would report anyway.

Do not reach for the author's inline comments as a substitute. Authors here do not leave top-level
inline comments on their own diffs; what you see under an author's name are replies to bot threads,
which is engagement with a review rather than one.

### The verdicts

| Verdict   | When                                                                    | What you do                                                                  |
| --------- | ----------------------------------------------------------------------- | ---------------------------------------------------------------------------- |
| `skip`    | Draft, closed, or merged                                                | Report it and stop — no queries argued, no subagent                          |
| `covered` | Signal 1 in full, plus Signal 2                                         | Report the evidence and **ask me** before reviewing                          |
| `partial` | Clean bot review but stale, or Signal 2 missing, or a thread still open | Report it, name exactly what is missing, and offer a narrower or a full pass |
| `review`  | No clean bot review — the bot found issues, or never ran                | Proceed to fan-out with no prompt                                            |

`skip` is the same judgement Phase 2 makes and the same one it reports; making it here as well just
saves spawning a subagent to reach it. A draft is the author saying they are not asking yet.

### The ask

If nothing is `covered` or `partial`, say so in one line and fan out. Otherwise, per such PR, put
the evidence in front of me before you spend anything:

- the bot review's first line, its width, and its date;
- its commit versus the head, and what the commits in between are, if any;
- what the Self-Review section says it looked for and found, in a line or two;
- the Live-validation section in one line — what the author says they exercised;
- for `partial`, the single thing that is missing.

Then offer three choices: **skip it**, **review only what landed since `<sha>`** — the bot review's
commit, the one the query prints as `lastbot … commit=` — or **a full pass anyway**. I decide —
coverage is a suggestion, and "the bot found nothing" is not a review verdict of yours.

Two of the filter's three `since:` outcomes take the narrowed option off the table, and for opposite
reasons. Offer two choices, not three, when the line reads either of these:

- **`since: nothing, the review is at the tip`** — there is nothing after the review to narrow to.
- **`since: review commit is not among those N commits`** — there is no anchor at all. That commit
  was force-pushed away, or the branch outran the `commits(last: 100)` window, and either way
  "everything since `<sha>`" names a starting point that is not on the branch. Phase 1 fetches only
  `refs/pull/<N>/head`, so the SHA would not even resolve in the worktree, and `git log
"$SINCE_SHA"..pr<N>` fails with `fatal: bad revision` on empty stdout — indistinguishable, to a
  subagent reading stdout, from a delta that is genuinely empty. This is the case a stale verdict
  exists for: offer the full pass, and say the anchor is gone rather than that there is nothing to
  see.

Do **not** withhold it for the remaining case, a tail of presumed base merges. That is where the
option earns its keep — a conflicted merge's hand-written resolution is precisely the code no review
has read, and from up here it looks exactly like a clean one. It may still turn out to hold nothing,
but that is a result Phase 3 reports after replaying the merge in a worktree, not a promise you can
make before spending it.

Fan out only for what I keep, and tell each subagent its verdict, its mode, and — for a narrowed
pass — the SHA, which it has no way to recover from a decision I made in the main loop.

## Subagent instructions (per PR)

You are reviewing PR **#\<N\>**. Work through these phases in order.

### Phase 0 — Resolve the repository

The repo is fixed; the remote pointing at it is not. `origin` and `upstream` are one person's
convention, not a guarantee, and the base branch is whatever the PR targets. Derive both:

```bash
REPO=gke-labs/kube-agents
REPO_SSH=git@github.com:$REPO.git
MAIN_ROOT=$(dirname "$(git rev-parse --path-format=absolute --git-common-dir)")

# The local remote whose URL points at $REPO, whatever it happens to be called.
# Matches both SSH (git@github.com:owner/repo.git) and HTTPS clones.
BASE_REMOTE=$(git remote | while read -r r; do
  case "$(git remote get-url "$r")" in *"$REPO"*) echo "$r"; break;; esac
done)
# No remote points at it — fetch from the SSH URL directly rather than guessing a name.
: "${BASE_REMOTE:=$REPO_SSH}"

BASE_BRANCH=$(gh pr view <N> --repo "$REPO" --json baseRefName -q .baseRefName)
```

Carry `$REPO`, `$REPO_SSH`, `$BASE_REMOTE`, `$BASE_BRANCH`, and `$MAIN_ROOT` through every later
phase. Use `gh` for everything GitHub-side (always with `--repo "$REPO"`, since a worktree may not
resolve a default repo the way the main checkout does) and SSH for everything git-side — never
`https://` fetches and never `WebFetch` against github.com.

`$BASE_REMOTE` is a fetch source, so it works as either a remote name or the SSH URL. Only the
remote-tracking ref differs: with a named remote the base is `$BASE_REMOTE/$BASE_BRANCH`; with a
URL there is no tracking ref, so fetch into a local one and use that instead:

```bash
git fetch "$BASE_REMOTE" "+refs/heads/$BASE_BRANCH:refs/kube-agents-base/$BASE_BRANCH"
```

Set `BASE_REF` once to whichever applies and use `$BASE_REF` everywhere below.

`$MAIN_ROOT` is the top of the primary checkout even when you are standing inside a worktree; it is
where saved reviews live so that every run — and every teammate — sees the same history.

Two more variables come from the main loop rather than from here, and carry the same way:
`$REVIEW_MODE` is `full` or `since`, and in `since` mode `$SINCE_SHA` is the commit Phase −1 found
already reviewed. Phase 3 picks what it reads off them and Phase 4 records them. **Spawned without
a mode, you are `full`** — that is what the `review` verdict hands over, and it is the common case.
Never infer a narrower one from the PR's history yourself: narrowing is mine to authorise, and a
review that silently reads less than the whole diff still saves a file the next run trusts.

### Phase 1 — Worktree

Create your own worktree so you never contend with the other agents:

```bash
WT="$MAIN_ROOT/.claude/worktrees/pr-<N>"
git -C "$MAIN_ROOT" worktree prune                      # clear registrations whose directory is gone
[ -d "$WT" ] || git -C "$MAIN_ROOT" worktree add --force -B pr-<N>-review "$WT" || exit 1
cd "$WT" || exit 1
```

Every part of that earns its place, because nothing here ever deletes a worktree or its branch and
so the second run is the normal case: `prune` clears a registration left behind by a directory
someone deleted by hand, `-B` reuses a leftover `pr-<N>-review` branch instead of failing on it,
`--force` tolerates that branch being checked out elsewhere, the `[ -d ]` guard reuses an intact
worktree rather than failing on the occupied path, and the two `|| exit 1` are the point of the
whole line.

**If you cannot get into the worktree, stop and report `status: skipped` with the reason.** Never
continue into the later phases from the shared checkout. A swallowed failure here does not degrade
the review, it redirects it: `git checkout -B` and `git merge` in Phase 1b would then run against
the developer's primary checkout, several subagents at once, all in the same directory.

Then run **everything else from inside that worktree**. Never use `git -C` pointing at the shared
checkout after this point — a worktree-isolated session refuses it.

Fetch the base branch and the PR head over SSH:

```bash
git fetch "$BASE_REMOTE" "$BASE_BRANCH"
git fetch "$BASE_REMOTE" "refs/pull/<N>/head:pr<N>" --force
git checkout -B pr-<N>-review "pr<N>"
```

### Phase 1b — Sync, and tolerate conflicts

Record whether the branch already contains the tip of the base branch:
`git merge-base pr<N> "$BASE_REF"` vs `git rev-parse "$BASE_REF"`.

Then attempt the sync:

```bash
git merge "$BASE_REF" --no-edit
```

- **Merge succeeds (or was already up to date)** → set `CONFLICTS=none`. Diff base for the review is
  `git diff "$BASE_REF"...HEAD`.
- **Merge conflicts** → **review anyway.** Capture the conflicting paths, then back the merge out
  and review the PR's own diff, exactly as GitHub renders it:

  ```bash
  git diff --name-only --diff-filter=U        # record these paths
  git merge --abort
  git checkout -B pr-<N>-review "pr<N>"
  ```

  Diff base is now `git diff "$BASE_REF"...pr<N>` — the three-dot form, so the
  comparison is against the merge base and unrelated base-branch drift stays out of scope.

**Never resolve a conflict.** Do not edit conflicted files, do not pick a side, do not commit a
resolution. Conflicts are a fact you report, not a problem you fix: the author owns the rebase, and
a resolution you invent would make every finding downstream of it fiction.

When conflicts exist, note the limits of the review honestly in the output: you reviewed the PR as
authored, not as merged, so defects arising from the _interaction_ between the PR and newer base
commits are out of scope. Where a conflicting file is central to the change, say so — and look at
`git log "$BASE_REF" -- <conflicting-path>` to see what landed there, so you can
flag an interaction risk as an open question without pretending to have verified it.

### Phase 2 — Should this review happen at all?

Read the PR's history before spending tokens on a review:

```bash
gh pr view <N> --repo "$REPO" --json title,author,state,isDraft,mergeable,\
mergeStateStatus,reviewDecision,headRefOid,body,comments,reviews,changedFiles,additions,deletions
gh api "repos/$REPO/pulls/<N>/comments" --paginate \
  --jq '.[] | "\(.user.login) \(.created_at) \(.path):\(.line // .original_line)\n\(.body)\n"'
```

Also check for a prior review of ours: `ls "$MAIN_ROOT/.claude/pr-reviews/"` and read any file
matching this PR.

**Skip the review** (report `status: skipped` plus the reason) when any of these hold:

- the PR is closed or merged;
- the PR is a **draft** — full stop, whatever its commit history looks like. A draft is the author
  saying they are not asking for review yet, and review comments on unfinished work are noise at
  best. Wait for ready-for-review;
- a saved review already exists for this PR **and** `headRefOid` matches the head SHA it recorded —
  nothing has changed since (`full` mode only, see below);
- a saved review exists and the author has landed no work of their own since — merging the base
  branch in is not new work to review (`full` mode only, see below):

  ```bash
  git log <recorded-sha>..HEAD --no-merges --not "$BASE_REF"     # empty → skip
  ```

  `--not "$BASE_REF"` is what makes this condition reachable. Without it the range still contains
  every base-branch commit the merge pulled in — `--no-merges` drops the merge commit itself, not
  the commits underneath it — so the check would report new work in exactly the case it is meant to
  skip. Phase 1b has already merged the base locally by this point, which makes the unfiltered range
  wrong even when the author pushed nothing at all.

Both of those conditions read the saved review's **scope** as well as its SHA, and skip only on
`scope: full`. A file recording `scope: since <sha>` covers the commits after that SHA and nothing
before them, so a matching head SHA there says that nothing new has landed, not that the diff was
ever read whole — skipping on it would let one narrowed pass suppress the full one permanently.
Proceed instead, and say in your report that the prior review was a narrowed one and what it
covered. A saved review with no scope field predates the narrowed mode and was a full pass.

**Neither condition applies at all when `$REVIEW_MODE` is `since`.** They read a saved review of
ours; Phase −1 read the bot's and mine; and because the two never consult each other, the second
condition silently cancels precisely the pass Phase −1 got authorisation for. It is empty _by
construction_ on a merge tail — `--no-merges` drops the merge and `--not "$BASE_REF"` drops
everything it pulled in — which is the same fact that made Phase −1 call the bot review current and
offer the narrowed pass in the first place. Skipping there means the merge's hand-written
resolution is read by nobody, and that resolution is the whole reason I paid for the pass. So run
it. If the delta turns out empty, Phase 3 says so and names what it ran, which is a report; `status: skipped` off a
condition I was never asked about is not.

A merge conflict is **not** a skip reason. Neither is an unmergeable `mergeStateStatus`.

**Do not re-litigate coverage.** Phase −1 already weighed the bot's verdict and the author's
evidence — either it found no coverage worth raising, or it raised it and was told to go ahead. You
were spawned either way, so a clean bot review is not a skip reason at this point, and neither is a
thorough Self-Review section. Review at the mode you were given.

Both are still worth reading, for a different purpose. The Self-Review section is where AGENTS.md
tells every reviewer to start — it says where the author's own pass stopped, so yours can start
there — and the bot's review says what a second reader already cleared. Neither is a reason to drop
a finding of your own; both are a reason to be able to say why they missed it. A finding on a line
the bot passed, or one the author rejected with a reason, needs the skill's step 5 to answer them
rather than talk past them.

Otherwise proceed. If a prior review exists but new commits landed, review the current head in full
and note in your report which findings from the prior review the new commits resolved. Read the
existing PR conversation carefully either way — a finding the author has already answered in a
thread is either resolved (drop it) or contested (address their argument directly rather than
restating the claim).

### Phase 2b — Establish intent

Read the PR description (`body`) and any issue it links (`gh issue view <M> --repo "$REPO"`), and
carry both into step 3 of the skill below. On a pull request the description is also a thing that
can be wrong: a body promising a behaviour the diff does not implement, or silent about one it
does, is itself a finding.

### Phase 3 — Find the candidates and verify them

Run `$MAIN_ROOT/.agents/skills/review-adversarial/SKILL.md`. It is the repository's review method
and the canonical home for the ten angles and the verification discipline; read it now and work it
in order — intent, angles A–J, then the verification step, which is not optional.

Its step 1 is already satisfied: you are a subagent that did not write this change, which is the
separation it asks for. Do not spawn another one. Start at its step 2.

`$MAIN_ROOT` is not decoration. You are standing in the worktree, which holds the pull request's
own content: a bare path would load the review method **from the change under review**, so a fork
branch could edit the angles that judge it, and a branch cut before the skill existed would find no
file at all and silently review nothing. Read it from the primary checkout, the way Phase 2 and
Phase 4 already read the saved-review directory.

Two substitutions for this context:

- **The diff range follows `$REVIEW_MODE`.** In `full` mode it is the one Phase 1b settled on —
  `$BASE_REF...HEAD` after a clean merge, or `$BASE_REF...pr<N>` when the merge conflicted and was
  aborted. Never `main` on its own.

  `since` mode has **no such range, and you must not invent one.** The obvious answer,
  `git diff "$SINCE_SHA"..pr<N>`, is the wrong one: every base-branch commit an intervening merge
  pulled in lands inside it — on #675 that is 250 files and 25,323 insertions of already-reviewed
  work, more than the full pass I declined rather than less. The three-dot form is no escape, being
  the identical diff whenever `$SINCE_SHA` is an ancestor of `pr<N>` — which is a precondition to
  check, not a property to assume. Read the commits instead, and against `pr<N>` rather than `HEAD`,
  since `HEAD` after a clean merge carries a merge commit Phase 1b created seconds ago that no
  author wrote:

  ```bash
  # Precondition. Phase 1 fetched only refs/pull/<N>/head, so a rebased-away anchor is
  # not in this worktree at all: every command below would then exit 128 on empty stdout.
  git merge-base --is-ancestor "$SINCE_SHA" pr<N> || exit 1

  git log "$SINCE_SHA"..pr<N> --no-merges --not "$BASE_REF" --format=%H   # the author's own commits
  git show <sha>                                                          # one per commit above
  git log "$SINCE_SHA"..pr<N> --merges --format=%H                        # every merge in between

  # Per merge: replay it, and diff the machine's result against the author's. What
  # comes out is exactly what the human did that an automatic merge would not have.
  AUTO=$(git merge-tree --write-tree <merge-sha>^1 <merge-sha>^2 | head -1)
  git diff "$AUTO" <merge-sha>^{tree}
  ```

  **Stop if the precondition fails** and report `status: skipped`, reason `since-anchor <sha> is not
an ancestor of pr<N>`. Phase −1 is supposed to withhold the narrowed option in exactly that case,
  so reaching here means either it did not, or the branch was force-pushed between its query and
  your fetch. Do not fall back to a full pass I did not ask for, and above all do not read the empty
  stdout as an empty delta: `fatal: bad revision` and "nothing landed since" are the same two blank
  lines, and Phase 2's skip conditions — which would otherwise have caught a stale anchor — are
  disabled for this mode.

  `--not "$BASE_REF"` earns its place for the reason Phase 2 gives, and it is doing more work than
  it looks: a sibling branch merged into the PR contributes its own non-merge commits to that first
  list, because they are reachable from `pr<N>` and not from the base. That is the git-side check
  Phase −1 can only approximate with `compare`.

  **Do not reach for `git show --cc` here**, however natural it looks. `--cc` prunes every hunk whose
  merge result matches one of the parents, and taking one side wholesale — `git checkout --ours`,
  `--theirs`, or picking a variant per hunk — is how most conflicts are actually resolved. A merge
  that discarded the base branch's change to a file the PR also touched prints a bare 162-byte
  header, byte-identical to a merge that had no conflict at all. Empty `--cc` means "nothing was
  resolved into a form that differs from both parents", which is not the claim this mode needs.

  `git merge-tree --write-tree` (git ≥ 2.38) makes the claim it needs. It replays the merge with no
  worktree and prints the tree an automatic merge would have produced — conflict markers and all,
  exiting 1 when it hit one — so diffing that tree against the merge's own tree yields precisely the
  author's contribution: the resolution they wrote, or nothing at all when the merge really was
  clean. Empty there **is** the clean base merge Phase −1 could only presume it was, and non-empty is
  the unreviewed code that justified the pass. Two edges: on a merge with more than two parents
  `merge-tree` takes only two, and on git below 2.38 the flag does not exist. In either case fall
  back to `git show --cc`, and say in the review that the merge was checked with the weaker test.

  The author's commits and whatever the replay turns up are together what the skill's step 2 means
  by the range — its angles apply to them, read as always at their state in `pr<N>` rather than as
  isolated hunks.

  **Both lists coming back empty is a real outcome once the precondition has passed**, and #675 is
  one — no author commits, and a merge the replay reproduces exactly. Report the delta as empty and
  say what you ran; do not go looking for something to say, and do not quietly widen to the full
  diff I did not ask for. A defect you happen to notice outside the delta is still worth reporting,
  but you have not read the rest of the diff, so do not imply you have — Phase 4 records the scope.

- **Angle J already has an author to filter by**, which the skill cannot assume:
  `gh pr list --repo "$REPO" --author <login> --state open --json number,title,files`. Read the
  review comments on any sibling touching adjacent paths.

- **No green-suite bypass.** Do not skip hunting candidates because CI or unit tests are passing.
  Work every angle explicitly against the diff as defined in `review-adversarial`.

One thing the skill has no way to know about:

- **Merge mechanics are part of this review.** Run `gh pr checks <N> --repo "$REPO"` and read the
  `tide` status on the head commit, which states its reason outright. Do not read
  `mergeStateStatus`: every open pull request in this repository reads `BLOCKED` or `DIRTY` and
  none reads `CLEAN`, so it distinguishes nothing —
  `docs/pull-request-workflow.md`, "How a change merges", says why. `DIRTY` is the one signal it
  does carry, and it means the conflict you have probably already found.

The skill's step 6 does not apply: dispositions belong to the author, and you fix nothing here. Its
"single confident first pass" constraint does, and it covers the saved file, the PR comment, and
your report back — everywhere.

### Phase 4 — Output

Save the review to `$MAIN_ROOT/.claude/pr-reviews/pr-<N>-<short-slug>.md`, matching the structure of
the files already in that directory:

- header block: title, author, review date, **head SHA reviewed**, **scope** — `full`, or
  `since <sha>` — base branch and base SHA, diff stat, worktree path. The next run's skip check
  needs both of the bold ones: it trusts a matching SHA only where the scope says the whole diff was
  read, so a narrowed pass that records only the SHA reads exactly like a full one and cancels it;
- **Intent** — the one-sentence claim from Phase 2b, so the next reader knows what the findings were
  measured against;
- **Verdict** — can this merge as is, yes or no, with the blocking items named, and what `tide`
  says it is waiting for. When the PR conflicts with its base, say so here and state
  that the review covers the PR as authored, not as merged. In `since` mode say that too: the
  verdict speaks for the commits you read, not for the pull request;
- **Checks run** — commands executed and what they showed;
- **Findings** — severity-ordered, each with anchor, description, failure scenario, and verdict;
- **Not findings, for the record** — things that look wrong but are fine, so the next reader does
  not re-litigate them;
- **Suggested path to merge** — the ordered minimum set of fixes, with the conflict resolution
  listed as the author's step when there is one.

Do **not** post anything to GitHub. Posting is the main agent's call.

Report back to the main agent, and nothing more than this:

```
pr: <N>
status: reviewed | skipped
reason: <one line, only when skipped>
scope: full | since <sha>
mergeable: yes | no
block_reason: ci | review-required | labels | conflicts | none
conflicts: none | <comma-separated paths>
synced: <whether the base was already merged, that you merged it, or that the merge conflicted and was aborted>
base: <base branch>
review_file: <path>
findings: <count by severity, e.g. 2 BLOCKER / 1 HIGH / 3 MEDIUM / 4 LOW>
blockers: <one line each, file:line — claim>
```

---

## After the subagents return

Print one compact table across all PRs — number, title, verdict, block reason, blocker count,
review file path — then the blocker one-liners grouped by PR. Call out explicitly any PR that was
skipped and why, any that was reviewed against a conflicting base (those reviews cover the PR as
authored, not as merged), and any reviewed at a narrowed scope, naming the SHA it started from —
those cover the commits since that SHA and say nothing about the rest of the diff.

Do not post to GitHub unless I ask. When I do, post findings only — no verdict, no CI summary, no
closing section — and tell me whether you posted it as an issue comment or a formal review.

## Posting, when I ask for it

One review per PR with the findings anchored inline, not a summary comment that makes the author
hunt for the line:

````bash
cat > /tmp/review-<N>.json <<'JSON'
{"event":"COMMENT","body":"<summary>","comments":[
  {"path":"<path>","line":<n>,"side":"RIGHT","body":"<finding>\n\n```suggestion\n<replacement>\n```"}
]}
JSON
python3 -m json.tool /tmp/review-<N>.json      # a malformed payload 422s and posts nothing at all
gh api "repos/$REPO/pulls/<N>/reviews" --input /tmp/review-<N>.json
````

The rules that decide whether it lands:

- `event` is `COMMENT`. Never `APPROVE`, never `REQUEST_CHANGES` — that is the human's signature,
  not yours.
- **`/lgtm` is the same signature by another route, and here it is the merge itself.** Tide merges
  on labels, and an `APPROVE` review sets `lgtm` — so does the bare command, from any account Prow
  counts as a collaborator. `/approve` sets the other label. Neither goes out on my behalf unless I
  asked for it,
  and nor does `/hold`, which blocks somebody else's change in public. A review that concludes the
  change is good says so to me; a person signs it. `docs/pull-request-workflow.md`,
  "How a change merges", is what each label does.
- `line` must be a RIGHT-side line the diff actually shows; use `start_line` with `line` for a
  range. A finding that anchors to no changed line goes in the summary body under a **Findings
  outside this diff** heading — never forced onto a nearby unrelated line, which is how a reviewer
  ends up arguing about the wrong code.
- A `suggestion` block replaces exactly the commented range, so it must contain the complete new
  text for those lines, at the right indentation. Getting the range wrong silently deletes code
  when the author clicks Commit.
- A `suggestion` cannot contain a fenced code block — the inner fence closes the outer one. When
  the fix is itself a fenced block, describe it in prose instead.

Validate the JSON before sending. The API rejects the whole review on one bad anchor, so an
unvalidated payload usually means posting nothing and believing you posted everything.
