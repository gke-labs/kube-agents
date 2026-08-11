# `runner/` — the runner contract

One interface for every agent execution:

```
run(principal, profile, task, workspace, budget) -> stream of events
```

The design rationale, the field-by-field breakdown, and the open questions live in
[`docs/architecture/09-runner-contract.md`](../docs/architecture/09-runner-contract.md). This file
is the short version for someone about to edit this directory.

**No production runner implements the contract yet.** The null runner is the only conforming
implementation; pointing the suite at Hermes is a later milestone.

## Layout

| File                               | What it is                                                                                      |
| ---------------------------------- | ----------------------------------------------------------------------------------------------- |
| `contract/run_request.schema.json` | The request. Seven required top-level fields, `additionalProperties: false`.                    |
| `contract/run_event.schema.json`   | A `oneOf` over seven event types sharing one envelope.                                          |
| `schema.py`                        | Constants, `request_errors` / `event_errors`, `check_*` raisers, a `new_request()` builder.     |
| `jsonschema_mini.py`               | Stdlib validator for the keywords the schemas use. Raises on any keyword it does not implement. |
| `conformance.py`                   | The suite, as a mixin.                                                                          |
| `null_runner.py`                   | Echo and scripted modes.                                                                        |
| `responses_adapter.py`             | Recorded `/v1/responses` payload → contract events.                                             |

## Running the tests

`make test-python` from the repository root, or from here:

```bash
python3 -m unittest discover -p "test_*.py"
```

Stdlib only — no install step, and deliberately so.

## Conforming a new runner

```python
class MyRunnerConformance(RunnerConformanceTests, unittest.TestCase):
    def make_runner(self):
        return MyRunner()
```

The suite speaks plain JSON dicts, so a runner in another language conforms by being wrapped in a
subprocess or HTTP shim rather than rewritten in Python.

## Editing here

- **Changing a schema means changing `contract_version`.** The current version is `v1alpha1`, and a
  runner meeting a version it does not know must refuse rather than guess — so a silent shape change
  is the one failure mode the contract cannot detect.
- **A new schema keyword needs `jsonschema_mini.py` taught first.** It raises rather than ignoring,
  which is what keeps "validated" from meaning "partly validated".
- **A new contract rule is a new test in `conformance.py`, plus a broken runner in
  `test_conformance_null_runner.py::ConformanceSuiteHasTeeth` that the new test catches.** A rule
  nothing can fail is a comment.
