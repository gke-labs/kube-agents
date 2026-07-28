# kube-agents-bench

devops-bench agent harness for the kube-agents platform agent. This package
replaces the legacy evaluator baked into the eval container image
(`/app/pkg/evaluator/evaluate.py`, `AGENT_TARGET=kubeagents`):
[kubernetes-sigs/devops-bench](https://github.com/kubernetes-sigs/devops-bench)
is consumed as a pip-installed **library** (pinned git SHA — no PyPI release
yet), the agent transport lives here, and tasks are authored in this repo. The
two projects ship independently.

Everything is contained in `bench/` — nothing outside this directory is
touched.

## What the harness does

`KubeAgentsHarness` is a pure HTTP transport (no model SDK):

1. ensures the agent service is reachable on `AGENT_LOCAL_PORT`, lazily
   spawning a background `kubectl port-forward svc/$AGENT_SERVICE_NAME` when
   the port is closed;
2. POSTs the task prompt to the Responses-style endpoint (`AGENT_API_PATH`,
   default `/v1/responses`) with the `PLATFORM_AGENT_TOKEN` bearer token;
3. parses the response into devops-bench's canonical `AgentResult` — final
   assistant text, `function_call`/`function_call_output` items as the
   canonical tool-call trajectory, and token usage.

Known failures (HTTP errors, unreachable endpoint, bad JSON) surface as
`AgentResult.errors`, so one agent fault never aborts a benchmark run.

Environment variables are documented in `kube_agents_bench/harness.py`; they
match the legacy runner (`AGENT_SERVICE_NAME`, `AGENT_NAMESPACE`,
`AGENT_CLUSTER_CONTEXT`, `AGENT_LOCAL_PORT`, …).

## Running evals

```bash
cd bench
uv sync                        # installs devops-bench at the pinned SHA
uv run kube-agents-bench --source ./tasks --agent-type kubeagents
```

`kube-agents-bench` is a thin driver that imports the harness (registering
`kubeagents`) and then delegates verbatim to the stock `devops-bench` CLI.

### Two registration mechanisms, one active

- **Entry point (forward-looking).** `pyproject.toml` declares the harness
  under the `devops_bench.agents` entry-point group. Once the pinned
  devops-bench SHA includes agent entry-point discovery, the stock
  `devops-bench` CLI resolves `--agent-type kubeagents` with no driver.
- **Driver import (works today).** Until then, `kube-agents-bench` (or any
  script that imports `kube_agents_bench.harness` before `run_benchmark()`)
  registers the harness explicitly.

Registration is idempotent across both mechanisms, so upgrading the pin
requires no code change here — the driver simply becomes an alias.

## Infrastructure stacks

The devops-bench wheel does not ship `tf/` stacks. Once the pinned SHA
includes the `BENCH_TF_ROOT` override, point it at this repo's stack
directory so relative `stack:` names in `task.yaml` resolve (and keep per-run
isolation for parallel evals). Until then, use absolute `stack:` paths in
`task.yaml` — and avoid concurrent runs sharing one stack directory (absolute
stacks skip per-run isolation).

## Tests

```bash
cd bench
uv sync
uv run pytest tests
```

The tests drive the full request → parse → `AgentResult` path against a local
HTTP stub standing in for the platform agent; no cluster or `kubectl` needed.
