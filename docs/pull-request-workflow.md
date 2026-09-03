# Pull-request workflow mechanics

**Scope:** The commands for getting a branch from "about to start" to "merged" in this repository —
finding work already in flight, measuring drift from `main`, validating locally, working the
automated review, the labels Tide merges on, and whose move it is at any point in between.

**Owns:** the mechanics. Every _requirement_ — that you scan for duplicate work, run the pre-PR
review passes, live-test the change, resolve every thread — is stated in
[`AGENTS.md`](../AGENTS.md) and stays there. This page is what you open at the moment you carry one
out. When the two disagree, `AGENTS.md` is right and this page needs fixing.

A rule about how to run a command correctly can sit here rather than in `AGENTS.md`, because it
means nothing until you have the command in front of you — that a contributor on a fork cannot
self-assign an issue is the example. The test is when a rule has to fire, though, not how
procedural it sounds: that a draft is not in the review queue reads like mechanics, but it has to
reach an agent before it decides to wait, so `AGENTS.md` states it and only the measurement behind
it is here. `AGENTS.md` is loaded into every session and this page is not, so anything that has to
fire before an agent thinks to open a link belongs on that side of the line, not this one.

The split is by form, not by topic: this page owns the mechanics that are commands, and
[`.agents/rules/`](../.agents/rules/) owns the ones that are prose. `AGENTS.md` had no budget left
to hold prose mechanics itself, so the pre-PR passes' went to
[`.agents/rules/pre_pr_review.md`](../.agents/rules/pre_pr_review.md) and the workflow-authoring
rules' to [`.agents/rules/github_actions.md`](../.agents/rules/github_actions.md). A mechanic with
no command in it goes there rather than here, whichever rule it serves.

Related: [`.agents/skills/review-preflight/SKILL.md`](../.agents/skills/review-preflight/SKILL.md)
owns the pre-PR review plumbing,
[`.agents/rules/`](../.agents/rules/) owns the rules an agent follows by family, and
[`.claude/commands/pr-review-batch.md`](../.claude/commands/pr-review-batch.md) owns the mechanics of
reviewing somebody else's pull request.

---

## Check whether someone is already doing it

`AGENTS.md`, "Before Starting a Task", requires this scan and says what to report. Branches live on
forks, so name the upstream repository on every call:

```bash
# Open PRs, with the files each one touches. File overlap is the strongest duplicate
# signal and one call gets it for every open PR.
gh pr list --repo gke-labs/kube-agents --state open --limit 100 \
  --json number,title,author,headRefName,isDraft,updatedAt,files

# Open issues, and who has already claimed them.
gh issue list --repo gke-labs/kube-agents --state open --limit 100 \
  --json number,title,assignees,labels

# Already tried? A closed pull request is a decision, not an absence.
gh search prs --repo gke-labs/kube-agents --state closed --limit 20 '<keywords>'
```

Claiming an unassigned issue, once the user has agreed:

```bash
gh issue edit <number> --repo gke-labs/kube-agents --add-assignee @me
```

`@me` is the account whose token you hold, and `AGENTS.md` is explicit that this makes it a person
you are volunteering. A contributor working from a fork without write access cannot self-assign at
all; offer a comment instead.

## Measure how far a branch has drifted from `main`

The files you are changing that `main` has also changed since you diverged — the list `AGENTS.md`
sends you here for:

```bash
# Fetch again before measuring anything. Every command below compares against the
# remote-tracking ref, and one you have not refreshed is stale in exactly the way
# this is about -- it answers "nothing has changed" for a main that has.
# The guard is for the offline case: `git diff` reports an unresolvable range on
# stderr while comm still exits 0, so a missing upstream/main prints an all-clear.
git fetch upstream main
git rev-parse --verify --quiet upstream/main >/dev/null || echo 'no upstream/main -- fetch first'

git rev-list --count HEAD..upstream/main   # how far this branch has drifted

# Files you are changing that main has also changed since you diverged. Three
# things the obvious version of this gets wrong:
#
#   - Your side has to count work that is not committed yet. Mid-branch, most of
#     what you are changing is still in the working tree, and a commit-only
#     comparison calls that case clean. `git diff HEAD` covers staged and
#     unstaged; `ls-files --others` adds files you have created but not added.
#   - --no-renames keeps both sides naming the same path. Rename detection is on
#     by default, so when main renames a file you are editing, its side reports
#     only the new path and yours only the old, and the intersection is empty.
#     Docs restructures move whole trees here, so this is not hypothetical.
#   - The two `...` ranges are in opposite orders -- your side of the fork point,
#     then main's. Do not pass the first as a pathspec to the second: a branch
#     with no commits of its own passes an empty pathspec, which git reads as no
#     filter and answers "every file main touched".
comm -12 <( { git diff --no-renames --name-only upstream/main...HEAD
              git diff --no-renames --name-only HEAD
              git ls-files --others --exclude-standard; } | sort -u ) \
         <(git diff --no-renames --name-only HEAD...upstream/main | sort)
```

`AGENTS.md` says what to do with the result. The one mechanical detail it leaves out: rebase refuses
on a dirty tree, so commit or stash before you start one.

## Local validation before committing

`AGENTS.md` names these checks; the detail is here.

**Formatting.** Run `prettier --write <files>` on changed Markdown or YAML files — `.md`, `.yaml`,
and `.yml` are the three extensions both CI and `make prettier-check` look at, so a reformatted
`.json` file is churn nothing asked for. `make prettier-check` checks all files — note that this
covers files outside your PR scope, while CI only checks the ones your branch changed. Install the version CI pins (see the Install Prettier step
in `.github/workflows/prettier.yml`), e.g. `npm install -g prettier@<that version>`: the manifests
gate in `k8s-operator-test.yml` asserts byte-equality against that version's output, so a skew fails
CI on files you did not touch. Prefer the installed binary over `npx prettier`, which re-resolves the
package against the npm registry on every run and fails outright behind an authenticated mirror —
that failure is why this step has previously been skipped rather than run.

**Docker build.** Validate the agent runner Dockerfile by building it locally:

```bash
docker build --platform linux/amd64 -f deploy/docker/Dockerfile --target platform .
```

Keep `--platform linux/amd64`: the base images are multi-arch and deployment targets are amd64 GKE
nodes, so a bare build on an arm64 machine produces an image that cannot run on the cluster (#560).

**Image layer budget.** If you add a `RUN` or `COPY` to `deploy/docker/Dockerfile`, build the
`platform` target with `-t platform-agent:latest` and run `python3 scripts/check_image_layers.py`.
Docker's overlay2 driver stops mounting at 128 layers and `agent-base` → `platform` is the deepest
chain the file ships; the gate fails at 120, leaving a fix somewhere to go. Because buildx has no
such limit, an over-budget image passes every PR build and fails only in Cloud Build, on main,
after merge (#658). CI runs the same check in
`docker-build.yml`. The docstring in `scripts/check_image_layers.py` owns which image the gate points
at and why — read it before changing the target.

**Operator code.** If you modify `k8s-operator/`, run `make` or `go build` inside that directory to
ensure compilation succeeds.

**A2A module code.** If you modify `a2a/`, run `go vet ./...` and `go test -race ./...` inside that
directory — what the `A2A Module Tests` CI job runs. The conformance suite starts an embedded
JetStream server, so no cluster or credentials are needed.

## The automated review

`AGENTS.md`, "Automated Review After Opening a Pull Request", says what `kube-agents-bot` is, when
it runs, and what you owe its findings. This section is how you watch for one and answer it.

### How to read it

A 👀 reaction means the review started; a posted review means it finished. Across #630–#699 the 👀
landed within seconds of the trigger, and the review a median of **9 minutes** after that — 15
minutes at the 90th percentile, 45 in the slowest of the 54 reviews in that range (#634, an XXL
diff). A `/review` re-read is no quicker: median 11 minutes, and none of the 42 measured took longer
than 22. A review that runs always reports back, so a one-line "no findings" is a result, not
silence. Findings arrive as inline comments badged 🔴 High, 🟠 Medium, or 🟡 Low; findings the bot
could not anchor to a changed line appear in the summary body under **Findings outside this diff**. A
👀 with nothing following it is a bug in the bot, not a verdict — it happened to 3 of the 57 pull
requests picked up in that range (#647, #649, #679), which is rare enough to be worth waiting through
and common enough that you must not wait forever.

### Waiting for it

Poll on a schedule rather than continuously — nothing is worth checking in the first 5 minutes, then
once a minute. Expect the review by 15 minutes; at 30 with nothing posted, stop waiting and tell the
user the bot dropped this one. Nothing retries on its own, so ask whether to spend a trigger — and
say which: a review that goes missing was a first-review-width one, so `/review all` is what replaces
it, and `/review` narrows the retry to what the bot is certain of.

Two things make a wait read wrong:

- **The 👀 does not come back.** A reaction is one per user, so the eyes from the first review are
  still sitting there when you comment `/review` or mark a draft ready. Only the review list moves:
  note how many bot reviews exist _before_ you re-trigger, and wait for that count to change rather
  than for a reaction that already fired.
- **A draft is not waiting on anything.** The trigger is ready-for-review, not opened. Every
  multi-hour gap in the range above was a draft sitting unreviewed by design — #652 for 12 hours,
  #659 for 18 — and measured from the ready event each was picked up in seconds. Do not start the
  clock, or report the bot broken, while the pull request is still a draft.

Poll with:

```bash
# Both commands name gke-labs/kube-agents explicitly: PR branches live on forks,
# but the review lives on the upstream pull request.

# Has the bot reviewed yet? Takes the LAST bot review and prints its timestamp
# first: after a /review the earlier review is still there, and reading it back
# looks exactly like the new one having landed. No output = no review yet.
# (gh reports the login without the [bot] suffix; the REST API below adds it.)
gh pr view <number> --repo gke-labs/kube-agents --json reviews \
  --jq '[.reviews[] | select(.author.login == "kube-agents-bot")] | last | select(.)
        | "\(.submittedAt)\n\(.body)"'

# The inline findings, with the comment ids needed to reply. --paginate matters:
# the default page holds 30 comments and a truncated list still looks complete.
# .line is null once a finding's line falls out of the diff, hence the fallback.
gh api repos/gke-labs/kube-agents/pulls/<number>/comments --paginate \
  --jq '.[] | select(.user.login == "kube-agents-bot[bot]")
        | "\(.path):\(.line // .original_line) [id \(.id)]\n\(.body)\n"'
```

### Replying to a finding

A finding you disagree with gets answered in its thread, not silently dropped:

```bash
gh api repos/gke-labs/kube-agents/pulls/<number>/comments/<comment-id>/replies \
  -f body='<the reasoning>'
```

## Resolving conversations

Reply first — `AGENTS.md` says why — naming what changed and the commit that changed it. Then
resolve:

```bash
# Every unresolved thread, with both ids you need: resolveReviewThread takes the
# thread's node id, while the reply endpoint above takes the first comment's
# databaseId. REST returns only the latter, which is why this one is GraphQL.
gh api graphql -f query='
query($pr: Int!) {
  repository(owner: "gke-labs", name: "kube-agents") {
    pullRequest(number: $pr) {
      reviewThreads(first: 100) {
        nodes {
          id isResolved isOutdated viewerCanResolve path line
          comments(first: 20) { nodes { databaseId author { login } body } }
        }
      }
    }
  }
}' -F pr=<number> --jq '.data.repository.pullRequest.reviewThreads.nodes[]
  | select(.isResolved | not)
  | "\(.path):\(.line // "outdated") thread \(.id) canResolve=\(.viewerCanResolve)
  reply to \(.comments.nodes[0].databaseId) — \(.comments.nodes[0].author.login): \(.comments.nodes[0].body | split("\n")[0])
  replies so far: \(.comments.nodes | length - 1)"'

# Per thread, once the reply naming the fix is posted:
gh api graphql -f query='
mutation($thread: ID!) {
  resolveReviewThread(input: {threadId: $thread}) { thread { isResolved } }
}' -f thread='<PRRT_...>'
```

Four ways that goes wrong quietly:

- `first: 100` is a cap, not a promise. A long-lived pull request can carry more threads than that;
  page for the rest, or say you only looked at the first hundred rather than reporting the branch
  clear.
- `line` is `null` once a thread's line falls out of the diff. Outdated is not addressed — the code
  moved, which says nothing about whether the finding still holds. Read the thread.
- `viewerCanResolve` is the authoritative answer to whether your token can resolve at all; it
  differs between a maintainer and a contributor pushing from a fork. Check it before you reply,
  because a mutation that fails after the reply is posted leaves a half-answered thread that looks
  handled.
- `unresolveReviewThread`, same `threadId`, is the undo. Use it the moment the user disagrees with
  something you resolved.

## How a change merges

Nobody presses Merge. This is a Prow repository: `google-oss-prow` squash-merges a pull request on
its own once Tide's query matches it, and that query wants the labels `lgtm` and `approved` both
present, with `do-not-merge`, `do-not-merge/hold`, `do-not-merge/invalid-owners-file`, and
`do-not-merge/work-in-progress` all absent. The configuration is
[`prow/oss/config.yaml`](https://github.com/GoogleCloudPlatform/oss-test-infra/blob/master/prow/oss/config.yaml)
and
[`prow/oss/plugins.yaml`](https://github.com/GoogleCloudPlatform/oss-test-infra/blob/master/prow/oss/plugins.yaml)
in `GoogleCloudPlatform/oss-test-infra` — another repository, which is why nothing here can be
authoritative about it. Read it there when the answer matters, and
[oss.gprow.dev/command-help](https://oss.gprow.dev/command-help) for the full command set.

The two labels are the two people:

- **`lgtm` is the reviewer's.** A GitHub "Approve" review sets it, and so does `/lgtm` in a comment.
  This is what the auto-requested human reviewer is being asked for. Prow does not take an `/lgtm`
  from the pull request's own author, so every change needs one other person however it is approved.
  `trusted_team_for_sticky_lgtm: Googlers` is configured, which means a push after the label lands
  strips it again unless the author is in that team, and the reviewer has to give it a second time.
- **`approved` is an `OWNERS` approver's.** `/approve`, from someone in the `OWNERS` file governing
  the changed paths — [`OWNERS`](../OWNERS) at the root, [`k8s-operator/OWNERS`](../k8s-operator/OWNERS)
  for the operator, with [`OWNERS_ALIASES`](../OWNERS_ALIASES) expanding `waw-leads`. An approver's
  "Approve" review sets both labels at once, which is why most pull requests here need exactly one
  review from one person (#1070). An approver's own pull request counts as self-approved, so a
  change from someone in `OWNERS` starts with the `approved` half already satisfied and waits only
  on the `lgtm` (#1075).

Everyone `.github/auto_request_review.yml` can assign is also an `OWNERS` approver, so the reviewer
the bot's green check summons is always someone who can clear both labels in one action. That is a
property of two lists agreeing today, not a guarantee either file makes.

Before any of that, a pull request from an author Prow does not already trust is labelled
`needs-ok-to-test`, and its Prow presubmits hold until a member comments `/ok-to-test`. It gates
Prow's jobs, not the GitHub Actions checks, so a pull request can look fully green and still be
waiting on it.

`/hold` parks an otherwise-mergeable pull request without withdrawing anything else, and
`/hold cancel` releases it — #1045 held that way for a smoke test. `/override <context>`, which only
a repository admin can use, forces a required check that cannot pass on its own — and expires: the
forced status embeds the base SHA at override time, so the next merge to `main` invalidates it and
Tide re-runs the job. `/override-sticky` writes the `[prow:skip-retest]` sentinel instead, which
Tide accepts regardless of base, so it survives `main` moving (#1202).

**Branch protection is not the gate and reads as though there is none.** `main` requires ten
contexts — `cla/google`, `actionlint`, `build`, `prettier`, `validate`, `Run Controller Tests`,
`Run Python Unit Tests`, `Documentation Checks`, `Validate Conventional Commit PR Title`, and
`Agent instructions cite assets that exist` — and conversation resolution, but **zero** approving
reviews, because approval is Tide's business rather than GitHub's. A reader who checks the
repository settings for the review requirement therefore finds nothing and concludes wrongly.

The last four joined the set on 2026-09-02; before that they reported on every pull request without
gating one.

Those ten are not the whole required set either. Tide also requires every Prow presubmit not marked
`optional`, and those are configured in `oss-test-infra` rather than in branch protection —
`pull-kube-agents-smoke-test` dropped its `optional: true` on 2026-09-02
(GoogleCloudPlatform/oss-test-infra#2677), so the behavioural presubmit gates every merge from that
date. The command below therefore answers half the question, and a red check in neither list blocks
no merge:

```bash
gh api repos/gke-labs/kube-agents/branches/main/protection \
  --jq '.required_status_checks.contexts'
```

**`mergeStateStatus` cannot answer "is this ready to merge" here, and it is the natural thing to
reach for.** Every open pull request reads `BLOCKED` or `DIRTY` and none ever reads `CLEAN`, because
`main` restricts pushes to the `google-oss-prow` app and GitHub scores that restriction as a block
on the querying user. #1065 read `BLOCKED` while carrying both labels and while `tide` reported
`In merge pool.` Ask Tide instead — its status on the head commit states its own reason — and
[oss.gprow.dev/tide](https://oss.gprow.dev/tide) shows the queue. One reason the status omits: a
merge attempt that failed — an unresolved review thread, most often — is retried every ~85
seconds and recorded only in the `err` field of
[tide-history](https://oss.gprow.dev/tide-history); #1122 sat approved for 5h46m and 231
attempts that way.

```bash
# Why Tide has not merged it: its own reason first, then the labels it wants.
gh api repos/gke-labs/kube-agents/commits/<head-sha>/status \
  --jq '.statuses[] | select(.context == "tide") | "\(.state): \(.description)"'
gh pr view <number> --repo gke-labs/kube-agents --json labels --jq '[.labels[].name]'
```

## Who owns an open pull request

Every open pull request has exactly one party whose move it is, and the commonest way one sits for
a fortnight is that both sides believe it is the other's. The rule:

**The author owns it while it is blocked on them** — a draft, failing _required_ checks, merge
conflicts, unresolved review threads, changes requested, or no human reviewer requested yet.
**Otherwise the requested reviewers own it.** A past reviewer does not: an approval already given
is not an outstanding obligation. `kube-agents-bot` and other bot reviewers never count either way.

A draft is owned by its author and waited on by nobody, so it is not a backlog item — it is work in
progress, and chasing it is noise.

Four states that look like somebody else's problem and are not:

- **`CHANGES_REQUESTED` does not clear itself.** It stays set until a reviewer submits a _new_
  review. Pushing the fix does not clear it, resolving every thread does not clear it, and GitHub
  will report `CHANGES_REQUESTED` and "review requested from X" in the same breath. Re-requesting
  review is the explicit hand-back — do that rather than assuming the push spoke for itself.
- **Nobody is requested at all.** Because a human is only assigned once the `AI Review` check goes
  green, an author with outstanding bot findings has no reviewer and no notification saying so.
  Clearing the findings and commenting `/review` for a clean pass is what summons one;
  `/request-review` is the override. Answering every bot thread does not summon one by itself, so
  an author who has done everything asked of them can still be sitting with nobody assigned.
- **A red check that is not required.** It blocks no merge and is not the author's problem — but
  "required" means both lists above, not branch protection's ten alone, and `tide` is what actually
  knows. Ask it before treating a failing job as work owed, and before concluding one is not.
- **`mergeable: UNKNOWN`.** GitHub computes mergeability lazily and the first query only triggers
  the job, so a conflict reads as conflict-free until you ask twice.

`skip_reason()` in [`scripts/request_reviewers.py`](../scripts/request_reviewers.py) is the same
rule in code for the one decision this repository automates — it declines to request a reviewer for
a draft, for a title carrying an ignored keyword, when someone is already requested, and when a
human other than the author has already submitted `APPROVED` or `CHANGES_REQUESTED`. A periodic
triage sweep applies the wider rule and messages whoever owns each pull request; it runs outside
this repository, so the rule above is all this page can state about it.

```bash
# Whose move is it: draft, conflicts, verdict, and who is on the hook now.
gh pr view <number> --repo gke-labs/kube-agents \
  --json isDraft,mergeable,reviewDecision,reviewRequests \
  --jq '{draft: .isDraft, mergeable, decision: .reviewDecision,
         requested: [.reviewRequests[].login]}'
```
