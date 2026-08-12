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

Read the PR description (`body`) and any issue it links (`gh issue view <M> --repo "$REPO"`), then
write down, in one sentence, **what this PR claims to do**. Keep that sentence; it is the yardstick
for Angle I and it is what tells you whether a given change belongs here at all.

Two failure modes to avoid. Do not let the description talk you out of a defect — "known
limitation, follow-up PR" in the body does not make a dropped guard correct, though it does change
how you phrase the finding. And do not treat the description as a description of the diff: where
the two disagree, the diff is what merges. A body that promises a behaviour the diff does not
implement, or omits a behaviour the diff does, is itself a finding.

### Phase 3 — Find candidates (ten angles)

Work through all ten angles below yourself, in sequence, in this context. Do not skip an angle
because an earlier one found nothing there, and do not let one angle's conclusion suppress
another's — if two angles flag the same line for different reasons, record both.

Each angle surfaces up to six candidates, each with a `file`, a `line`, a one-line `summary`, and a
concrete `failure_scenario`. Pass every candidate with a nameable failure scenario through to
Phase 4 — finders that silently drop half-believed candidates are the dominant cause of misses.
A candidate you cannot express as a failure scenario is not yet a candidate.

**Angle A — line-by-line diff scan.** Read every hunk, line by line. Then read the enclosing
function for each hunk — bugs in unchanged lines of a touched function are in scope, since the PR
re-exposes or fails to fix them. For every line ask: what input, state, timing, or platform makes
this line wrong? Inverted or wrong conditions, off-by-one, nil/undefined deref, missing `await`,
unchecked `err`, falsy-zero checks, wrong-variable copy-paste, an error swallowed in a catch,
unescaped regex metacharacters.

**Angle B — removed-behavior auditor.** For every line the diff deletes or replaces, name the
invariant it enforced, then find where the new code re-establishes it. If you cannot find it,
that's a candidate: a removed guard, a dropped error path, a narrowed validation, a deleted test
that was covering a real case, a loosened RBAC or NetworkPolicy rule.

**Angle C — cross-file tracer.** For each function, template, chart value, or CRD field the diff
changes, grep for its consumers and check whether the change breaks any of them: a new
precondition, a changed return shape, a renamed key a manifest still reads, a timing dependency.
Trace runtime wiring through to the source — which container an env var lands in, which process
reads a port, which service account a binding actually grants — rather than inferring it from names.

**Angle D — operations and security.** This repo provisions clusters and holds credentials, so
weigh blast radius: IAM and RBAC scope, credential handling and redaction, NetworkPolicy reach,
what an agent is newly permitted to do, and whether a failure mode degrades or destroys. Check that
third-party GitHub Actions are pinned to a full commit SHA with the version in a trailing comment.

**Angle E — reuse.** The angles above hunt for bugs; this one and the next two hunt for cleanup in
the changed code. Flag new code that re-implements something the codebase already has — grep shared
and adjacent modules, and name the existing helper to call instead.

**Angle F — simplification and efficiency.** Flag unnecessary complexity the diff adds: redundant
or derivable state, copy-paste with slight variation, deep nesting, dead code left behind. And
wasted work: repeated I/O, independent operations run sequentially, blocking work added to startup
or a hot path. Name the simpler or cheaper form that does the same job.

**Angle G — altitude.** Check that each change sits at the right depth rather than being a fragile
bandaid. Special cases layered onto shared infrastructure are a sign the fix isn't deep enough —
prefer generalizing the underlying mechanism.

**Angle H — conventions and docs.** Read the `AGENTS.md` / `CLAUDE.md` files that govern the changed
code: the repo root, plus any in a directory that is an ancestor of a changed file (a directory's
file only applies at or below it). Flag a violation only when you can quote the exact rule and the
exact line that breaks it — no style preferences, no "spirit of the doc" inferences. Name the file
and quote the rule so the report can cite it. This is also where docs drift belongs: one canonical
home per fact, generated `<!-- BEGIN GENERATED -->` regions regenerated rather than hand-edited,
identifiers verified against source rather than against other docs.

**Angle I — scope and test coverage.** Hold the diff against the intent sentence from Phase 2b.
Flag changes that do not serve it: an unrelated refactor riding along, a dependency bump nobody
asked for, a behaviour change buried in a PR described as a rename, reformatting that inflates the
diff and hides the real hunks. Repo convention is scoped changes and no unrelated formatting, so
cite the rule when it applies. Judge by whether a change serves the stated intent, not by how large
it is — a big diff that does one thing is in scope, and a three-line change that does a second
thing is not.

Then check that the intent is actually tested: for each behaviour the PR claims, name the test that
would fail if that behaviour regressed. Where there is none, the candidate is the untested
behaviour, not the absent test — say which regression would ship silently. Bug fixes without a
regression test, and new error paths nothing exercises, are the usual cases.

**Angle J — sibling pull requests.** Every angle so far has looked only at this PR. Widen once:

```bash
gh pr list --repo "$REPO" --author <login> --state open --json number,title,files
```

Read the review comments on any sibling that touches adjacent paths. Three things come out of this
that nothing else in the review can see. A finding already accepted on a sibling usually applies
here unchanged — apply it rather than rediscovering it. A near-identical PR that has diverged is
itself a finding: name which copy carries the fix and which does not, because merge order then
decides whether the fix survives. And where one PR is a superset of others, say so — reviewing the
subset in isolation spends effort on a diff that may never merge.

For cleanup, altitude, conventions, scope, and sibling candidates the `failure_scenario` states the concrete
cost — what is duplicated, wasted, harder to maintain, out of scope, or which rule or untested
behaviour is at risk — instead of a crash. Correctness bugs always outrank them when the output cap
forces a cut.

Prefer running things over reasoning about them: execute the test suites the PR touches and
reproduce the failures you claim. Also check merge mechanics: `gh pr checks <N> --repo "$REPO"`. If
`mergeStateStatus` is `BLOCKED` or `DIRTY`, determine _why_ — failing required checks, merely
`REVIEW_REQUIRED`, missing labels, or the merge conflict you already found. The `tide` check usually
states its reason outright. Report which it is; they mean very different things.

### Phase 4 — Verify every claim (this phase is not optional)

Dedup first: candidates pointing at the same line and the same mechanism collapse into the one with
the most concrete failure scenario.

Then take each surviving candidate and re-derive it from the source as if you were a hostile second
reviewer trying to get it thrown out. Open the actual file and read the actual code path — do not
re-read your own notes, and do not accept a claim because it sounded right when you wrote it.
Confirm the mechanism, not just the conclusion: a real defect reached by an imaginary code path is
still a wrong finding. Assign each one a verdict:

- **CONFIRMED** — you can name the inputs or state that trigger it and the resulting wrong output,
  crash, or misconfiguration. Quote the line.
- **PLAUSIBLE** — the mechanism is real but the trigger is uncertain (timing, environment, cluster
  state). State what would confirm it. Realistic-but-unproven is PLAUSIBLE, not REFUTED:
  concurrency races, nil on a rare-but-reachable path (error handler, cold cache, absent optional
  field), falsy-zero treated as missing, off-by-one on a boundary the code does not exclude, a
  regex or allowlist that lost an anchor.
- **REFUTED** — factually wrong (the code doesn't say that), provably impossible (show the type,
  constant, or invariant), already handled in this diff (cite the guard), or pure style with no
  observable effect. Quote the line that proves it.

Keep CONFIRMED and PLAUSIBLE. Then rewrite the finding list so it reflects only what survived:

- **Claim holds** → keep it as written.
- **Claim holds but the mechanism, severity, line number, or blast radius is wrong** → rewrite the
  finding to the corrected version. The finding now reads as though the corrected version is what
  you found in the first place.
- **REFUTED, or you cannot verify it** → delete it entirely. Do not demote it to a footnote, a
  "worth checking" aside, or a parenthetical. If the underlying uncertainty is genuinely worth the
  author's time, restate it as an open question in its own right, with the uncertainty stated
  plainly in the finding body — never as a correction to something you previously asserted.

Then clean up after the edit, because these are what give away a second pass:

1. Re-sort findings by severity (`BLOCKER` / `HIGH` / `MEDIUM` / `LOW`) and **renumber from 1 with
   no gaps**.
2. Update every cross-reference between findings to the new numbers.
3. Update any count in the prose ("three blockers") to match the surviving set.
4. Re-read the whole document once for tense and voice consistency.

**The finished review must read as a single confident first pass.** It must contain no
"Correction", "Verification pass", "on second look", "an earlier draft said", "downgraded from",
"initially I thought", no diff-of-claims, and no changelog of your own reasoning. The reader should
have no way to tell that Phase 4 happened. This constraint applies to the saved file, the PR
comment, and your report back — everywhere.

### Phase 5 — Output

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
