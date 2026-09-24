---
name: review-address-feedback
description: "Address review feedback on an open pull request: triage and fix findings, reply in every thread, resolve the addressed ones, and request one re-read. Use after an automated or human reviewer reviews a pull request."
---

# Task

Work a review round until every thread has a reply and a re-read comes back clean. `AGENTS.md` wins if this file disagrees, and unattended agents follow [`agents/contributor/AGENTS.md`](../../../agents/contributor/AGENTS.md) where it differs; [`docs/pull-request-workflow.md`](../../../docs/pull-request-workflow.md) holds the commands.

# Workflow

1. **Collect** every unresolved thread, review-body finding and red check. Read each against current `main`, not your checkout: a finding that reads wrong is often a stale checkout, and an outdated thread is not an addressed one.
2. **Cross-check the Self-Review.** A finding it recorded without a disposition, or claimed fixed when it is not, is a fix this round; one it rejected with a reason gets that reason as the reply.
3. **Triage** each: fix; push back with a reason about this change and its evidence; or defer with a filed issue ([`pre_pr_review.md`](../../rules/pre_pr_review.md)). A finding that correctly applies a rule with no exemption is a fix, never a push-back.
4. **Drive toward merge.** Decide each finding from the pull request's stated intent and the user's direction for it. Ask only when neither settles the disposition, and then with concrete options and a recommendation, never an open-ended question.
5. **Bracket each fix with evidence**, red before and green after: the eval loop for agent behaviour ([`eval_driven_development.md`](../../rules/eval_driven_development.md)), live validation for runtime behaviour. Take the live-test lease before touching a shared install.
6. **Fix the class:** every site of the same shape in what the change touches, plus a test that fails on the shape where one can exist.
7. **Re-review non-trivial fixes** with [`review-preflight`](../review-preflight/SKILL.md) before pushing.
8. **Push once:** a push restarts CI.
9. **Convince with proof.** Reply in every thread with the fixing commit or the refutation. Ground every statement in something the reviewer can verify (a test, command output, a `file:line`, a design doc, or a design choice the user made for this pull request) and reason from it; make no unsourced claim.
10. **Request one re-read** of the new commits: never before the first automatic review lands unless it never arrives, never twice for one commit. On new findings, return to step 1.
11. **Once the last re-read settles,** resolve each thread answered with a commit or a refutation, leave judgment calls open, and fold the round into **Self-Review** and **Live validation** in place.
