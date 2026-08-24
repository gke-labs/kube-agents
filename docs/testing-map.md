# Where tests go

`AGENTS.md` owns the rule: decide by asking whether a model call is in the loop. This page is the
mechanics behind it — the full set of homes, what runs each one, and the traps that make a
misplaced test look fine.

## The nine homes

| What you are testing                                                                       | Where it goes                                                                                    | What runs it                                                                           | On a pull request                                                                                   |
| ------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------ | -------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------- |
| A Python module's own logic                                                                | beside the module; the exact directory set is the `PYTHON_TEST_DIRS` globs at `Makefile:129-142` | `make test-python`                                                                     | runs, unconditionally                                                                               |
| A shell script, a rendered manifest, an installer — something with no module to sit beside | `tests/test_*.py`, and `tests/memory/` for the memory provider                                   | `make test-python`, and `agent-startup-test.yml` for the startup subset                | runs, unconditionally                                                                               |
| Two components across a seam, no model call                                                | `tests/integration/test_seam_*.py`                                                               | `make test-python`                                                                     | runs, unconditionally                                                                               |
| The bench harness itself — verifiers, parsing                                              | `bench/tests/`                                                                                   | `make test-bench`                                                                      | runs, unconditionally                                                                               |
| The Go operator                                                                            | `k8s-operator/`                                                                                  | `make -C k8s-operator test`                                                            | paths-filtered: runs only when the change touches `k8s-operator/**` or `agents/platform/scripts/**` |
| An agent plugin                                                                            | `agentplugins/*/tests/test_*.py`                                                                 | `agentplugins-test.yml`                                                                | paths-filtered: runs only when the change touches `agentplugins/**`                                 |
| An agent, graded against a planted defect                                                  | `bench/tasks/<name>/task.yaml`                                                                   | `hack/ci-eval-pr.sh`                                                                   | reports; whether it blocks is Prow config this repository cannot read                               |
| A live journey through a deployed install                                                  | `bench/cuj/test_<NN>_<name>.py`, or `bench/cuj/<area>/` under it                                 | `uv run --project bench pytest -s bench/cuj`                                           | nothing runs it, by design                                                                          |
| The release gate                                                                           | `tests/e2e/`                                                                                     | `rc-release-pipeline.yml` on a three-hourly schedule, and `e2e-gchat-test.yml` by hand | nothing — it gates releases, not pull requests                                                      |

Three of those rows carry a footnote that matters more than the row.

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
and `make bench-case-check` enforces it. Read it before writing a case;
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
