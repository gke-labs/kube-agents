---
# Claude Code loads this rule only beside files matching `paths`; other tools ignore this block.
paths:
  - ".github/PULL_REQUEST_TEMPLATE.md"
  - "docs/pull-request-workflow.md"
---

# Pre-PR review mechanics

[`AGENTS.md`](../../AGENTS.md) owns both rules below — that adversarial self-review and live
validation are required before opening a pull request, and that each is recorded in the pull
request body. This file holds the mechanics of carrying them out, and what the automated review
shares with the first of them. Change the rule in `AGENTS.md`; change how it is done here.

## Adversarial self-review

The rule, and the requirement to fill in the template's **Self-Review** section, are in
`AGENTS.md` under Pull Request Hygiene.

- **Run the pass in a context that did not write the change** — a subagent, or a new session,
  handed the diff range and nothing else. Not your plan, not your reasoning, not the summary you
  were about to write. Reviewing a diff in the conversation that produced it is the one
  configuration that reliably does not work: the same context that talked you into the code
  talks you into approving it, and the blind spot sits exactly where you were already wrong.
- **`/pr-preflight` is how you get one**, and it covers the docs-drift pass — the second required
  pre-PR pass, stated in `AGENTS.md` beside this one — at the same time. It wraps
  [`.agents/skills/review-preflight/SKILL.md`](../skills/review-preflight/SKILL.md), which
  holds the plumbing and the rules for what to do with what comes back. Read the skill directly if
  your harness has no slash commands. Invoking the command is also the request to delegate that an
  agent is otherwise told to wait for — coding agents are instructed not to spawn subagents on
  their own initiative, which is why `AGENTS.md` names the command in the rule itself rather than
  leaving the only route to it on this page, which an agent reaches only after deciding to look.
- **If your harness will not spawn one without a human's approval, go and get the approval.** A
  setting that requires sign-off before starting a subagent blocks this step; it does not waive
  it. Ask when you hit it, not after the review, and say what you are blocked on. Quietly running
  the pass in the session that wrote the code instead buys a review from the context that already
  believes the change is correct, and reporting that as a self-review without the caveat tells
  the reviewer something untrue about how the change was checked.
- **Every finding gets a disposition: fixed, or deliberately not with a reason that argues about
  this change.** "Out of scope", "pre-existing", and "will fix later" are not reasons on their
  own; the separate issue you filed is. Fix what a pass confirms and report what it only
  suspects — a finding it could not pin down is an open question for the section, not a licence
  to rewrite working code. And "no findings" is an answer only alongside what you looked for: a
  pass that names none of its angles is indistinguishable from no pass.
  [`.agents/skills/review-preflight/SKILL.md`](../skills/review-preflight/SKILL.md) §6
  elaborates, including how to merge two passes that grade differently.
- **Do not claim more than you did.** A self-review the diff contradicts is worse than none: it
  spends the reviewer's trust before they reach the code. Name the kind of context each pass ran
  in — subagent, fresh session, or the one that wrote the change — so the claim above it is
  something a reviewer can weigh rather than take on trust.
- **The automated review runs the same skill.** `.github/kube-agents-bot.yml`, read from this
  repository's default branch, nominates
  [`review-adversarial`](../skills/review-adversarial/SKILL.md) as `kube-agents-bot`'s playbook,
  and the bot reads the skill from the pull request's base commit — for a pull request into
  `main`, a version this pull request did not write. Its candidate passes work the skill's angles
  less angle J; a step the skill says to run is reasoned about and named as not run, never
  dropped. The bar is the bot's own — a confidence score and a floor that differs between the
  automatic review and `/review` — not step 5's keep-what-survives rule, though the re-derivation
  step 5 asks for is the same. So a finding in one pass and not the other can be the floor, the
  missing shell, or the model, and is worth reading as any of the three rather than dismissed as
  variance. With the playbook in force, prose the change makes false and a guard or test the
  change claims that does not cover what it says are candidates the bot raises rather than
  classes its false-positive list suppresses — bounded as the bot's own prompt bounds them, so
  "this should be documented" and "this needs more tests" stay excluded — and the wave still
  scores them. Changing the skill changes what the bot looks for on every pull request after the
  change merges. The bot's own README and design document are canonical for the key and the
  mechanism.

## Live validation

The rule, and the requirement to fill in the template's **Testing → Live validation** section, are
in `AGENTS.md` under Pull Request Hygiene.

- **For a change to what an agent does, the live validation is the eval loop**: a case seen
  red against `main` and green three times against the branch, per
  [`eval_driven_development.md`](eval_driven_development.md). The bullets below are the form for
  runtime changes that do not alter agent behaviour.
- **Name the install and what you observed.** Cluster, image tag, operator version; what you
  did; and the result at each layer the change claims to touch — the CR `.status`, the
  Deployment env, the file or process inside the pod.
- **Prove the mechanism, not a coincidence.** If the new value happens to equal the old
  default, the observation proves nothing. Set something distinctly different, then revert and
  confirm it goes back.
- **Say what you could not cover, and why**, rather than implying full coverage. Clean up test
  artifacts, restore prior state, and note anything left behind.
- **Screenshots of graphical surfaces go through `scripts/pr_evidence_screenshot.sh`**, which
  publishes the image where a PR body can render it and prints Markdown stamped with the
  commit and capture time. Command output stays as fenced text transcripts — a screenshot of a
  terminal is evidence degraded, not evidence.
- **If the install is shared with other agents, take the lease.**
  `scripts/live_test_lease.py` holds it as a ConfigMap in the install's own namespace. Copy
  `.claude/settings.json.example` to `.claude/settings.json` once per checkout and its
  `PreToolUse` hook claims the lease for you on the first mutating command and denies the
  command while somebody else holds it — so two agents cannot overwrite each other's live
  validation. Without the copy nothing is enforced, and you run `acquire` and `release` by hand.
  [`docs/designs/live-test-lease.md`](../../docs/designs/live-test-lease.md) covers what counts as
  a mutation, how an install is discovered, and why the wiring is not committed.
- **If the change cannot reach a running installation** — docs-only, a CI workflow, a code path
  that needs infrastructure you do not have — write "Not live-tested" and say why. An empty
  section is not an answer.
