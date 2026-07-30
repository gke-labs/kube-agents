# bench

Evaluation harness that runs [kubernetes-sigs/devops-bench](https://github.com/kubernetes-sigs/devops-bench) against the Platform Agent, with devops-bench consumed as a pip-installed library (pinned git SHA — no PyPI release yet) instead of the legacy evaluator baked into the eval image. Tasks and the agent transport live here, so kube-agents and devops-bench ship independently.

> **Blocked on upstream.** Requires devops-bench [#48](https://github.com/kubernetes-sigs/devops-bench/pull/48) (agent entry-point discovery) and [#49](https://github.com/kubernetes-sigs/devops-bench/pull/49) (`BENCH_TF_ROOT`). The pin in `pyproject.toml` must be bumped to their merge commit — and `uv lock` re-run — before this merges.

## Layout

- `kube_agents_bench/harness.py` — the `kubeagents` agent harness: establishes `kubectl port-forward` to `svc/platform-agent` when the local port is closed, POSTs the task prompt to `/v1/responses`, and parses the response into devops-bench's canonical `AgentResult`. Environment variables are documented in the module docstring.
- `tasks/` — task definitions. `agent-kanban-smoke` is a no-infrastructure smoke task that exercises the whole pipeline using only toolsets the deployed agent actually ships with. See `tasks/README.md` for how to write your own.
- `tests/` — offline tests against a local HTTP stub.

## Running evals

```bash
cd bench
uv sync
BENCH_NO_INFRA=true JUDGE_PROVIDER=google JUDGE_MODEL=gemini-flash-latest \
  uv run devops-bench ./tasks --no-infra --agent-type kubeagents
```

This is the stock `devops-bench` CLI — there is no wrapper command. `source` is positional. Drop `--no-infra` for tasks that provision infrastructure, and see `--help` for the rest.

The harness reads `PLATFORM_AGENT_TOKEN` (the `API_SERVER_KEY` from `platform-agent-secrets`, as `hack/ci-eval-pr.sh` already exports it) and honours the same `AGENT_*` variables as the legacy runner.

Tasks that provision infrastructure name their OpenTofu stack relative to `BENCH_TF_ROOT`; point it at a stack directory in this repo so the eval never depends on stacks bundled with the library:

```bash
BENCH_TF_ROOT=./tf uv run devops-bench ./tasks --agent-type kubeagents
```

## Running before the upstream PRs merge

`uv sync` installs the devops-bench SHA pinned in `pyproject.toml`, which predates
agent entry-point discovery — so the stock command fails with
`'kubeagents' is not registered in the 'agents' registry` until upstream
[#48](https://github.com/kubernetes-sigs/devops-bench/pull/48) and
[#49](https://github.com/kubernetes-sigs/devops-bench/pull/49) merge and the pin
is bumped. To try it today, override the pin with a build that includes both PRs
(their refs are publicly fetchable):

```bash
git clone https://github.com/kubernetes-sigs/devops-bench /tmp/devops-bench
git -C /tmp/devops-bench fetch origin pull/48/head:pr48 pull/49/head:pr49
git -C /tmp/devops-bench merge --no-edit pr48 pr49

cd bench
uv sync
uv pip install --reinstall --no-cache /tmp/devops-bench
uv run --no-sync devops-bench ./tasks --no-infra --agent-type kubeagents
```

Two flags are load-bearing: `--no-sync`, because a plain `uv run` re-syncs to
`uv.lock` and silently reinstalls the pinned pre-#48 build; and `--no-cache`,
because both builds carry version `0.1.0` and uv would otherwise serve a stale
cached wheel. Once the upstream PRs merge, bump the pin, re-run `uv lock`, and
this whole section becomes obsolete.

### A local agent to run against

Without cluster access, the real platform agent runs in Docker (the image is
public; on arm64 hosts it runs emulated and boots in ~1 min):

```bash
IMG=ghcr.io/gke-labs/kube-agents/platform-agent:latest

# First boot seeds /opt/data root-owned, then exits on a PermissionError —
# expected. Chown the seeded volume and boot again.
docker run -d --name pa-local --platform linux/amd64 \
  -p 18642:8642 -v pa-data:/opt/data \
  -e API_SERVER_HOST=0.0.0.0 \
  -e API_SERVER_KEY=local-test-token \
  -e PATH=/command:/opt/hermes/bin:/opt/hermes/.venv/bin:/opt/data/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
  "$IMG"
sleep 45
docker run --rm --platform linux/amd64 -v pa-data:/opt/data --entrypoint sh "$IMG" \
  -c 'chown -R 10000:10000 /opt/data'
docker start pa-local
until curl -sf http://127.0.0.1:18642/health >/dev/null; do sleep 5; done

# The image pins the model to an in-cluster litellm Service that does not
# exist off-cluster; repoint it at any OpenAI-compatible endpoint. The
# gateway re-reads config per request — no restart needed.
docker exec -i \
  -e MODEL_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai \
  -e MODEL_API_KEY="$GEMINI_API_KEY" \
  -e MODEL_NAME=gemini-flash-latest \
  pa-local /opt/hermes/.venv/bin/python - <<'PY'
import os, re
p = "/opt/data/config.yaml"
s = open(p).read()
s = re.sub(r"^model:\n(?:  .*\n)+",
    "model:\n"
    f"  api_key: {os.environ['MODEL_API_KEY']}\n"
    f"  base_url: {os.environ['MODEL_BASE_URL']}\n"
    f"  default: {os.environ['MODEL_NAME']}\n"
    f"  model: {os.environ['MODEL_NAME']}\n"
    "  provider: custom\n", s, flags=re.M)
open(p, "w").write(s)
assert "litellm" not in s
print("model repointed")
PY

# Point the harness at it (no kubectl involved):
export AGENT_LOCAL_PORT=18642
export PLATFORM_AGENT_TOKEN=local-test-token
```

`API_SERVER_HOST=0.0.0.0` matters: the server binds `127.0.0.1` by default,
which Docker's port mapping cannot reach — the agent then looks healthy in its
logs while every request from the host gets no response. The toolset config is
deliberately left stock; tasks must pass against what the agent actually ships
with (see `tasks/README.md`).

## Registration

The harness is registered solely by the `devops_bench.agents` entry point declared in `pyproject.toml`. devops-bench scans that group on the first unresolved agent lookup, so `--agent-type kubeagents` resolves without importing this package — nothing in the invocation references `kube_agents_bench` by name. Importing the harness module has no side effects.

## Tests

```bash
cd bench
uv sync
uv run pytest tests
```

No cluster or `kubectl` required — the suite drives the full request → parse → `AgentResult` path against a local stub.
