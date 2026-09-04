# Integration seam tests

The tier between unit tests and behavioural evals (`docs/designs/testing-strategy.md`
§4.1b): real components wired together, with the agent replaced by a fake. The
dividing line is one sentence — if a model call is in the loop, it is an eval; if
not, it is an integration test. That line keeps this tier deterministic, and
deterministic is what lets it block merges with no repetitions and no statistics.

## Status: gating

This directory is in `PYTHON_TEST_DIRS`. `make test-python` runs it, the `test` job
in `python-tests.yml` runs it, and a red seam test is a red pull request. It spent
its first weeks on probation in a job of its own so that a flake in a young suite
could not red an already-gating check; it came through without a failure, and a
tier nothing gates on is a tier people learn to merge around. The separate
`integration` job is gone — one check, run once.

Two things follow from that, and both bite people who do not know them:

- **Install a Go toolchain before you trust a green run.** `test_seam_injector_kv.py`
  compiles and runs the real Go client from `k8s-operator/cmd/k8s-event-watcher`
  against the live Python server, and the whole class is `skipUnless(GO)`. Without
  `go` on `PATH` those four tests skip and the run still prints `OK`, which is
  indistinguishable from having run them. CI's `test` job sets up Go for exactly
  this reason.
- **An `expectedFailure` that starts passing now fails the build.** That is the
  design, not an accident: the pins below assert the contract we want, so the day
  the product code satisfies one, unittest reports an unexpected success and the
  gate goes red until the decorator is deleted. See the rule under
  "Adding a seam test" — it now has teeth.

Run just this tier while working on a seam with `make test-integration`; it is a
convenience, not the definition of what must pass.

## Adding a seam test

One file per seam, `test_seam_<name>.py`, stdlib unittest. `_seams.py` carries the
shared fixtures: the real `session_kv_server` in a subprocess with a controlled
environment, argv-recording fake executables for PATH, and a recording stub HTTP
server for the one component a seam deliberately excludes.

Rules the existing files follow:

- Real components on real transports (sockets, pipes, files); fakes only at the
  seam's ends, and a fake that records is worth two that answer.
- No model calls, no cloud calls, minutes not hours.
- A behaviour that is broken on `main` today is pinned with
  `@unittest.expectedFailure` asserting the **desired** contract, with a comment
  naming the mechanism — the suite documents the breakage and flips loudly when
  it is fixed. **Whoever fixes the path deletes the decorator in the same change.**
  Now that the tier gates, this is not a courtesy: unittest treats an unexpected
  success as a failure, so a fix that leaves the decorator behind turns the `test`
  job red on the pull request that made things better. Two pins are live today,
  both on the autoops alert path (`test_seam_alert_path.py`,
  `test_seam_chat_ingress.py`); repairing that path is out of scope for the tier,
  which catalogs the breakage rather than fixing it.
- Environment edits register cleanup on the test case; this directory runs under
  discovery, and a leaked variable poisons every module after it.
