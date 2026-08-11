# bench

Evaluation harness that runs [kubernetes-sigs/devops-bench](https://github.com/kubernetes-sigs/devops-bench) against the Platform Agent, with devops-bench consumed as a pip-installed library (pinned git SHA — no PyPI release yet) instead of the legacy evaluator baked into the eval image. Tasks and the agent transport live here, so kube-agents and devops-bench ship independently.

## Layout

- `kube_agents_bench/harness.py` — the `kubeagents` agent harness: establishes `kubectl port-forward` to `svc/platform-agent` when the local port is closed, POSTs the task prompt to `/v1/responses`, and waits out any work the agent delegates to a subagent. Environment variables are documented in the module docstring.
- `kube_agents_bench/parsing.py` — pure payload and trajectory reading: maps a response onto devops-bench's canonical `AgentResult`, and reads back which kanban cards a turn filed, what statuses it reported, and what a finished card delivered.
- `tasks/` — task definitions. `agent-kanban-smoke` is a no-infrastructure smoke task that exercises the whole pipeline using only toolsets the deployed agent actually ships with.
- `tests/` — offline tests against a local HTTP stub.

To add a task or plug in a different agent, see
[CUSTOM-TASKS.md](CUSTOM-TASKS.md).

## Running evals

```bash
cd bench
uv sync
PLATFORM_AGENT_TOKEN=$(kubectl get secret platform-agent-secrets -n <namespace> \
  -o jsonpath='{.data.API_SERVER_KEY}' | base64 --decode) \
  JUDGE_PROVIDER=<provider> JUDGE_MODEL=<model> \
  uv run devops-bench ./tasks --no-infra --agent-type kubeagents
```

This is the stock `devops-bench` CLI — there is no wrapper command. `source` is positional. Drop `--no-infra` for tasks that provision infrastructure, and see `--help` for the rest.

`hack/ci-eval-pr.sh` exports `PLATFORM_AGENT_TOKEN` for you in CI. The harness also honours the same `AGENT_*` variables as the legacy runner.

Tasks that provision infrastructure name their OpenTofu stack relative to `BENCH_TF_ROOT`; point it at a stack directory in this repo so the eval never depends on stacks bundled with the library:

```bash
AGENT_CLUSTER_CONTEXT=gke_<project>_<location>_<agent-cluster> \
  PROJECT_ID=<project> CLUSTER_NAME=<task-cluster> \
  BENCH_TF_ROOT=./tf uv run devops-bench ./tasks --agent-type kubeagents
```

`PROJECT_ID` and `CLUSTER_NAME` are required once infrastructure is on; without them the run exits before provisioning. Set `AGENT_CLUSTER_CONTEXT` for these too. Provisioning a task cluster runs `gcloud container clusters get-credentials`, which repoints kubectl's current context at it; without the pin, the harness port-forwards into the task cluster, where the agent does not run.

A stack under `tf/` does not have to vendor the upstream OpenTofu modules — reference them over git, pinned to a SHA:

```hcl
module "cluster" {
  source = "git::https://github.com/kubernetes-sigs/devops-bench.git//tf/modules/cluster?ref=<sha>"
}
```

The deployer scans `*.tf` in the stack directory only and never descends into modules, so re-declare every variable you want to reach the module in the stack's own `variables.tf` and pass it through. A variable a task's `variables:` block sets but the stack does not declare raises `ConfigError`; one the runner injects is dropped with a log warning.

## Registration

The harness is registered solely by the `devops_bench.agents` entry point declared in `pyproject.toml`. devops-bench scans that group on the first unresolved agent lookup, so `--agent-type kubeagents` resolves without importing this package — nothing in the invocation references `kube_agents_bench` by name. Importing the harness module has no side effects.

## Tests

```bash
cd bench
uv sync
uv run pytest tests
```

No cluster or `kubectl` required — the suite drives the full request → parse → `AgentResult` path against a local stub.
