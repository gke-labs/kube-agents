# Contributor Agent - kube-agents

This is the contract for an AI agent that contributes to kube-agents as a
developer: claiming issues, investigating defects, testing, opening pull
requests, and responding to review.

It is written for the case where **more than one such agent** contributes at
once, the agents are **implemented and operated by different owners**, and they
**share no runtime bus** - the only coordination channel between them is GitHub
itself. Everything an agent needs to coordinate is expressed as GitHub state:
an issue's assignee, a pull request's review events and comments, a label.

This document is **implementation-agnostic**. It specifies _what_ state to read
and write, and _what rules_ to obey - not _how_ to poll. Whether you use the
`gh` CLI, the REST/GraphQL API, a GitHub Action, or a cron is your operator's
concern; the `gh` snippets are reference examples, not requirements.

Read this alongside the root [`AGENTS.md`](../../AGENTS.md) (PR hygiene, the
`kube-agents-bot` review contract, local validation) and
[`CONTRIBUTING.md`](../../CONTRIBUTING.md) (the CLA). This document
does not restate them - it only adds the agent-to-agent loop. Root `AGENTS.md`
is written for an agent working **with** a human user - its "work the findings
with the user" and "ask the user before acting" clauses assume one is in the
loop. This document is for an **unattended** agent with no human to ask, and
where the two conflict, this document governs.

## Scope

Agents **do not review one another's work unless asked to by a human.** Review
and approval are human responsibilities (assisted by `kube-agents-bot`); merge
is external automation applied once a human approves (see the root
`AGENTS.md`). A review a human asks you for is a comment, never a verdict: see
[Hard rules](#hard-rules). An agent's job ends at "answer every review comment,
resolve the ones a commit fixed, and get the human to approve." The only
coordination _between_ agents is the claim, below.

## The loop

Run on your own cadence. Each cycle, take the **first** step below that has
work, do that step fully, then stop. Do not start the next step in the same
cycle.

1. **Own open pull requests first.** For each of your open PRs
   (`gh pr list --author @me --state open`), check for new review events,
   inline comments, and check status. Answer every finding in its thread. On
   the first review, fix and push. On a later round, decline by default: fix
   a 🟠 Medium only when it is a `behaviour` finding on code your PR added and
   the fix is one mechanical edit per site that adds no file, helper, branch,
   flag, dependency or regex alternative; otherwise reply
   `declined: <reason about this change>` or `deferred to #N`, record it in
   **Self-Review**, and leave the thread open - a human's approval, or
   their reply in the thread that accepts the decline, is the ruling, and
   then you resolve it citing that; a reply that asks for the fix owes the
   fix, thread open. A correct 🔴 High is a fix on any round, unless its fix would add a
   mechanism - then it is a design question for the human reviewer, deferred
   to an issue, not built on a review round. Resolve only a thread a commit
   fixed, you showed factually wrong, or a human ruled on as above, per the
   bar in the root `AGENTS.md`.
   Bring the body current in the same push as the fix, before any re-read.
   You get at most two re-reads of your own before green: after a push that
   fixed something, comment `/review` for a narrow re-check of the diff, or
   `/review all` for a wider re-check when the changes are substantial; after
   an edit to the pull request body alone, comment `/review fresh` instead,
   since a plain `/review` on an unchanged commit re-cuts the earlier review
   without reading the body again. A merge of `main` or a rebase that changes
   nothing of yours earns none. At the third reviewed commit, or after a
   round that pushed nothing - every finding declined, deferred or refuted -
   comment `/request-review` if no human is on it yet (_who counts_, in
   [green is settled](../../docs/pull-request-workflow.md#green-is-settled)),
   and stop. A green
   `AI Review` check is settled: type no `/review` in any form after it, and
   do not mark a draft ready or close and reopen to buy one; answer each open
   🟠 Medium by reply, push a fix only for one you would have fixed unasked,
   and wait for the human the green summons. Their changes-requested review,
   a `/review` that is not yours, or a diff grown past twice the green head's
   size owes one more; nothing else does
   ([the stop rule](../../docs/pull-request-workflow.md#green-is-settled)). If
   `lgtm` is present, you are done only when the full merge gate holds -
   `lgtm` _and_ `approved` present and the required checks passing (and
   `ok-to-test` applied when Prow does not trust the author). The system then
   merges; you never do. `lgtm` alone is not the finish line: verify the rest
   before moving on. If `do-not-merge/hold` is present, read the comment
   explaining why and wait.
2. **Continue in-progress work.** If you have an assigned issue with a branch
   in progress, continue it. Skip any issue carrying `needs-human` - it is
   blocked on a human, not on you (see [Escalating to
   humans](#escalating-to-humans)).
3. **Claim one unassigned issue.** See [Claiming](#claiming). Fix it on a
   branch, push to your fork, and open a PR against `gke-labs/kube-agents`.
   Reviewer assignment and the automated review happen on their own (see the
   root `AGENTS.md`).
4. **Stop.**

Step 1 cannot permanently miss a review: review state is durable on the PR, so
a missed cycle simply catches it on the next one - nothing is lost the way a
one-shot notification would be.

## Claiming

Ownership of an issue is expressed by its **assignee** - there is no other
claim channel. To claim:

1. `gh issue edit <number> --add-assignee @me` (or the API equivalent).
2. **Re-read the issue** and confirm you are the **sole** assignee.

If another agent assigned itself in the same window, you collided. Tie-break
deterministically, without coordinating: sort the assignees by GitHub username;
if yours is first, keep the assignment, otherwise remove yourself and pick a
different issue. Never try to break the tie by force.

You work only issues assigned to you. Never reassign or close an issue you do
not own. Re-verify you are still the sole assignee before opening the PR - the
other agent's assignment may have landed after your re-read.

## Before you claim or file

- **Reproduce first.** File or claim an issue only for a defect you executed in
  the current session and whose actual output you can paste. A claim
  transcribed from a document is not evidence - the document may be wrong. A
  number you can reproduce is not a number you have understood: read the
  field's definition before building an argument on it.
- **Search first.** Check for an existing issue before filing, so two agents do
  not file the same defect twice.

## Escalating to humans

When you are blocked on a human decision - a design trade-off, a
permission/config change, anything only a maintainer can resolve - do **not**
silently stall:

1. Apply the `needs-human` label.
2. Comment, `@mention`ing the relevant maintainer: the primary for the
   area in `docs/ownership.md`, or any approver from `OWNERS` when the row
   names no login, and an approver as well when the decision is one only
   an approver can make; expand a group alias (e.g. `eval-crew`) through
   `OWNERS_ALIASES` to the accounts a mention actually reaches. State what
   blocks you, what would unblock you, and who can unblock it.

The label plus the mention **is** the escalation - there is no other channel.
The `@mention` is what draws a human to act on it. An issue carrying
`needs-human` is not claimable: your claim filter must skip it. After applying
`needs-human`, stop working the issue until a human removes the label - the
label, not a comment, is the signal to resume.

## Hard rules

- **Never merge**, not even a PR you authored that is approved. Merging is
  external automation: once `lgtm` and `approved` are both present and the
  required checks pass, the system merges. Your account holds `triage` on
  `upstream` - no write access, push to your fork only - so you cannot push
  or press merge. But `triage` can still apply labels, and the merge is
  triggered by label state, so the next rule is a rule you obey, not a
  permission that stops you.
- **Never apply `lgtm` or `approved` to your own PR.** Prow applies both in
  the normal flow, but your `triage` account can apply any label, including
  these two - so this is a rule, not a permission. Applying both to your own
  PR is the one way you could cause a merge; you must not. You do not
  self-approve.
- **Never approve, request changes, or `/lgtm` on any PR.** A review a human
  asks you for goes in as a comment (`gh pr review --comment`), never as
  `APPROVE` or `REQUEST_CHANGES`, and never carrying `/lgtm` or `/approve`.
  Prow reads an approving review as `/lgtm` and a changes-requested one as
  `/lgtm cancel`: from an account the `OWNERS` files name that moves the merge
  gate, and from yours it draws a refusal - while the `CHANGES_REQUESTED` left
  behind stops the automatic reviewer request from asking a human unless
  `options.robot_accounts` in `.github/auto_request_review.yml` names your
  account; `/request-review` overrides it either way. Either way it reads as a
  verdict, and an approver's own PR self-approves, so an `lgtm` there is the
  whole human half of the gate.
  `kube-agents-bot` lives by the same rule: an agent's read is not a vote. Say
  what you found, and leave what it is worth to the human
  ([`docs/pull-request-workflow.md`, "How a change merges"](../../docs/pull-request-workflow.md#how-a-change-merges)).
- **Never push to `upstream`.** Push PR branches to your fork and open the PR
  against `gke-labs/kube-agents`.
- **Never self-authorize.** No unreviewed change reaches tracked state.
- **Never touch another agent's issue or PR** beyond a review, and only review
  when a human asks you to. You do not gate anyone, and no one gates you.
- **Never file without evidence** (see [Before you claim or
  file](#before-you-claim-or-file)).

## The review you will receive

Opening a PR starts `kube-agents-bot`. The path to merge:

1. Every review thread (the bot's and any human's) gets a reply, and `main`
   requires all conversations resolved before it can merge. Resolve a thread
   only once genuinely resolved, per the root `AGENTS.md`; a declined bot
   finding stays open with its reply until a human rules on it - an approval,
   or a reply in the thread that accepts the decline; a reply that asks for
   the fix owes it - then resolve it, citing the ruling.
2. Reach a human reviewer: a green bot pass - clean on the first review, or
   nothing above Medium and the description answered (the body edited, not
   just its thread resolved) on a later one, per
   [what the check means](../../docs/pull-request-workflow.md#what-the-check-means) -
   or the bot's third round, whichever comes first. Comment `/review` after a
   push that fixed something (`/review all` for a wider re-check, and
   `/review fresh` after an edit to the body alone, since a plain `/review` on
   an unchanged commit re-cuts the earlier review without reading the body
   again), at most twice; at the third reviewed commit, or after a round that
   pushed nothing, comment `/request-review` if no human is on it yet, and
   stop; the workflow's hand-off comment (opening
   `<!-- auto-request-review:handoff -->`) means one was requested already,
   so stop - the command on the comment's first line, then a line telling the
   reviewer where the fix for any 🔴 High is and that the bot re-runs on
   their `/review`. `/request-review` also assigns one immediately when a
   review never arrives. It reacts 👀 to the comment when it requested someone
   and 😕 when it declined; the workflow run's annotations say why. Once the
   check is green or a reviewer is requested, self-service is over: reply in
   each thread and wait. A reply from them gets a reply; only their
   changes-requested review or their `/review` owes one more round (step 1 of
   the loop).
3. Merge is external automation: it fires when `lgtm` _and_ `approved` are
   both present and the required checks pass. You never merge.

See [`AGENTS.md`](../../AGENTS.md#automated-review-after-opening-a-pull-request)
for the contract and
[`docs/pull-request-workflow.md`](../../docs/pull-request-workflow.md#the-automated-review)
for the mechanics. You do not merge, you do not apply `lgtm`/`approved`, and you
do not need to shepherd the PR beyond confirming it eventually lands.
