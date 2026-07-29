# bench

Evaluation harness that runs [kubernetes-sigs/devops-bench](https://github.com/kubernetes-sigs/devops-bench) against the Platform Agent, with devops-bench consumed as a pip-installed library (pinned git SHA — no PyPI release yet) instead of the legacy evaluator baked into the eval image. Tasks and the agent transport live here, so kube-agents and devops-bench ship independently.

## Layout

- `kube_agents_bench/harness.py` — the `kubeagents` agent harness: establishes `kubectl port-forward` to `svc/platform-agent` when the local port is closed, POSTs the task prompt to `/v1/responses`, and parses the response into devops-bench's canonical `AgentResult`. Environment variables are documented in the module docstring.
- `kube_agents_bench/driver.py` — `kube-agents-bench`, a CLI that registers the harness and delegates to the stock `devops-bench` CLI.
- `tests/` — offline tests against a local HTTP stub.

## Running evals

```bash
cd bench
uv sync
uv run kube-agents-bench --source ./tasks --agent-type kubeagents
```

The harness reads `PLATFORM_AGENT_TOKEN` (the `API_SERVER_KEY` from `platform-agent-secrets`, as `hack/ci-eval-pr.sh` already exports it) and honours the same `AGENT_*` variables as the legacy runner.

## Registration

The harness is declared under the `devops_bench.agents` entry-point group and also self-registers on import. The entry point activates once the pinned devops-bench SHA includes agent entry-point discovery; until then `kube-agents-bench` provides registration. Registration is idempotent, so bumping the pin requires no change here.

## Tests

```bash
cd bench
uv sync
uv run pytest tests
```

No cluster or `kubectl` required — the suite drives the full request → parse → `AgentResult` path against a local stub.
