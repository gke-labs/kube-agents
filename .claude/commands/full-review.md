---
description: Review the whole branch in a clean context, fix, and review again until a pass finds nothing; only then push once and ask the bot
argument-hint: [pr-number] [base-branch]
---

Run the review loop on this branch until it is clean, against pull request **$ARGUMENTS** (the
number, if one is open; and the base branch, empty means `main`).

This is not `/pr-preflight`. That command runs each pass once and hands back a list. This one
repeats review and fix until a full read of the branch finds nothing above low severity, and only
then pushes, so `kube-agents-bot` reads one head instead of one per fix. It exists because a fix
made in the writing context is the next finding's most likely home: on one pull request here,
thirty strict passes each found one to four defects, most of them in the previous round's fix.

**You were asked.** Invoking this command is the request to delegate every review to a context that
did not write the change, and to loop without asking between rounds. Review nothing yourself. Your
job is the range, the mechanical gate, the fan-out, the fixes, and the push at the end.

## The loop

1. **Range.** Resolve the base with `review-preflight` §1: fetch it, stop if the fetch fails, three-dot
   against the merge base. The range is the whole branch every round, never the last commit.

2. **Mechanical gate, in this context.** Every test file the diff touches, three runs in a row (the
   timing-sensitive suites here go red one run in three); `make docs-check`; `prettier --check` on
   changed Markdown and YAML; `make shellcheck` on changed shell. Red here ends the round: fix it
   before spending a reviewer on it.

3. **One full read in a clean context.** Spawn `review-adversarial` over the whole range, per
   `review-preflight` §4 and §5, and hand it, beyond the range: the pull request body; every finding
   from the previous round with its disposition, so the pass verifies each fix and hunts for what the
   fix introduced; and the bot's findings so far on this pull request, open and resolved, read with a
   `reviewThreads(last:100)` query, because the families it has raised are the families it will raise
   again. Spawn `review-docs-drift` beside it on the first round and on any round in which a document
   changed. Name the commit under review in the prompt; a reviewer that starts as you push reads the
   wrong tree.

4. **Fix.** Every CONFIRMED finding is fixed in this round, with a test that goes red without the
   fix; run that test against the previous head to prove it. Every PLAUSIBLE finding gets a
   disposition that argues about this change. Fold both into the body's **Self-Review** as one entry
   per round, not one per finding, and keep **Testing** and **Live validation** current (`AGENTS.md`,
   "Keep these sections current, not chronological").

5. **Again.** Go back to step 2. The loop ends when a round's pass returns no finding above low
   severity and reports the previous round's fixes verified. Five rounds without reaching that is a
   stop, not a push: report where it stands and what keeps surfacing, and let the user decide.

## The push, once

6. **Push.** One push carrying every round. Then wait until `gh pr view --json headRefOid` returns the
   commit you pushed; the bot resolves the head when the `/review` comment arrives, and a comment
   posted twenty seconds after the push has been read against the previous head.

7. **Threads.** Reply to each of the bot's open threads with the commit that answered it, then
   resolve it; a thread the fix did not answer stays open with the reason. Then `gh pr edit
--body-file` with the folded body. Only then post `/review` on a line of its own.

8. **Read the pass.** When it lands, a clean pass is the end. Findings are the input to step 2: the
   next round's clean-context reviewer gets them verbatim, and the loop runs again before anything
   is pushed.

## What not to do

- Do not review in this context, however small the delta. The last two defects on the pull request
  this was written for were in ten-line fixes that looked too small to hand out.
- Do not narrow a later round to the files that changed. Fixes ripple; the range is the branch.
- Do not push between rounds to "see what the bot says". Each push is a full bot pass and a smoke
  run, and the answer is the same list a clean-context pass would have given you first.
- Do not post `/lgtm`, `/approve` or `/request-review`; the loop ends at a clean bot pass, and a
  human reviewer is assigned from there.
