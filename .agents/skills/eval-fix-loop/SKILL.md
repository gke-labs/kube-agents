---
name: eval-fix-loop
description: Take one failing eval case (or one root cause behind several), diagnose it from its recorded runs, classify the miss, record a red, make the smallest allowed fix, prove three greens, and open the pull request. Use when a nightly or local run shows a red that someone must turn green, or when a repair list names a root cause.
---

# Eval fix loop

One root cause per loop. A classified cause with quoted evidence before any code. Never loosen a check to fit a model's wording. Three greens on the final head. Markers flip in the same pull request.

## First-day addendum (2026-11-02)

1. Before anything, take one row from the batch file the planner names (`~/.claude/handoffs/cert-batch-<date>.md`, outside the repository) and write your session name in its claims column.
2. Read the store state first: `expected_fail` on `main`, passes and fails per model, `fail` against `not_applicable`.
3. Search open issues and open pull requests three ways: the case name, the failing check's reason string, the mechanism. A fix in flight ends the loop with a page row note.
4. The red is on the record: quote the store rows and two failing run directories, and replay them through the check as it is on `main`.
5. For a check-only fix the green is the replay of every recorded answer with the new check: all addressed fails pass, zero passes turn to fails. Three live greens are for an agent or product change only.
6. A wording check admits every phrasing of the fact and excludes nothing; a safeguard grades the action. Narrowing is measured on the record.
7. Pin every wording check through the whole check, never with a regex alone: the helper `check_passes` in `bench/tests/conftest.py` once #4530 merges, until then the four-line form (`transcript.set`, then `ReportContainsVerifier(**check).verify(5.0).status`).
8. Prefer the case folder. A change under `bench/kube_agents_bench/` must still work when the presubmit grades with the base commit's bench.
9. A case whose wrong answer may open a pull request the reply never names declares `writes_alone: true` (a key that lands with #4530); it then runs alone, and the lane safeguard charges it any write in its window.
10. The body keeps every sentence at 25 words or fewer. It says `Closes #<issue>` and names the former owner as reviewer. It carries `Part of #3978`, the certification epic, no `Co-Authored-By` trailer, and ends with "Generated with the help of Claude Code."
11. One session, one PR: mind it to the first human lgtm, then hand it to the PR minder with a written handoff. Post `/review` only if the bot did not review on open.
12. Step 8 is "send the numbers": record them in your own status with `cd ~/ka-overseer && python3 overseer/ovsr.py report --from <session>` : fails flipped, passes kept, the PR number, the page row note. Message the Overseer only for an ask. The store takes the night at the morning store read.

## Inputs

- A case id under `bench/tasks/<id>/`, or a root cause with its case ids.
- The run directories that show the failure: a nightly build's `artifacts/` in the local cache, or a local `bench/results/run_*/`.
- A checkout with the case, its fixture (`bench/tf/prebuilt/<stack>/` or the seeded fleet), and the product code behind it: the procedure (`agents/platform/skills/<skill>/` SOP markdown), the collector scripts (`agents/platform/skills/<skill>/scripts/`), the checks (`bench/kube_agents_bench/verifiers.py`).
- The rules: `.agents/rules/eval_driven_development.md` governs red, green and registration; this skill adds the diagnosis and the refusals.

## Step 1: pick and brief

Write a one-screen brief before reading anything else in depth:

- Case id, domain, audit stream, the install model and release of the failing runs.
- Pass counts per model from the record (`bench/run-store/runs/` if present, else the run directories).
- The failing checks by name, with their `reason` strings quoted verbatim from `results.json` `verification_report[]`.

## Step 2: diagnose from the runs

Read in this order, and quote what you find with its source path:

1. `results.json` → `verification_report[]`: which checks failed, which passed, and the exact reasons.
2. `results.json` → `output`: the agent's final report. Does it name the target resource? Does it name the planted object under a different finding? Does it say anything was "unevaluated", "unjudged", "skipped", a "coverage gap", or "not applicable"?
3. `results.json` → `trajectory[]`: did the required collector or script run (`tool_called`, `terminal` commands)? What did the collector print? Grep for the target resource, the namespace, and words like `unevaluated`, `skipped`, `error`, `429`, `timeout`.
4. The ledger or pull request the check read, when the check names one (`ledger_issue_contains` reasons name the issue): what finding ids does it carry? A ledger is rewritten by every later run, so when the live issue has moved on, recover the body the check graded from the issue's edit history (GraphQL `userContentEdits { editedAt diff }` on the issue) rather than reading the current one.
5. For a fleet-audit case, the collector manifest and the findings document the run wrote in the sandbox, copied out during the run since the next run overwrites them: `kubectl -n kubeagents-system exec platform-agent-shell-0 -c shell -- cat /opt/data/scratch/manifest_<audit>.json` (per cluster, `candidates` and `checks_not_applicable`) and the same for `findings_<audit>.json`.
6. What those two files split, which the ledger alone cannot: the collector never listed the object (collector gap); the collector listed it and the worker dropped it (agent behaviour); the finding is in the document and the ledger body left it out (ledger rendering).
7. The fixture: was the planted state really there (the safeguard's reason tells you; a `resource_property` pass proves the object existed with that value)?
8. The product code the case exercises: the collector predicate or the procedure line that should have produced the finding. Find where the slug the check wants would be emitted. If nowhere, say so.

## Step 3: classify

Pick exactly one class, by the first rule that matches, and write the rule you used:

| Class            | Rule that selects it                                                                                                                                                                                                                                                                                           | Fix type allowed                                                                                                                                                                                                               |
| ---------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Fixture defect   | The safeguard or the planted object is absent, or the planted state differs from the scenario (wrong namespace, wrong value, pods not in the state the scenario describes)                                                                                                                                     | Fix the stack; re-run red                                                                                                                                                                                                      |
| Check defect     | The report or ledger shows the right outcome, but the check reads it wrong: a phrase list the report does not use, a type compare (string against boolean), a shortened id, a display-line parse                                                                                                               | Make the check read state or the ledger id, not words; add the missing phrasing only beside a state check; never delete the check                                                                                              |
| Collector gap    | The collector ran and logged that it skipped, could not evaluate, or does not know the object or shape (for example a ComputeClass priority of a kind it does not parse)                                                                                                                                       | A predicate or a parser branch in the collector, with a test                                                                                                                                                                   |
| Procedure gap    | The collector saw the state, the procedure has no check for the pattern, and no finding was emitted                                                                                                                                                                                                            | A new check in the procedure with a published source line, and the collector support it needs                                                                                                                                  |
| Ledger rendering | The finding is in the run's findings document and absent from the ledger body: the body budget rendered the higher severities in full, listed some without detail, and dropped the rest, so no `Where:` line carries the object (the `audit-findings-all` ids, clipped at 100 characters, are all that remain) | A renderer change with a test (every finding keeps one line with its check and object); until it lands, the case lands as `expected_fail` naming the renderer issue, since no check edit can read what the body does not carry |
| Agent behaviour  | The procedure has the rule, the collector gave the facts, and the agent did not follow the rule (skipped a step, reworded a verdict, did not open the required tool)                                                                                                                                           | Move the rule out of prose: a script that prints the verdict line, a check, or a persona rule the persona loads on every run                                                                                                   |
| Role set         | The agent lacked a permission or an API the scenario needs, and the report says so                                                                                                                                                                                                                             | A permission change proposed to the humans; never a Secret grant                                                                                                                                                               |
| Infrastructure   | A 429, a timeout, a lost lease, a missing token, a forge-less install for a ledger check                                                                                                                                                                                                                       | Not a red; fix the environment and re-run                                                                                                                                                                                      |

Write the class with three evidence quotes and one sentence on why the next-nearest class does not apply. If two classes apply to different checks of the same case, split: one loop per class.
Then, for any class but Infrastructure, write the Step 3b card; no code and no run beyond the record before it. An Infrastructure red is fixed and re-run first, with no card.

## Step 3b: the decision card

One card per loop, written from the record after Step 3. It goes to the human first; code starts on the human's word. Fixed headings, this order, each within the size its text names.

1. **The case in customer terms.** Two sentences: who asks, and what they should get back.
2. **What the agent does.** One verbatim line per failing shape, its count over the record, and the `file:line` of the code that produced the text.
3. **Root cause.** One paragraph: the Step 3 class, the evidence line that selects it, and why the next-nearest class does not apply.
4. **Behaviour or case.** In two sentences, say whether the case's bar is the product's own stated rule, and quote the rule. If the case is loose or unbound, give the one-line tweak: bind the claim to the planted object, or require the thing the customer must see.
5. **What the customer wants to see.** Before and after, real text, each under 10 lines. The after carries the verdict, the one proving number, and the one thing they did not ask but should know.
6. **The fix, one lever per cause.** One paragraph per lever. Prose for a prompt contradiction, code for a code behaviour, a fixture change for a fixture gap; never prose to cover code. Give the size in lines, the resource cost in requests and objects, and where it lives; name a Hermes patch as one, with the note that it moves when Hermes goes. When one root cause takes two levers, both halves ship in one pull request.
7. **Do less.** The one-line option, and what it would certify falsely.
8. **Live run needs.** One line each: install, forge, fixture, model, minutes.
9. **Not covered.** At most three bullets.
10. **Rec and principle.** One line each.

The card and the fix it proposes obey these rules:

| Do                                                                           | Do not                                                     |
| ---------------------------------------------------------------------------- | ---------------------------------------------------------- |
| Name the model of every red and every green.                                 | Report a green on one model as a green on another.         |
| Bind every check to the planted object.                                      | Grade a sibling finding as the planted one.                |
| Keep to one lever per cause.                                                 | Use prose to cover a code behaviour.                       |
| State a limit as a ceiling ("stays under").                                  | State a limit as a target ("aim for").                     |
| Show the customer the verdict, the proving number and the one unasked thing. | Patch wording round after round when a state check exists. |
| Count server-side, with a field or label selector.                           | List everything and filter locally.                        |
| Put numbers in a table and link every pull request and issue.                | Add a constant or a mechanism for its own sake.            |
| Keep every claim on the record, with its run directory or line.              | Widen the fix mid-loop without a new card.                 |
| Stop after the card and wait for the word.                                   | Take a review clear as push authority.                     |
| Let the review check's green or the third round assign the human reviewer.   | Request a human reviewer by hand.                          |

## Step 4: red

Run the case once, unchanged, on an install the check can be graded on (a ledger check needs a forge; a plain-report check does not). Keep the run directory and quote the failing reason. Use the lease tool; the install's model is the model you report.

## Step 5: fix

Make the smallest change the class allows, on a branch from fresh `upstream/main`:

- A check gains a state or ledger read; a phrase list grows only beside one; a type compare becomes a typed compare.
- A collector gains the predicate or parser branch, with a unit test on the shape that was skipped.
- A procedure gains the check and its `source:` line.
- A behaviour rule moves into a script, a check, or the persona file, and the prose line that failed is removed or shortened.

Refuse: a fixture name or a test id in product code; a phrase list edited to match one model's report; two root causes in one branch; a change that makes the eval pass without changing what the product does for a customer.

## Step 6: green

On the final head: run the case three times; run every sibling case of the same root cause once; read the second model's record for the case if the store has one. All three must pass on the deterministic checks. A judged score moving is not a pass.

## Step 7: ship

Open the pull request from the template: the red (install, model, commit, failing check and reason, one line of the report), the three greens (run directories), the class and its evidence, the markers flipped (`expected_fail: true` removed for the cases the fix proves), the registration line, and the self-review from `/pr-preflight`.

## Step 8: record

Append the runs to the run store and move the case's row in the suite status file from red to fixing, with the pull request number.

## Cost to expect

A check defect: one to two hours and four runs. A collector or procedure gap: half a day to a day, and up to forty runs when a family of cases shares the cause. An agent-behaviour fix needs an install serving the model the rule failed on.
