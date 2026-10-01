---
# Claude Code loads this rule only beside files matching `paths`; other tools ignore this block.
paths:
  - ".github/PULL_REQUEST_TEMPLATE.md"
  - "docs/pull-request-workflow.md"
---

# Pre-PR review mechanics

[`AGENTS.md`](../../AGENTS.md) owns the rules: keep pull request descriptions brief, rely on
automated tests rather than manual testing, and run the pre-PR review passes (`review-adversarial`
and `review-docs-drift`) before opening a pull request. This file holds the mechanics of running
those passes.

## Adversarial and docs-drift self-review

- **Run the passes in a context that did not write the change** — a subagent, or a new session,
  handed the diff range and nothing else. Not your plan, not your reasoning, not the summary you
  were about to write. Reviewing a diff in the conversation that produced it is the one
  configuration that reliably does not work: the same context that talked you into the code
  talks you into approving it, and the blind spot sits exactly where you were already wrong.
- **`/pr-preflight` is how you get one**, covering both `review-adversarial` and
  `review-docs-drift` at the same time. It wraps
  [`.agents/skills/review-preflight/SKILL.md`](../skills/review-preflight/SKILL.md), which
  holds the plumbing and the rules for what to do with what comes back. Read the skill directly if
  your harness has no slash commands.
- **If your harness will not spawn one without a human's approval, go and get the approval.** A
  setting that requires sign-off before starting a subagent blocks this step; it does not waive
  it. Ask when you hit it, not after the review, and say what you are blocked on.
- **Fix confirmed findings before opening the PR; do not paste the review report into the PR body.**
  Fix what a pass confirms and surface any open questions to the user. Keep the pull request body
  itself brief and focused on _why_ the change is being made.
- **The automated review runs the same skill.** `.github/kube-agents-bot.yml`, read from this
  repository's default branch, nominates
  [`review-adversarial`](../skills/review-adversarial/SKILL.md) as `kube-agents-bot`'s playbook,
  and the bot reads the skill from the pull request's base commit — for a pull request into
  `main`, a version this pull request did not write.

## Automated tests, not manual testing

- **Do not perform or describe manual/live testing for pull requests.** The code and its automated
  tests (unit tests, integration tests, or evals) must stand on their own.
- **On a bug fix, include an automated regression test** that fails without the fix and passes with
  it. Do not write a prose section in the PR body arguing why the bug will not recur; let the test
  in the diff prove it.
