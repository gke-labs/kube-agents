# Critical User Journey tests

This directory contains live, black-box functional tests for complete user
journeys through the admin portal API. Each test talks to Kage as a user and
scores only evidence returned by the deployed system.

From the repository root, run every CUJ with pytest:

```bash
uv run --project bench pytest -s bench/cuj
```

The suite targets the stock `platform-agent` resource created by
`./install.sh`. Set `CUJ_PROJECT_ID` for scenarios that
need a Google Cloud project. `CUJ_PROFILE`, `CUJ_TIMEOUT`, and
`CUJ_POLL_INTERVAL` are optional overrides shared by the whole suite.
Pytest reports every journey independently using its normal test discovery.
Before running a journey, `test_00_agent_responsive.py` requires the configured
agent to be discoverable and complete a minimal `READY` interaction through the
same admin portal path.
Each test starts an API-only portal on an OS-assigned loopback port and uses a
unique interaction session, so parallel workers do not share ports or sessions.
Each journey appends its request, every portal interaction response, acceptance
criteria, milestones, and summary to `interactions.jsonl` in a unique
`/tmp/kube-agents-<scenario>-*` directory. Backend-independent acceptance
criteria determine the pytest result. Backend-specific milestones report
diagnostic progress but do not pass or fail the test. Both report the required
proof and observed evidence.

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
- write interaction, acceptance, and milestone evidence beneath `/tmp`;
- use ordinary pytest assertions for configuration, transport, and acceptance
  failures.

Reuse acceptance reporting from `cuj.utils.acceptance_criteria`, configuration
from `cuj.utils.scenario`, evidence output from `cuj.utils.evidence`, portal
execution and projection helpers from `cuj.utils.interaction`, portal startup
from `cuj.utils.portal`, and dependency-aware diagnostic reporting from
`cuj.utils.milestones`. Keep only the prompt and scenario-specific acceptance
and milestone checks in the scenario, then expose it as an ordinary pytest
test:

```python
def test_02_example() -> None:
    Scenario(
        "cuj2",
        build_prompt,
        evaluate_acceptance,
        evaluate_backend_milestones,
    ).run_test()
```
