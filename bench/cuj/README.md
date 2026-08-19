# Critical User Journey tests

This directory contains live, black-box functional tests for complete user
journeys through the admin portal API. Each test talks to Kage as a user and
scores only evidence returned by the deployed system.

From the repository root, run every CUJ with pytest:

```bash
CUJ1_AGENT_ID=platform-agent \
CUJ1_PROJECT_ID=test-project \
uv run --project bench pytest -s bench/cuj
```

Pytest reports every journey independently using its normal test discovery.
Each test starts an API-only portal on an OS-assigned loopback port and uses a
unique interaction session, so parallel workers do not share ports or sessions.
CUJ1 appends its request, every portal interaction response, milestones, and
summary to `interactions.jsonl` in a unique `/tmp/kube-agents-cuj1-*`
directory. A failed milestone fails its pytest case but leaves the detailed log
in place. Every milestone reports the CUJ requirement, the proof required to
satisfy it, and the observed evidence.

## Adding a journey

Add a normal pytest module at:

```text
bench/cuj/<area>/test_<NN>_<name>.py
```

Pytest discovers each journey without a registry or custom collector. A test
must:

- run from the repository root without relying on the caller's working
  directory;
- accept live configuration through environment variables;
- act through public interfaces rather than importing production internals;
- write interaction and milestone evidence beneath `/tmp`;
- use ordinary pytest assertions for configuration, transport, and milestone
  failures.

Reuse configuration from `cuj.utils.scenario`, evidence output from
`cuj.utils.evidence`, portal execution and projection helpers from
`cuj.utils.interaction`, portal startup from `cuj.utils.portal`, and
dependency-aware pass/fail/blocked reporting from `cuj.utils.milestones`. Keep
only the prompt, scenario-specific inputs, and milestone checks in the
scenario, then expose it as an ordinary pytest test:

```python
def test_02_example() -> None:
    Scenario("cuj2", build_prompt, evaluate).run_test()
```
