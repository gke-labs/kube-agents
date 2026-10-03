---
name: review-address-feedback
description: "Address review feedback on an open pull request: triage and fix findings, reply in every thread, resolve the addressed ones, and request one re-read. Use after an automated or human reviewer reviews a pull request."
---

# Task

Work a review round until every thread has a reply and a re-read comes back clean. `AGENTS.md` and the rules it links win if this file disagrees, and unattended agents follow [`agents/contributor/AGENTS.md`](../../../agents/contributor/AGENTS.md) where it differs; [`docs/pull-request-workflow.md`](../../../docs/pull-request-workflow.md) holds the commands.

# Workflow

1. **Collect** every unresolved thread, review-body finding and red check. Read each against the current head and current `main`, not your checkout: a finding that reads wrong is often a stale checkout, and an outdated thread is not an addressed one.
2. **Cross-check the Self-Review.** A finding it recorded without a disposition, or claimed fixed when it is not, gets settled this round; one it rejected with a reason gets that reason as the reply.
3. **Triage** each: fix; push back with a reason about this change and its evidence; or defer with a filed issue ([`pre_pr_review.md`](../../rules/pre_pr_review.md)), or with an expected-fail eval case for an agent-behaviour gap you will not fix ([`eval_driven_development.md`](../../rules/eval_driven_development.md)). A finding that correctly applies a rule with no exemption is a fix, never a push-back. Read a red check's log before acting: a red your change caused is a fix; one it did not gets one retest if transient, otherwise a report to the check's owners.
4. **Drive toward merge.** A standing instruction from the user to address findings and move the pull request to merge is their decision: act on it, deciding each finding from that direction and the pull request's stated intent, and summarise each finding and its disposition to the user. Ask only without such an instruction or when neither settles a finding, and then with concrete options and a recommendation, never an open-ended question.
5. **Bracket each fix with evidence**, red before and green after: the eval loop for agent behaviour, live validation for runtime behaviour, or the one-line exemption those rules allow. Take the live-test lease before touching a shared install.
6. **Fix the class:** every site of the same shape in the lines this pull request writes and the functions around them, plus a test that fails on the shape where one can exist.
7. **Re-review non-trivial fixes** with [`review-preflight`](../review-preflight/SKILL.md) before pushing.
8. **Push once:** a push restarts CI.
9. **Convince with proof.** Reply in every thread with the fixing commit, the refutation, or the filed issue or case; answer review-body findings in one pull request comment. Ground every statement in something the reviewer can verify (a test, command output, a `file:line`, a design doc, or a design choice the user made for this pull request) and reason from it; make no unsourced claim.
10. **Request one re-read** of the new commits, unless the push already triggered one; never twice for one commit unless the first never arrived, and a draft gets none until it is marked ready. Re-request any human who asked for changes. When the round produced no commits, request a human reviewer instead. On new findings, return to step 1.
11. **Once the last re-read settles,** resolve each thread fixed by a commit or shown factually wrong. A disagreement stays open for the reviewer, including one your reply settles by a design choice. Fold the round into **Self-Review** and **Live validation** in place.
