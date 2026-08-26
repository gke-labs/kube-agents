# Where tests go

`AGENTS.md` owns the rule: decide by asking whether a model call is in the loop. This page is the
mechanics behind it — the full set of homes, what runs each one, and the traps that make a
misplaced test look fine.

## The nine homes

| What you are testing                                                                       | Where it goes                                                                                    | What runs it                                                                           | On a pull request                                                                                                 |
| ------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------ | -------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------- |
| A Python module's own logic                                                                | beside the module; the exact directory set is the `PYTHON_TEST_DIRS` globs at `Makefile:129-142` | `make test-python`                                                                     | runs, unconditionally                                                                                             |
| A shell script, a rendered manifest, an installer — something with no module to sit beside | `tests/test_*.py`, and `tests/memory/` for the memory provider                                   | `make test-python`, and `agent-startup-test.yml` for the startup subset                | runs, unconditionally                                                                                             |
| Two components across a seam, no model call                                                | `tests/integration/test_seam_*.py`                                                               | `make test-python`                                                                     | runs, unconditionally                                                                                             |
| The bench harness itself — verifiers, parsing                                              | `bench/tests/`                                                                                   | `make test-bench`                                                                      | runs, unconditionally                                                                                             |
| The Go operator                                                                            | `k8s-operator/`                                                                                  | `make -C k8s-operator test`                                                            | paths-filtered: runs only when the change touches `k8s-operator/**` or `agents/platform/scripts/**`               |
| An agent plugin                                                                            | `agentplugins/*/tests/test_*.py`                                                                 | `agentplugins-test.yml`                                                                | paths-filtered: runs only when the change touches `agentplugins/**`                                               |
| Whether the agent diagnoses a defect you planted for it                                    | `bench/tasks/<name>/task.yaml`                                                                   | `hack/ci-eval-pr.sh`, as the Prow presubmit                                            | runs as a presubmit and reports on the pull request; whether it blocks is Prow config this repository cannot read |
| Whether an install you already have still works for a user                                 | `bench/cuj/test_<NN>_<name>.py`, or `bench/cuj/<area>/` under it                                 | `uv run --project bench pytest -s bench/cuj`, by hand                                  | nothing runs it, by design                                                                                        |
| The release gate                                                                           | `tests/e2e/`                                                                                     | `rc-release-pipeline.yml` on a three-hourly schedule, and `e2e-gchat-test.yml` by hand | nothing — it gates releases, not pull requests                                                                    |

Four of those rows carry a footnote that matters more than the row.

**`bench/tasks/` and `bench/cuj/` both drive an agent, and they are the two most confusable rows.**
The difference is who supplies the failure and who owns the environment.

A `bench/tasks/` case is an **eval, and it runs in the Prow presubmit**. It plants a defect and the
run owns the environment the defect sits in: the case's `infrastructure.deployer` decides where,
with `tofu` provisioning a stack for the run and tearing it down after, and `noop` grading against
the cluster the deploy already stood up. The agent is pointed at it and its diagnosis is graded
against the case's `verification_spec`. The subject is the agent — the defect is known, and what is
in question is whether the agent finds it. That is why a case needs a `domain:` slug and
deterministic checks, and why adding one changes what a pull request reports.

A `bench/cuj/` journey is a **manual tier: a live black-box test you run by hand against your own
install, never part of the presubmit**. It plants nothing and provisions nothing. It talks to Kage
as a user through the admin portal API and scores only evidence the deployed system returned, so the
subject is the install: the agent is assumed to work, and what is in question is whether this
deployment is wired up. It needs a real installation to point at, which is why no CI job runs it,
why adding one changes nothing about what a pull request reports, and why it cannot gate anything.

Being manual is the design rather than a gap someone will automate later. The tier is what you reach
for after an install or an upgrade, to confirm the deploy landed, and while working a bug on a live
system — the cases where the question is about one deployment and no amount of CI could answer it.
Write a journey expecting to run it yourself, and do not expect anything to run it for you.

Rule of thumb: if you would have to break something on purpose for the test to be meaningful, it is
a `bench/tasks/` case. If you would run it against production to check the deploy landed, it is a
`bench/cuj/` journey.

**The two paths-filtered workflows report `success` on a pull request that ran nothing.**
`k8s-operator-test.yml` and `agentplugins-test.yml` both run `dorny/paths-filter` and then gate
every subsequent step on the result, so the job always completes and the check always goes green.
`k8s-operator-test.yml`'s own header comment says it: the job "reports `success` on a pull request
that ran no tests". A change that breaks an operator contract from outside `k8s-operator/**` gets a
green `Run Controller Tests` that compiled nothing.

**`tests/e2e/` is not manual-only.** `rc-release-pipeline.yml` runs it on `cron: "17 */3 * * *"`,
and `step-4-tag-validated` depends on it. Breaking a test there is not free — it reds the
release-candidate pipeline within three hours and stops the tag. The manual `e2e-gchat-test.yml` is
the second caller, not the only one. `tests/e2e/operator/agentplugins_e2e_test.py` is the exception
inside the exception: nothing runs it on any trigger, only by hand.

**A `*_e2e_test.py` suffix opts a plugin test out of CI, and `test_*.py` opts it in.**
`agentplugins-test.yml` discovers on `test_*.py`, which deliberately does not match the
`*_e2e_test.py` suites sitting in the same directory. Naming a live-infrastructure test
`test_dedup_e2e.py` rather than `dedup_e2e_test.py` joins it to the pull-request suite, where it
needs a Pub/Sub topic that CI does not have.

## Running on a pull request is not gating a merge

The last column says what a trigger and its `if:` conditions support, which is a weaker claim than
"blocks the merge". Which checks are actually required lives in branch protection on
`gke-labs/kube-agents` and in Prow config in `GoogleCloudPlatform/oss-test-infra`; neither is
readable from this repository, so neither is asserted here. `make verify` (`Makefile:161`) is the
local answer to the same question — everything a pull request must pass offline, in one target —
and [`site/src/content/docs/contributing.md`](site/src/content/docs/contributing.md) lists the
individual targets to run when you have touched a given area.

Per-tier detail lives with each tier: [`bench/cuj/README.md`](../bench/cuj/README.md) for adding a
journey, [`tests/integration/README.md`](../tests/integration/README.md) for the seam tier,
[`tests/e2e/README.md`](../tests/e2e/README.md) for the release gate, and
[`bench/README.md`](../bench/README.md) for running the evals that already exist.

For an eval case, [`bench-case-format.md`](designs/bench-case-format.md) is the contract and this
page does not restate it. It rules on what a `task.yaml` must carry — the `id`, the mandatory
`domain:` slug and `verification_spec`, the exact-versus-judged line, and which keys red a build —
and `make bench-case-check` checks it in about a second before you push. The target itself
runs in no workflow: `scripts/test_task_registration.py` calls the same validator and
asserts it returned no findings, and that lint is what gates, through `PYTHON_TEST_DIRS`
and `python-tests.yml`. Read the contract before writing a case;
[`bench/CUSTOM-TASKS.md`](../bench/CUSTOM-TASKS.md) is the walkthrough that sits under it.

## The trap that spans every tier

**A new test directory that no wildcard reaches never runs.** `make test-python` discovers from
`PYTHON_TEST_DIRS`, a list of thirteen globs at `Makefile:129-142`. A directory the globs miss fails
nothing — it sits unexecuted and the suite reports green around it, which is how eight test files
stayed unrun for months. Adding a directory means adding its glob in the same change.
`scripts/test_test_discovery.py` fails the build if you forget, and its `EXCLUDED` dict is where a
directory goes that must deliberately not run, with the reason it must not.

This is the one that catches people who put a test in a reasonable-looking place. The equivalent
traps for eval cases — a missing `domain:` slug counting as coverage of nothing, and registering in
`TASKS` before the activation blockers in [`../bench/tasks/DRAFTS.md`](../bench/tasks/DRAFTS.md)
clear — are in [`bench-case-format.md`](designs/bench-case-format.md), enforced rather than
described.
