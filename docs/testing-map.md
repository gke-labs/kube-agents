# Where tests go

`AGENTS.md` owns the rule: decide by asking whether a model call is in the loop. This page is the
mechanics behind it — the full set of homes, what runs each one, and the traps that make a
misplaced test look fine.

## The eight homes

| What you are testing                          | Where it goes                                                                                         | What runs it                                 | Runs before a merge                                                                  |
| --------------------------------------------- | ----------------------------------------------------------------------------------------------------- | -------------------------------------------- | ------------------------------------------------------------------------------------ |
| A Python module's own logic                   | beside the module — `agents/*/scripts/`, `deploy/docker/patches/`, `scripts/`, `admin_console/tests/` | `make test-python`                           | yes, on every pull request                                                           |
| Two components across a seam, no model call   | `tests/integration/test_seam_*.py`                                                                    | `make test-python`                           | yes, on every pull request                                                           |
| The bench harness itself — verifiers, parsing | `bench/tests/`                                                                                        | `make test-bench`                            | yes, on every pull request                                                           |
| The Go operator                               | `k8s-operator/`                                                                                       | `make -C k8s-operator test`                  | yes, on every pull request                                                           |
| An agent plugin                               | `agentplugins/*/tests/`                                                                               | `agentplugins-test.yml`                      | yes, on every pull request                                                           |
| An agent, graded against planted findings     | `bench/tasks/<name>/task.yaml`                                                                        | `hack/ci-eval-pr.sh`                         | reports on every pull request, but the Prow job is `optional: true` and never blocks |
| A live user journey through the portal        | `bench/cuj/<area>/test_<NN>_<name>.py`                                                                | `uv run --project bench pytest -s bench/cuj` | no — nothing runs it, by design                                                      |
| The release gate                              | `tests/e2e/`                                                                                          | `e2e-gchat-test.yml`, manual dispatch        | no                                                                                   |

Per-tier detail lives with each tier: [`bench/CUSTOM-TASKS.md`](../bench/CUSTOM-TASKS.md) for
writing an eval case, [`bench/cuj/README.md`](../bench/cuj/README.md) for adding a journey,
[`tests/integration/README.md`](../tests/integration/README.md) for the seam tier, and
[`bench/README.md`](../bench/README.md) for running the evals that already exist.

## Three traps

**A new test directory that no wildcard reaches never runs.** `make test-python` discovers from
`PYTHON_TEST_DIRS`, a fixed list of globs in the `Makefile`. A directory the globs miss fails
nothing — it sits unexecuted and the suite reports green around it, which is how eight test files
stayed unrun for months. Adding a directory means adding its glob in the same change.
`scripts/test_test_discovery.py` fails the build if you forget, and its `EXCLUDED` dict is where a
directory goes that must deliberately not run, with the reason it must not.

**A `bench/tasks/` case with no `domain:` slug counts as coverage of nothing.** Coverage is
accounted per domain against [`designs/domains.yaml`](designs/domains.yaml) and enforced by
`scripts/test_domain_coverage.py`. Covered also means running: a case registered commented-out in
`hack/ci-eval-pr.sh`'s `TASKS` array is progress, not coverage, and a domain whose only scenario is
dormant is still uncovered.

**Registering a case in `TASKS` is the last step of activation, not the first.**
[`../bench/tasks/DRAFTS.md`](../bench/tasks/DRAFTS.md) carries the per-scenario activation blockers,
and every dormant scenario is waiting on at least one of them. When a case does activate, note that
the presubmit holds `VerificationCorrectness` to a floor of `1.0`: a two-objective case goes red on
one miss, with no partial credit. Prefer `tool_called` and structural assertions over matching
phrases in model prose — a text match against what a model chose to write is a flake generator
against a floor with no margin.
