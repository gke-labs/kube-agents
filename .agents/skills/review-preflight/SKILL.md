---
name: review-preflight
description: Runs the review passes required before a pull request is opened — adversarial and docs-drift — each in a context that did not write the change, and merges what they return into one disposition list.
---

# Task

Get every pre-PR review pass into a context that did not write the change, then merge what comes
back into the list that becomes the pull request's **Self-Review** section.

This skill is plumbing. It holds no review method — [`review-adversarial`](../review-adversarial/SKILL.md)
and [`review-docs-drift`](../review-docs-drift/SKILL.md) own that — and it does not state the
requirement: "Pull Request Hygiene" in [`AGENTS.md`](../../../AGENTS.md) does, and that list wins if
this file disagrees with it.

[`.claude/commands/pr-preflight.md`](../../../.claude/commands/pr-preflight.md) is the Claude Code
wrapper. Any harness can follow this file directly.

# Procedure

## 1. Fix the diff range

One range, shared by every pass. Fetch the base first — a base you have not refreshed silently
widens the range with commits that already merged, and the passes then review other people's work
as though it were yours:

```bash
# The remote pointing at gke-labs/kube-agents is not reliably called `upstream`, and
# on a clone of the upstream repository rather than a fork it is `origin`. Find it.
BASE_BRANCH=main                                  # or the branch you will target
BASE_REMOTE=$(git remote | while read -r r; do
  case "$(git remote get-url "$r")" in *gke-labs/kube-agents*) echo "$r"; break;; esac
done)
# HTTPS, not SSH. The fallback fires when no remote matches — a plain clone of a fork,
# which every open pull request here comes from — and that is where a sandbox or a CI
# runner has no key. The repository is public, so HTTPS needs no credential at all.
: "${BASE_REMOTE:=https://github.com/gke-labs/kube-agents.git}"

git fetch "$BASE_REMOTE" "+refs/heads/$BASE_BRANCH:refs/kube-agents-base/$BASE_BRANCH" || {
  echo "base fetch failed — stop here rather than reviewing against a stale base" >&2
  exit 1
}
BASE=refs/kube-agents-base/$BASE_BRANCH

git diff --stat "$BASE"...HEAD     # three-dot: against the merge base, not the base tip
```

Three-dot keeps base-branch drift out of everyone's scope. If the fetch fails, stop and say so:
there is no safe fallback, because the wrong base fails in the direction that hides findings.

`BASE_BRANCH` is the branch name as the remote has it, so reduce whatever you were handed to that
first. `origin/main`, `refs/heads/main`, and the `refs/kube-agents-base/main` a previous run wrote
all go into the refspec verbatim, ask for a branch no remote has, and trip the guard above over a
base that was fine.

Skip the fetch only for a base that cannot go stale — a commit SHA, or a tag. That a name resolves
is not that test: a local `main` forty commits behind resolves, and so does a remote-tracking ref
last fetched a week ago, which is the ordinary state of a checkout here — "Branch from a `main` you
have just fetched" in `AGENTS.md` is there because of it. Fetching a base you already fetched costs
a round trip; the other way costs the passes reviewing commits that merged last week as though this
branch wrote them.

## 2. Which passes run

- `review-adversarial` — always.
- `review-docs-drift` — always.

Both, on every change. Neither has a trigger to evaluate and there is no third pass, so this step is
a checklist rather than a judgment — but say which ran anyway, and say it when one of them did not.
A pass nobody ran is a gap in the section, not an absence of findings.

## 3. Run the mechanical gate first, in the main loop

```bash
make docs-check
```

Every time, whatever the range touches: CI runs it unfiltered on every pull request. It is cheap, it
is deterministic, and its output is a fact the passes would otherwise re-derive, so hand the result
on. It does not substitute for its pass — it covers generated regions, links, terminology, map
coverage, and the always-loaded instruction files' context budget, but not whether the prose is
still true, which is the whole of what `review-docs-drift` asks.

It is here because the passes want its output, not because it is the local check you owe.
`AGENTS.md` "Local Validation Checks" is that list — prettier on changed Markdown and YAML,
the Docker build, the image-layer budget, `go build` in `k8s-operator/` — and a clean preflight
discharges none of it.

## 4. Get each pass a context that did not write the change

One subagent per applicable pass, spawned together so they run concurrently. The main loop fixes the
range, runs the gates, fans out, and relays — it reviews nothing itself. `review-adversarial` §1 has
the argument for why; the short form is that a context holding the reasoning behind a diff re-derives
that reasoning instead of testing it.

**The delegation has to be requested, and that is the problem this skill exists to solve.** Coding
agents are instructed not to spawn subagents on their own initiative — Claude Code ships a standing
instruction to that effect, and other harnesses carry their own version of it — so an agent reading
only the requirement finds the one route it is told not to take, and quietly runs the pass inline
instead. In order of preference:

- **The user invoked `/pr-preflight`, or asked for the review in words.** That is the request. Spawn
  the passes and do not ask again.
- **You got here on your own.** Ask once, before the pass, and wait: say you need a context that did
  not write the change, how many subagents that is, and that the alternative is a review by the
  context that already believes the change is correct. Ask when you hit it, not after.
- **The harness has no subagents.** Start a fresh session, or a headless run, handed the same
  material and nothing else — one per applicable pass, not just the adversarial one:

  ```bash
  claude -p "In $PWD, follow .agents/skills/review-adversarial/SKILL.md against
  refs/kube-agents-base/main...HEAD. Derive the change's intent from the diff. Do not read the
  branch's commit message bodies, plan files, or scratch notes. Report every finding and edit
  nothing; its step 6 is mine."
  ```

  Write the range out, substituting the ref §1 resolved, rather than `$BASE`. Most harnesses run
  each command in its own shell — Claude Code is one — so a variable §1 assigned is gone by the
  time you compose this one, while `$PWD` above is set afresh by every shell and survives. An
  empty `$BASE` is not an error either: `git diff ...HEAD` defaults the omitted left side to
  `HEAD`, so the pass reviews `HEAD...HEAD`, exits 0, and reports no findings on no diff. That is
  the silent wrong-range failure §1 exists to stop, arriving through the handoff instead.

  Neither of those trailing instructions is padding — step 5 explains what each one carries.

If none of those is available to you, **you are blocked, and the pull request waits**. Say what you
are blocked on. [`.agents/rules/pre_pr_review.md`](../../rules/pre_pr_review.md) is explicit that
an approval you could not get blocks this step rather
than waiving it, so the authoring context is not the fallback — running the pass there and
disclosing it is what you do when a human, told the above, tells you to proceed anyway. Then
**Self-Review** says which context ran the pass, in those words.

## 5. What to hand each pass, and what to withhold

Hand it: the repository, the diff range, the path to its skill, and the gate output from step 3.

**Say that the report is the deliverable and the pass writes nothing.** `review-docs-drift` already
refuses to fix silently, but `review-adversarial` §6 tells whoever runs it to edit on CONFIRMED, and
its Angle I asks for a test run against the pre-change behaviour — both of which mutate the tree. The
passes run concurrently in your checkout rather than one worktree each, so a pass that reverts a file
to watch a test fail is a file its sibling is reading at the same time.
`.claude/commands/pr-review-batch.md` carries the sentence for the reviewer side and this is its
author-side equivalent:

> The skill's step 6 does not apply to you: report every finding, edit nothing, and leave the tree as
> you found it. Dispositions are the caller's.

Step 6 below is where those edits happen, once, after the relay.

Withhold everything you know that it does not: your plan, your reasoning, the commit messages you
drafted, the summary you were about to write, which hunks you think are the risky ones, and which
findings you expect. Each of those tells the pass what to conclude.

Withhold your intent sentence in particular. `review-adversarial` §3 derives one from the change
itself, and the gap between what you meant and what the diff says is a finding you cannot get any
other way — supply the sentence and you have closed the gap by hand.

**Some of it you cannot withhold, so tell the pass not to read it.** A diff range is made of commits,
so every commit message you wrote travels with it, and the pass works in your checkout rather than a
clean worktree, so your plan and scratch files are in front of it too. Both usually state the intent
verbatim. Put the instruction in the handoff:

> Derive the change's intent from the diff. Do not read the branch's commit message bodies, plan
> files, or scratch notes.

Without that line the gap closes itself and neither of you notices.

## 6. Merge what comes back, and act on it

Dedupe across passes before reading them as a list. The passes overlap by design: angle H of
`review-adversarial` reaches into docs, angle D into security and operational blast radius. The same
defect found twice is one finding, kept in whichever form names the failure concretely.

Where two passes disagree, go and read the source. Do not average them and do not take the more
alarming one on the grounds that it is safer.

The two passes do not grade alike, and the merged list keeps each one's own severity rather than
translating it. `review-adversarial` returns a verdict per finding, so `review-adversarial` §6
governs its half: edit on CONFIRMED, report on PLAUSIBLE. `review-docs-drift` returns a
Blocking/Advisory triage instead and never emits a verdict — Blocking is addressed before the pull
request opens, Advisory gets the same disposition treatment as PLAUSIBLE. Do not restate a Blocking
finding as CONFIRMED to make one column of it; the pass did not do the verification that word
claims.

Every survivor gets a disposition either way, to the bar
[`.agents/rules/pre_pr_review.md`](../../rules/pre_pr_review.md) sets: fixed, or deliberately
not, with a reason that argues about this change.

Report what a pass only suspects rather than acting on it. A finding it could not pin down is an
open question for the pull request's **Self-Review** section, not a licence to rewrite working code
— chasing an uncertain finding on your own change is how a self-review makes it worse than it
started.

## 7. Re-runs

A fix changes the shared range, so re-run whatever the fix invalidated — not whichever pass raised
the finding. They are rarely the same set: rewriting a paragraph to answer an adversarial finding is
exactly how the prose starts contradicting another document, which is docs-drift's question and not
one `make docs-check` can answer. Work that still holds stays, and is not re-run just to have been
run against the new head; `AGENTS.md` states that rule and this restates it.

New context, same handoff. Feeding the previous round's findings into the re-run defeats the point
of the fresh one.

# Output

One severity-ordered disposition list covering every pass that ran. Severity and confidence are two
axes, and a finding carries both: its pass's severity (`BLOCKER`/`HIGH`/`MEDIUM`/`LOW` from
`review-adversarial`, Blocking or Advisory from `review-docs-drift`) orders the list, and its
CONFIRMED/PLAUSIBLE verdict or triage says how sure the pass was. Name which pass each came from, so
a reader can tell what kind of check stands behind it. Above the list, three lines the reviewer
cannot reconstruct from the findings:

- which passes ran, and which were skipped and why. A pass that found nothing reports what it
  looked for — its angles are the vocabulary for that, and a clean result naming none of them is
  indistinguishable from no pass having run;
- for each, what kind of context it ran in — subagent, fresh session, or the one that wrote the code;
- what the passes could not cover: suites not run, infrastructure absent, angles refused.

That list is the pull request's **Self-Review** section. "No findings" is an ordinary result, and a
complete one only alongside what was looked for.
