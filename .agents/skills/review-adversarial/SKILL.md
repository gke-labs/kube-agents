---
name: review-adversarial
description: Reviews a change adversarially — establishes what it claims to do, hunts candidates from ten angles, then re-derives every candidate from source as a hostile second reader and reports only what survives.
---

# Task

Given a diff range, find the defects in it and report only the ones you can defend. This skill is
the review method itself and holds no plumbing: it is run by an author against their own branch
before opening a pull request (the `AGENTS.md` requirement), and by a reviewer against a pull
request already open (`.claude/commands/pr-review-batch.md`, which wraps it in the GitHub-side
work).

`review-docs-drift` is the companion pass, not part of this one. Angle H stops at the rules the
diff visibly breaks and leaves the rest of the documentation question to that skill.

# Procedure

## 1. Run this in a context that did not write the change

If you are the agent that just produced the diff, **do not run the pass in the conversation that
produced it**. Spawn a subagent, or start a new session, and hand it the diff range and the
repository — not your reasoning, not your plan, not the summary you were about to write. A model
reviewing work it has just justified is the weakest configuration there is: it is poorly calibrated
about its own output, rates it higher than an outsider would, and the bias is worst on exactly the
lines it got wrong. It is also more likely to "fix" something correct than to catch something
broken, which is why step 5 exists and why step 6 will not let you edit on a hunch.

That is why `.claude/commands/pr-review-batch.md` gives each pull request its own subagent. A
self-review gets the same treatment for the same reason.

You will be tempted to skip this on a small diff. The cost of a fresh context is one subagent; the
cost of skipping it is that the pass reports what you already believed.

## 2. Fix the diff range

Everything below is measured against one range, so name it before you start:

```bash
git diff --stat "$BASE"...HEAD     # three-dot: against the merge base, not the base tip
```

`$BASE` is `main` for a branch you are about to propose, or the branch the pull request targets.
The three-dot form keeps unrelated base-branch drift out of scope.

Read the changed files at their **post-merge state**, not just the hunks. A hunk shows you what
moved; the enclosing function tells you whether it still works.

## 3. Establish intent

Write down, in one sentence, **what this change claims to do**. Take it from the issue it closes,
the pull request body, or — for a branch with neither — the change itself. Keep that sentence: it
is the yardstick for Angle I, and it is what tells you whether a given hunk belongs here at all.

Two failure modes. Do not let the stated intent talk you out of a defect — "known limitation,
follow-up" does not make a dropped guard correct, though it does change how you phrase the
finding. And do not treat the stated intent as a description of the diff: where the two disagree,
the diff is what merges. A description promising a behaviour the diff does not implement, or
silent about one it does, is itself a finding.

## 4. Find candidates (ten angles)

Work all ten angles yourself, in sequence. Do not skip an angle because an earlier one found
nothing there, and do not let one angle's conclusion suppress another's — if two angles flag the
same line for different reasons, record both.

Each angle surfaces up to six candidates, each with a `file`, a `line`, a one-line `summary`, and a
concrete `failure_scenario`. Pass every candidate with a nameable failure scenario through to step
5 — silently dropping half-believed candidates is the dominant cause of misses. A candidate you
cannot express as a failure scenario is not yet a candidate.

**Angle A — line-by-line diff scan.** Read every hunk, line by line. Then read the enclosing
function for each hunk — bugs in unchanged lines of a touched function are in scope, since the
change re-exposes or fails to fix them. For every line ask: what input, state, timing, or platform
makes this line wrong? Inverted or wrong conditions, off-by-one, nil/undefined deref, missing
`await`, unchecked `err`, falsy-zero checks, wrong-variable copy-paste, an error swallowed in a
catch, unescaped regex metacharacters.

**Audit error handlers and fallback values**: for every `catch`, `except`, `if err != nil`,
`if ! cmd` (or `$? != 0`), or fallback assignment on a failed resolution, trace the fallback value.
Does it preserve the invariant downstream code assumes — a normalized 40-character SHA, a validated
SemVer, an absolute path, a non-nil struct — or does it pass a raw, unvalidated input forward? Check
if error suppression (`|| true`, `_ = err`, `except: pass`) transforms a failed lookup on a
malformed input into a benign empty result (`nil`, `""`, `[]`), silently disabling downstream
collision, uniqueness, or security guards.

**Angle B — removed-behavior auditor.** For every line the diff deletes or replaces, name the
invariant it enforced, then find where the new code re-establishes it. If you cannot find it,
that's a candidate: a removed guard, a dropped error path, a narrowed validation, a deleted test
that was covering a real case, a loosened RBAC or NetworkPolicy rule.

**A weakened check is a removed invariant**, and it is the one an agent reaches for when a gate
will not go green, so audit CI separately and by name: a test removed, renamed, or marked skip or
xfail; a coverage threshold lowered; `|| true`, `continue-on-error`, or a silenced exit status
appended to a step; a workflow that no longer triggers on pull requests or on forks; a step newly
gated behind a condition it did not have. Any of these is a candidate on its own — the failure
scenario is the class of defect that gate was catching, now shipping unobserved. **A diff whose
only changes are to test files, on a branch whose CI was failing, is a candidate until you can
show the test was wrong.** That is the shape of a change that makes the build green without making
the code right.

**Angle C — cross-file tracer.** For each function, template, chart value, or CRD field the diff
changes, grep for its consumers and check whether the change breaks any of them: a new
precondition, a changed return shape, a renamed key a manifest still reads, a timing dependency.
Trace runtime wiring through to the source — which container an env var lands in, which process
reads a port, which service account a binding actually grants — rather than inferring it from
names.

**Audit sibling contract symmetry**: when reviewing code in a family of components (reconcilers,
CLI tools, admission webhooks, API handlers), compare how identical domain inputs (a commit, a
cluster name, a tag, credentials) are validated and handled across siblings. An unmotivated
asymmetry — where one component hard-fails on an unresolvable entity while a sibling falls back
silently — is an immediate candidate.

Then trace the other direction, because a change written by an agent can call things that do not
exist: for every symbol, method, flag, chart value, CRD field, environment variable, or file path
the diff **introduces a reference to**, open its definition. A plausible name is not evidence. The
repo already applies this to prose — identifiers verified against source, not against other
docs — and code gets it for the same reason. Watch for the neighbours: an import added for a symbol
nothing uses, a dependency added without a pin or a provenance, a value hard-coded where the
surrounding code reads configuration, and files changed that the stated intent never mentioned.

**Angle D — operations and security.** This repo provisions clusters and holds credentials, so
weigh blast radius: IAM and RBAC scope, credential handling and redaction, NetworkPolicy reach,
what an agent is newly permitted to do, and whether a failure mode degrades or destroys. Check
that third-party GitHub Actions are pinned to a full commit SHA with the version in a trailing
comment.

**Angle E — reuse.** The angles above hunt for bugs; this one and the next two hunt for cleanup in
the changed code. Flag new code that re-implements something the codebase already has — grep
shared and adjacent modules, and name the existing helper to call instead.

**Angle F — simplification and efficiency.** Flag unnecessary complexity the diff adds: redundant
or derivable state, copy-paste with slight variation, deep nesting, dead code left behind. And
wasted work: repeated I/O, independent operations run sequentially, blocking work added to startup
or a hot path. Name the simpler or cheaper form that does the same job.

**Angle G — altitude.** Check that each change sits at the right depth rather than being a fragile
bandaid. Special cases layered onto shared infrastructure are a sign the fix isn't deep enough —
prefer generalizing the underlying mechanism.

**Angle H — conventions and docs.** Read the `AGENTS.md` / `CLAUDE.md` files that govern the
changed code: the repo root, plus any in a directory that is an ancestor of a changed file (a
directory's file only applies at or below it). Flag a violation only when you can quote the exact
rule and the exact line that breaks it — no style preferences, no "spirit of the doc" inferences.
Name the file and quote the rule so the report can cite it. This is also where docs drift belongs:
one canonical home per fact, generated `<!-- BEGIN GENERATED -->` regions regenerated rather than
hand-edited, identifiers verified against source rather than against other docs. `review-docs-drift`
is the exhaustive form of that check and the author is required to have run it before opening —
which is a reason to read what they reported, not a reason to skip this angle.

**Angle I — scope and test coverage.** Hold the diff against the intent sentence from step 3. Flag
changes that do not serve it: an unrelated refactor riding along, a dependency bump nobody asked
for, a behaviour change buried in a change described as a rename, reformatting that inflates the
diff and hides the real hunks. Repo convention is scoped changes and no unrelated formatting, so
cite the rule when it applies. Judge by whether a change serves the stated intent, not by how large
it is — a big diff that does one thing is in scope, and a three-line change that does a second
thing is not.

Then check that the intent is actually tested: for each behaviour the change claims, name the test
that would fail if that behaviour regressed. Where there is none, the candidate is the untested
behaviour, not the absent test — say which regression would ship silently. Bug fixes without a
regression test, and new error paths nothing exercises, are the usual cases.

**Do not treat green test suites as proof of correctness**: a passing suite proves only that the
paths it exercises work on the fixtures it supplies. For every validation check, gate, and error
path, check whether **negative inputs** (unresolvable identifiers, malformed strings, absent fields)
are tested against the gate, or if the tests only pass valid fixtures through the happy path. Treat
previously resolved findings in PR history as high-risk areas where adjacent defects and edge cases
cluster.

For a change that fixes something, naming the test is not enough — **run it against the pre-change
behaviour and watch it fail.** A test that passes with the fix reverted is testing something else,
and a fix with no test that can be made to fail usually means the defect was not understood. Say
which it was. A red-then-green test is the one answer in this whole pass that does not come from
your own reading, which is what makes it worth more than any amount of staring at the diff.

**Angle J — sibling pull requests.** Every angle so far has looked only at this change. Widen
once, to the open pull requests touching adjacent paths:

```bash
gh pr list --repo gke-labs/kube-agents --state open --limit 100 --json number,title,author,files
```

Three things come out of this that nothing else can see. A finding already accepted on a sibling
usually applies here unchanged — apply it rather than rediscovering it. A near-identical change
that has diverged is itself a finding: name which copy carries the fix and which does not, because
merge order then decides whether the fix survives. And where one change is a superset of another,
say so — reviewing the subset in isolation spends effort on a diff that may never merge.

For the cleanup, altitude, conventions, scope, and sibling candidates the `failure_scenario` states
the concrete cost — what is duplicated, wasted, harder to maintain, out of scope, or which rule or
untested behaviour is at risk — instead of a crash. Correctness bugs always outrank them when
something has to be cut.

Prefer running things over reasoning about them: execute the test suites the change touches and
reproduce the failures you claim.

## 5. Verify every claim (this step is not optional)

Dedup first: candidates pointing at the same line and the same mechanism collapse into the one with
the most concrete failure scenario.

Then take each surviving candidate and re-derive it from the source as if you were a hostile second
reviewer trying to get it thrown out. Open the actual file and read the actual code path — do not
re-read your own notes, and do not accept a claim because it sounded right when you wrote it.
Confirm the mechanism, not just the conclusion: a real defect reached by an imaginary code path is
still a wrong finding. Assign each one a verdict:

- **CONFIRMED** — you can name the inputs or state that trigger it and the resulting wrong output,
  crash, or misconfiguration. Quote the line.
- **PLAUSIBLE** — the mechanism is real but the trigger is uncertain (timing, environment, cluster
  state). State what would confirm it. Realistic-but-unproven is PLAUSIBLE, not REFUTED:
  concurrency races, nil on a rare-but-reachable path (error handler, cold cache, absent optional
  field), falsy-zero treated as missing, off-by-one on a boundary the code does not exclude, a
  regex or allowlist that lost an anchor.
- **REFUTED** — factually wrong (the code doesn't say that), provably impossible (show the type,
  constant, or invariant), already handled in this diff (cite the guard), or pure style with no
  observable effect. Quote the line that proves it.

Keep CONFIRMED and PLAUSIBLE. Then rewrite the finding list so it reflects only what survived:

- **Claim holds** → keep it as written.
- **Claim holds but the mechanism, severity, line number, or blast radius is wrong** → rewrite the
  finding to the corrected version. The finding now reads as though the corrected version is what
  you found in the first place.
- **REFUTED, or you cannot verify it** → delete it entirely. Do not demote it to a footnote, a
  "worth checking" aside, or a parenthetical. If the underlying uncertainty is genuinely worth
  someone's time, restate it as an open question in its own right, with the uncertainty stated
  plainly — never as a correction to something you previously asserted.

Then clean up after the edit, because these are what give away a second pass:

1. Re-sort findings by severity (`BLOCKER` / `HIGH` / `MEDIUM` / `LOW`) and **renumber from 1 with
   no gaps**.
2. Update every cross-reference between findings to the new numbers.
3. Update any count in the prose ("three blockers") to match the surviving set.
4. Re-read the whole document once for tense and voice consistency.

**The finished review must read as a single confident first pass.** No "Correction", no
"Verification pass", no "on second look", no "an earlier draft said", no "downgraded from", no
diff-of-claims, no changelog of your own reasoning. The reader should have no way to tell that step
5 happened.

## 6. Act on what survived, and record the disposition

Every surviving finding gets a disposition, and there are only two:

- **Fixed** — name the commit or the hunk that fixes it.
- **Deliberately not fixed** — give the reason. A reason is an argument about this change: the
  path is unreachable for a stated invariant, the cost lands on a code path being deleted next
  week, the fix belongs to a separate issue that is now filed. "Out of scope", "pre-existing", and
  "will fix later" are not reasons on their own.

**Edit on CONFIRMED, report on PLAUSIBLE.** A CONFIRMED finding on your own change is a defect you
now know about, so fix it. A PLAUSIBLE one is a mechanism you could not pin down, and rewriting
working code to chase it is how a self-review makes a change worse than it started: the temptation
to "fix" something that was already right is the characteristic failure of reviewing your own work,
and it is strongest on the lines you were least sure about. Write it into the section as an open
question, say what would settle it, and leave the code alone unless the answer arrives. Recording a
PLAUSIBLE finding you did not act on is a complete disposition — it hands the reviewer the doubt
instead of a silent edit.

When the review is your own, before opening a pull request, that disposition list **is** the PR
body's **Self-Review** section. See `AGENTS.md`, "Pull Request Hygiene".

# Output

Severity-ordered findings, each with:

- an anchor (`file:line`),
- what is wrong, in one sentence,
- the concrete failure scenario,
- the verdict from step 5,
- the disposition from step 6.

Then two short sections:

- **Not findings, for the record** — things that look wrong but are fine, so the next reader does
  not re-litigate them.
- **What this pass did not cover** — angles you could not run, suites you could not execute,
  infrastructure you did not have. An honest gap is worth more than an implied one.

"No findings" is an ordinary outcome on a good change. Report it as one, alongside what you looked
for — never pad the list with something step 5 should have deleted.
