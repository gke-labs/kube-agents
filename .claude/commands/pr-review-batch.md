---
description: Review one or more PRs in parallel isolated worktrees, verify every claim, save first-pass-clean reviews
argument-hint: <pr-number> [pr-number ...]
---

Review these pull requests in `gke-labs/kube-agents`: **$ARGUMENTS**

PRs always live in that repo. Everything else — remote names, the base branch, the checkout
location — is discovered at runtime, so this command works for any teammate in any clone regardless
of what they called their remotes or where they cloned to.

Spawn **one subagent per PR number**, all in a single message so they run concurrently. Each
subagent owns exactly one PR end to end and reports back a short structured result. Do not review
any PR yourself in the main loop — your job is to fan out, then relay.

Give each subagent the instructions below verbatim, with `<N>` replaced by its PR number.

---

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
  nothing has changed since;
- a saved review exists and the author has landed no work of their own since — merging the base
  branch in is not new work to review:

  ```bash
  git log <recorded-sha>..HEAD --no-merges --not "$BASE_REF"     # empty → skip
  ```

  `--not "$BASE_REF"` is what makes this condition reachable. Without it the range still contains
  every base-branch commit the merge pulled in — `--no-merges` drops the merge commit itself, not
  the commits underneath it — so the check would report new work in exactly the case it is meant to
  skip. Phase 1b has already merged the base locally by this point, which makes the unfiltered range
  wrong even when the author pushed nothing at all.

A merge conflict is **not** a skip reason. Neither is an unmergeable `mergeStateStatus`.

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

- **The diff range is the one Phase 1b settled on** — `$BASE_REF...HEAD` after a clean merge, or
  `$BASE_REF...pr<N>` when the merge conflicted and was aborted. Never `main` on its own.
- **Angle J already has an author to filter by**, which the skill cannot assume:
  `gh pr list --repo "$REPO" --author <login> --state open --json number,title,files`. Read the
  review comments on any sibling touching adjacent paths.

One thing the skill has no way to know about:

- **Merge mechanics are part of this review.** Run `gh pr checks <N> --repo "$REPO"`. If
  `mergeStateStatus` is `BLOCKED` or `DIRTY`, determine _why_ — failing required checks, merely
  `REVIEW_REQUIRED`, missing labels, or the merge conflict you already found. The `tide` check
  usually states its reason outright. Report which it is; they mean very different things.

The skill's step 6 does not apply: dispositions belong to the author, and you fix nothing here. Its
"single confident first pass" constraint does, and it covers the saved file, the PR comment, and
your report back — everywhere.

### Phase 4 — Output

Save the review to `$MAIN_ROOT/.claude/pr-reviews/pr-<N>-<short-slug>.md`, matching the structure of
the files already in that directory:

- header block: title, author, review date, **head SHA reviewed** (needed for the skip check on the
  next run), base branch and base SHA, diff stat, worktree path;
- **Intent** — the one-sentence claim from Phase 2b, so the next reader knows what the findings were
  measured against;
- **Verdict** — can this merge as is, yes or no, with the blocking items named, and what the
  `mergeStateStatus` actually reflects. When the PR conflicts with its base, say so here and state
  that the review covers the PR as authored, not as merged;
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
skipped and why, and any that was reviewed against a conflicting base (those reviews cover the PR
as authored, not as merged).

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
