# The local eval runner

`bench-run` (`bench/kube_agents_bench/runner.py`) is how a developer runs bench cases against an
install of their own. This document is the reasoning behind it; the command reference is
`bench/README.md`, "Running evals", and the gate it defers to is `docs/designs/eval-scorer.md`.

## The problem

The repository asks for eval-driven development: a change to what the agent does starts from a
failing case and ends with the same case passing three times on the branch
(`.agents/rules/eval_driven_development.md`). Until this runner, the developer's tooling for that
loop was the stock `devops-bench` command, which runs one task once, plus whatever shell loop they
wrote around it. The presubmit's loop, in `hack/ci-eval-pr.sh`, could not be borrowed: it assumes
Prow's environment, a Boskos lease and a ledger token minter, and it is two thousand lines.

Three things followed. Nobody ran a case more than the required three times, because each
repetition was a hand-typed command. Nobody compared a branch's three against `main`'s record,
because the record lived in a dashboard and a GCS store rather than beside the terminal. And three
passes were read as proof, when on most cases they are what `main` produces on an ordinary day.

## Three repetitions do not decide a targeted change

The presubmit's three repetitions are sized for a different question: whether a case collapsed,
which the gate defines as failing all three (`docs/designs/testing-strategy.md`, section 4.2). For a
developer asking whether their change moved a case, three is rarely enough, and the arithmetic is
worth having in front of you.

A case's record on `main` is `K` passes in `N` repetitions, with `N` in the hundreds for every
presubmit case. A branch run is `k` passes in `n`. The right test at `n = 3` is Fisher's exact test on
the 2x2 table, two-sided; the gate's aggregate rule is a flat margin that is only meaningful at suite
scale, and `bench/kube_agents_bench/scoring.py` says a normal approximation is no substitute at
`n = 3`. Against a case at 0.87 on record, three passes give `p = 1.0`; three failures give
`p = 0.002`. So three repetitions can catch a collapse on a reliable case, which is what the gate
uses them for, and cannot show an improvement on it at all: showing that a case at 0.87 reached 1.0,
at the conventional 0.05 and 0.8 power, takes about 27 consecutive passes at today's record sizes.
A case at 0.20 shows a rise to 0.60 in ten. A case at 0.00, like the silence cases, shows a rise to
0.30 in five.

`bench-run plan` computes exactly that per case (`bench/kube_agents_bench/stats.py`,
`reps_to_detect`): the smallest `n` at which the Fisher test has the asked power against the
baseline for a rise or a drop of the asked effect. Its output ranks the selection by that number,
which is the answer to "which evals are worth my afternoon": the ones that can show a change of the
size I am after within the repetitions I can afford. The estimate uses the dashboard's median
duration per case, so it also prices the run.

## Stop when the answer is in, or cannot come

A targeted change is the case where "got lucky" matters most: a change to one skill, one case run
three times, a merge on three passes. `--until-decided` replaces the fixed count with a stopping
rule. After `--reps`, the runner keeps launching repetitions of a case while three things hold: the
Fisher p-value against the baseline has not crossed `--alpha`; some completion of the remaining
repetitions up to `--max-reps` could still cross it (`can_still_decide` checks the two extremes,
every remaining run passing and every one failing, because the p-value is monotone in each
direction); and the ceiling is not reached. The result is `better`, `worse`, or `undecided` with
the count it took, and an `undecided` at the ceiling is a real answer: this case cannot show this
change in this many runs, and the pull request should say so rather than quote three passes.

The rule is sequential and the p-values are not corrected for it, deliberately. This is a
developer's stopping rule, not the gate's; its job is to spend repetitions where they inform and to
name the uncertainty, and the presubmit still runs its own three under its own rules. A developer
who wants a stricter bar for a merge argument passes a smaller `--alpha`.

## Where the baseline comes from

The dashboard's `data.json` carries two records per case, republished every fifteen minutes: the nightly's,
which runs `main` and is the record a developer wants, and the pooled record of every pull-request
presubmit build, which is what the top-level pass rate is. The nightly is young and holds a handful
of runs per case, so the runner takes it once it holds twenty runs, the gate's own admission window,
and the presubmit record until then, and labels which one every number is against (`src`: `main` or
`prs`). The presubmit record is pull-request branches, most of them `main` plus one change; it is a
serviceable stand-in for planning and an honest label matters more than the difference. The runner
reads the file with `gcloud storage cat` and caches it for an hour (`bench/kube_agents_bench/
priors.py`). The evidence store the gate itself consults (`docs/designs/eval-scorer.md`) was not
used because it is keyed by version and needs an object-viewer grant most developers do not hold;
for planning and comparison a few infrastructure rows in the denominator move nothing.

A previous run set's `summary.json` is accepted in the same place. That is the A/B: `main` built and
run on the same install as the branch, on the same day, which removes the install and the model
version from the comparison. `bench-run compare` does the same test between two run sets.

## What it shares with the presubmit, and what it does not

Each repetition is the presubmit's own subprocess, `devops-bench <task> --agent-type kubeagents`,
under the presubmit's delegation ceilings (2700 seconds, 3000 for the six audit-shaped cases; the
harness's own default of 1800 would cut those audits short and grade as failures what the presubmit
grades as passes), so what the harness sends and how devops-bench scores are unchanged. Each
repetition is graded in-process by the scorer the gate uses
(`kube_agents_bench.scoring.classify_rep`), so `pass`, `fail`, `infra` and `blocked` mean what they
mean in a verdict. Scheduling copies the presubmit's locks: repetitions of one case run in series,
because concurrent identical prompts put identical cards on one board and stop being comparable;
different cases overlap up to `--parallel`; a tofu stack holds the one infra lock; each unit has its
own local port and launches are staggered so the first model calls do not burst. `--overlap-reps`
lets a case's repetitions overlap for speed, which the presubmit never does, and never applies to a
case that provisions a stack or writes a shared artifact. Each unit runs in its own session and the
runner is the only thing that signals it: on Ctrl-C, `kill -INT`, the hard timeout or a sibling's
crash, a unit gets exactly one SIGINT (devops-bench tears a stack down in a `finally` that runs on
SIGINT and on nothing else, and a second one would abort the destroy), a grace sized for that
teardown, then termination. The queue is dropped, and the summary of what completed is written, so an
interrupted run set is still readable.

It does not build or deploy the agent, read or write the evidence store, apply the gate's admission
and collapse rules, or run the judge comparison. Its `summary.json` is evidence for a pull request's
Live validation section and input to the next `bench-run`, nothing more; the presubmit remains the
only run whose verdict counts.

## Open

- The dashboard's `runs_on_record` counts infrastructure rows the pass rate excludes, so the pass
  count is recovered by rounding. Publishing the two counts in `data.json` would make it exact.
- `plan` sizes for one effect. A power curve per case would be more honest and harder to read; the
  single number with `--effect` adjustable was chosen for the terminal.
- The scripted `devops-bench` the tests use (`bench/tests/fake_devops_bench.py`) is, in outline,
  the fast guard the grading pipeline lacks: a test on every pull request that pushes a scripted
  agent through the real harness and checks the verdict. Extending it to that is separate work.
