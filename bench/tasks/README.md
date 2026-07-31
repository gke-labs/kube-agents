# Writing tasks

Tasks are plain directories consumed by the stock `devops-bench` CLI — no code
in this package needs to change to add one. The `kubeagents` harness resolves
through the `devops_bench.agents` entry point, so a new task is just a new
folder here:

```
tasks/
  my-new-task/
    task.yaml
```

Run one task or the whole tree the same way:

```bash
devops-bench ./tasks --no-infra --agent-type kubeagents          # everything
devops-bench ./tasks/my-new-task --no-infra --agent-type kubeagents
```

## task.yaml

```yaml
id: my-new-task            # unique slug; conventionally matches the folder name
name: Human-readable name
prompt: >-
  What the platform agent is asked to do. This is sent verbatim to
  POST /v1/responses.
expected_output: >-
  What a correct run looks like, in prose. The LLM judge compares the agent's
  behaviour against this text.
infrastructure:
  deployer: noop           # no cluster; or name an OpenTofu stack (see below)
validated: false
```

### Writing the prompt: stay inside the agent's real tool surface

The deployed platform agent denylists `terminal`, `file`, `skills`, and every
other execution toolset — in the image default (`/opt/defaults/config.yaml`)
and in the operator-rendered production config alike. What it actually
exposes is the kanban toolset (`kanban_create`, `kanban_list`, …), the MCP
router surface (`mcp__router__*`), and memory.

A task that asks the agent to run a shell command scores 0 against any real
deployment — the agent will correctly answer that it has no terminal tool.
Probe capabilities the agent ships with, or capabilities your task's
infrastructure grants it, never capabilities you wish it had.

### Writing expected_output: describe action and report

Two metrics are scored on every run, against the same `expected_output`:

- **OutcomeValidity** judges the agent's final text. This is the score CI
  gates on (`hack/ci-eval-pr.sh`, threshold ≥ 0.7).
- **ToolInvocation** judges the tool-call trajectory.

So describe both halves — what the agent should *do* (which tool activity
should appear in the trajectory) and what it should *say* (what the final
message must state). See `agent-kanban-smoke/task.yaml` for the shape.

Keep `expected_output` about observable behaviour. Don't enumerate exact tool
names or call counts unless they are genuinely required — agents commonly make
a harmless discovery call (e.g. `mcp__router__list_agents`) before acting, and
over-specified expectations turn that into a false failure.

## Tasks that provision infrastructure

Full from-scratch instructions — repository layout, stack conventions, the
provider-injected variables, run modes, and troubleshooting — live in
[`../docs/tf-task-setup.md`](../docs/tf-task-setup.md). Summary:

Replace `deployer: noop` with a stack reference and keep the stack in this
repo under `bench/tf/`, resolved via `BENCH_TF_ROOT` so the eval never depends
on stacks bundled with the devops-bench library.
`cluster-provision-kanban` is the reference example: its `stack: prebuilt/kind`
resolves to `tf/prebuilt/kind/`, which provisions a local kind cluster. Run it
with (requires OpenTofu, Docker, and a reachable agent):

```bash
BENCH_TF_ROOT=./tf devops-bench ./tasks/cluster-provision-kanban \
  --agent-type kubeagents --project local --cluster db-eval-smoke
```

What happens, in order: the stack is applied, `{{GKE_CLUSTER_NAME}}` /
`{{GCP_PROJECT_ID}}` are substituted from `--cluster` / `--project`, the
agent runs, the result is judged, and — because the task sets
`teardown: true` — the stack is destroyed. The authoring rules (stack
variables, provider selection, run modes and isolation) are in the guide
above.

## Environment the harness reads

The transport is configured entirely by environment variables (documented in
`kube_agents_bench/harness.py`): `PLATFORM_AGENT_TOKEN` is required (the
`API_SERVER_KEY` from `platform-agent-secrets`); `AGENT_LOCAL_PORT` when the
agent is already reachable locally, otherwise the harness spawns
`kubectl port-forward svc/platform-agent -n kubeagents-system` itself. The
judge needs `GEMINI_API_KEY` (or `JUDGE_PROVIDER`/`JUDGE_MODEL` overrides).

## Checklist before committing a task

1. `uv run devops-bench ./tasks/<your-task> --no-infra --agent-type kubeagents`
   passes locally against a real agent (not a stub).
2. The prompt uses only toolsets the deployed agent exposes.
3. `expected_output` covers both the tool activity and the final message.
4. Run it at least twice — judge scores near a threshold are a task-design
   smell even when they pass.
