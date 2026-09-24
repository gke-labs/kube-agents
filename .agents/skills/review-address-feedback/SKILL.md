---
name: review-address-feedback
description: "Address review feedback on an open pull request: verify each finding, fix the whole defect class in one batch, push once, reply to and resolve every thread, and re-request review once. Use after an automated or human reviewer reviews a pull request."
---

# Task

Close a review round with zero open threads and one clean re-review. `AGENTS.md` wins if this file disagrees; [`docs/pull-request-workflow.md`](../../../docs/pull-request-workflow.md#resolving-conversations) holds the commands.

# Workflow

1. **Collect** every unresolved thread, review-body finding and red check into one list. Check each against the head merged into current `main`: a finding that reads wrong is often a stale checkout, and one on an older commit may already be answered.
2. **Cross-check the Self-Review.** A finding it already raised is a missed disposition; fix it this round.
3. **Triage** each: fix; push back with a reason about this change and its evidence; or defer with a filed issue ([`pre_pr_review.md`](../../rules/pre_pr_review.md)). A finding citing a rule with no exemption is a fix, never a push-back.
4. **Get the user's decision** before changing code, unless they already authorized fixing findings without asking. Whenever a disposition is uncertain, ask with concrete options and a recommendation, never an open-ended question.
5. **Fix the class.** Grep every site with the same shape, fix them all, and add a test that fails for the shape.
6. **Start the evidence first:** the eval loop for agent behaviour ([`eval_driven_development.md`](../../rules/eval_driven_development.md)), live validation for runtime behaviour. Take the live-test lease before touching a shared install; restore what you found.
7. **Re-review non-trivial fixes** with [`review-preflight`](../review-preflight/SKILL.md) before pushing; it costs less than a reviewer round.
8. **Push once.** Every push restarts CI.
9. **Reply in every thread** once the fixes are pushed: name the commit for a fix, or say why the finding is wrong against current `main`.
10. **Re-request review once**, only after pushing new commits: never before the first automatic review lands, never twice for one commit. On new findings, return to step 1.
11. **Resolve each addressed thread** once the last re-review settles: the fix is on the head, or the finding is shown wrong. Leave judgment calls open for the reviewer.
12. **Fold the round** into **Self-Review** and **Live validation** in place at the same time.
13. **Never approve or merge** on anyone's behalf.
