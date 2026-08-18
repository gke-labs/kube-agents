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
CUJ1 writes its request, state transitions, conversation, skill routing,
delegated evidence, individual milestones, and summary to a unique
`/tmp/kube-agents-cuj1-*` directory. A failed milestone fails its pytest case
but leaves all evidence in place. Every milestone reports the CUJ requirement,
the proof required to satisfy it, and the observed evidence.

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
- write stage and milestone evidence beneath `/tmp`;
- use ordinary pytest assertions for configuration, transport, and milestone
  failures.

Reuse portal startup and HTTP behavior from `cuj.portal`, and dependency-aware
pass/fail/blocked reporting from `cuj.milestones`. Keep prompts, polling, and
milestone checks in the scenario.
