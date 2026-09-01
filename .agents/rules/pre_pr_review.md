# Pre-PR review mechanics

[`AGENTS.md`](../../AGENTS.md) owns both rules below — that adversarial self-review and live
validation are required before opening a pull request, and that each is recorded in the pull
request body. This file holds the mechanics of carrying them out. Change the rule in `AGENTS.md`;
change how it is done here.

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

## Live validation

The rule, and the requirement to fill in the template's **Testing → Live validation** section, are
in `AGENTS.md` under Pull Request Hygiene.

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
