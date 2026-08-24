# Integration seam tests

The tier between unit tests and behavioural evals (`docs/designs/testing-strategy.md`
§4.1b): real components wired together, with the agent replaced by a fake. The
dividing line is one sentence — if a model call is in the loop, it is an eval; if
not, it is an integration test. That line keeps this tier deterministic, and
deterministic is what lets it block merges with no repetitions and no statistics.

## Status: probation

This directory is deliberately **not** in `PYTHON_TEST_DIRS`. The `integration` job
in `python-tests.yml` is its only runner, so a flake in a young seam test cannot
red the already-gating unit and coverage jobs. When the job has a green track
record and becomes a required check, the directory joins the unit sweep — remove
the exclusion in `scripts/test_test_discovery.py` and add the glob back in the
same change. Run locally with `make test-integration`.

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
  it is fixed. Whoever fixes the path deletes the decorator in the same change.
- Environment edits register cleanup on the test case; this directory runs under
  discovery, and a leaked variable poisons every module after it.
