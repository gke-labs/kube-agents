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
[`docs/contributing.md`](../../docs/contributing.md) (the CLA). This document
does not restate them - it only adds the agent-to-agent loop.

## Scope

Agents **do not review one another's work unless asked to by a human.** Review
and approval are human responsibilities (assisted by `kube-agents-bot`); merge
is automated once a human approves and the system applies `lgtm`. An agent's
job ends at "resolve every review comment and get the human to approve." The
only coordination _between_ agents is the claim, below.

## The loop

Run on your own cadence. Each cycle, take the **first** step below that has
work, perform **one** action, then stop. Do not do two things per cycle.

1. **Own open pull requests first.** For each of your open PRs
   (`gh pr list --author @me --state open`), check for new review events,
   inline comments, and check status. Address every finding - fix and push, or
   answer it in the thread. Resolve each thread only once it is genuinely
   resolved - the bar is owned by the root `AGENTS.md`. After making changes,
   re-run the review: comment `/review` for a narrow re-check of the diff, or
   `/review all` for a wider re-check when the changes are substantial. If
   `lgtm` is present, you are done - the system merges; you never do. If
   `do-not-merge/hold` is present, read the comment explaining why and wait.
2. **Continue in-progress work.** If you have an assigned issue with a branch
   in progress, continue it.
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
not own.

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
2. Comment, `@mention`ing the relevant maintainer (see `OWNERS` /
   `OWNERS_ALIASES`), stating: what blocks you, what would unblock you, and who
   can unblock it.

The label plus the mention **is** the escalation - there is no other channel.
An issue carrying `needs-human` is not claimable: your claim filter must skip
it. After applying `needs-human`, stop working the issue until a human removes
the label - the label, not a comment, is the signal to resume.

`needs-human` is for decisions and blockers, on an issue or a PR.

## Hard rules

- **Never merge**, not even a PR you authored that is approved. Merging is
  automated: a human approves, the system applies `lgtm`, then merges. Your
  account holds `triage` on `upstream` (write on your fork only), so you
  _cannot_ merge - this rule documents what permissions already enforce.
- **Never apply `lgtm` or `approved` to your own PR.** Approval is a human
  signal, and `lgtm` is applied by the system - not by you. You do not
  self-approve.
- **Never push to `upstream`.** Push PR branches to your fork and open the PR
  against `gke-labs/kube-agents`.
- **Never self-authorize.** No unreviewed change reaches tracked state.
- **Never touch another agent's issue or PR** beyond a review, and only review
  when a human asks you to. You do not gate anyone, and no one gates you.
- **Never file without evidence** (see [Before you claim or
  file](#before-you-claim-or-file)).

## The review you will receive

Opening a PR starts `kube-agents-bot`. The path to merge:

1. Resolve every review thread (the bot's and any human's) - `main` requires
   all conversations resolved before it can merge. Resolve a thread only once
   genuinely resolved, per the root `AGENTS.md`.
2. Get a clean bot pass (`/review`, per [The loop](#the-loop)) - a human is
   assigned only after the bot's check goes green.
3. The human approves; the system applies `lgtm` and merges.

See [`AGENTS.md`](../../AGENTS.md#automated-review-after-opening-a-pull-request)
for the contract and
[`docs/pull-request-workflow.md`](../../docs/pull-request-workflow.md#the-automated-review)
for the mechanics. You do not merge, you do not apply `lgtm`/`approved`, and you
do not need to shepherd the PR beyond confirming it eventually lands.
