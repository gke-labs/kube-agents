# Setting up devops-bench tasks with TF provisioning in your own repository

How to author evaluation tasks — including ones that provision their own
infrastructure with OpenTofu — in a repository that consumes
[kubernetes-sigs/devops-bench](https://github.com/kubernetes-sigs/devops-bench)
as a pip-installed library. The layout and semantics deliberately mirror how
tasks are set up inside the devops-bench repo itself: same `tasks/` + `tf/`
split, same stack conventions, same deployer code paths. The only structural
difference is that a pip install has no bundled `tf/` tree, so the stack root
comes from the `BENCH_TF_ROOT` environment variable instead of the checkout
location.

This repo's `bench/` directory is the worked example; `tasks/cluster-provision-kanban`
plus `tf/prebuilt/kind` form a complete, verified provision → evaluate → destroy
cycle you can copy from.

## Prerequisites

- Python ≥ 3.12 and [uv](https://docs.astral.sh/uv/)
- [OpenTofu](https://opentofu.org/) (`brew install opentofu`)
- Docker (for local `kind` stacks) or cloud credentials (for `gcp` stacks)
- A reachable agent for your `--agent-type`, and a judge key (`GEMINI_API_KEY`)

## 1. Repository layout

```
your-repo/
  pyproject.toml            # pins devops-bench to a git SHA
  tasks/
    <task-name>/
      task.yaml             # one task per directory
  tf/
    prebuilt/
      <stack-name>/         # one OpenTofu stack per directory
        main.tf
        variables.tf
    modules/                # optional shared modules, referenced ../../modules/...
```

This is the devops-bench repo's own convention (`tasks/`, `tf/prebuilt/`,
`tf/modules/`). Nothing enforces the `prebuilt/` segment — `stack:` is just a
path relative to the stack root — but keeping it makes stacks portable between
repos.

## 2. Pin devops-bench in `pyproject.toml`

```toml
[project]
name = "your-evals"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    # No PyPI release yet -- pin a kubernetes-sigs/devops-bench git SHA.
    # Must be cdf2c206... or newer (agent entry-point discovery + BENCH_TF_ROOT).
    "devops-bench @ git+https://github.com/kubernetes-sigs/devops-bench@cdf2c20692f7dcf69bf1f5c4b21ce047161458c8",
]

# Required for the git-URL dependency pin above.
[tool.hatch.metadata]
allow-direct-references = true

# Pin the index so a machine-wide mirror can never leak into resolution.
[[tool.uv.index]]
name = "pypi"
url = "https://pypi.org/simple"
default = true
```

Then `uv sync`. If your repo also ships its own agent harness, declare it under
`[project.entry-points."devops_bench.agents"]` (see this repo's
`bench/pyproject.toml`); if you evaluate one of the built-in agents
(`gemini`, `api`, ...), you need no harness at all.

> Do not run with `uv run --no-sync` after changing the pin — `--no-sync`
> freezes whatever devops-bench is already in the venv, which is how stale
> installs happen. Its only legitimate use is protecting a deliberate
> `uv pip install` override of the pin.

## 3. Write the stack

`tf/prebuilt/<stack-name>/` is ordinary OpenTofu. Two conventions matter:

**Declare the variables the provider layer injects.** devops-bench passes
variables as `-var` flags, filtered to what the stack declares. The provider
fills these automatically when the stack doesn't get them from the task:

| variable | filled from |
|---|---|
| `cluster_name` | `CLUSTER_NAME` env / `--cluster` flag |
| `project_id` | `PROJECT_ID` env / `--project` flag |
| `location` | provider default (`local` for kind, zone for gcp) |
| `kubeconfig_path` | `$KUBECONFIG` or `~/.kube/config` |
| `infra_provider` | the selected provider name |

Declare the ones your stack uses; undeclared injected variables are dropped
rather than erroring.

**Reference shared code relatively.** Stacks may use
`source = "../../modules/<name>"` — the whole `tf/` tree travels together, so
these references survive per-run isolation (see §6).

Minimal local-cluster example (this repo's `tf/prebuilt/kind/main.tf`, seeded
from the devops-bench repo's own stack):

```hcl
terraform {
  required_version = ">= 1.5.0"
  required_providers {
    kind = { source = "tehcyx/kind", version = ">= 0.5.0" }
  }
}

provider "kind" {}

resource "kind_cluster" "default" {
  name            = var.cluster_name
  wait_for_ready  = true
  kubeconfig_path = pathexpand(var.kubeconfig_path)
}

output "cluster_name" { value = kind_cluster.default.name }
```

## 4. Write the task

`tasks/<task-name>/task.yaml`:

```yaml
id: my-provisioned-task
name: Human-readable name
prompt: >-
  The evaluation cluster {{GKE_CLUSTER_NAME}} has just been provisioned.
  <what the agent should do>
expected_output: >-
  <what a correct run looks like: both the tool activity that should appear
  in the trajectory and what the final message must state>
infrastructure:
  deployer: tofu
  stack: prebuilt/<stack-name>   # relative to BENCH_TF_ROOT
  teardown: true                 # destroy the stack after scoring
  variables:                     # optional; passed as -var flags
    node_count: 1
validated: false
```

Details that matter:

- **Placeholders.** `{{GKE_CLUSTER_NAME}}` / `{{CLUSTER_NAME}}`,
  `{{GCP_PROJECT_ID}}` / `{{PROJECT_ID}}`, `{{TARGET_DEPLOYMENT_NAME}}`, and
  `{{NAMESPACE}}` are substituted in both `prompt` and `expected_output` from
  the run's cluster/project settings.
- **Provider selection.** A relative stack directory named `kind` auto-selects
  the kind provider. Anything else must set `provider: gcp` (or the
  `INFRA_PROVIDER` env var, which wins over the task key).
- **Stay inside the agent's real tool surface.** A task that asks for
  capabilities the deployed agent doesn't ship (e.g. a shell) scores 0 against
  every real deployment. See `tasks/README.md`.

## 5. Run

```bash
PROJECT_ID=local \
CLUSTER_NAME=db-eval-smoke \
PLATFORM_AGENT_TOKEN=$PLATFORM_AGENT_TOKEN \
JUDGE_PROVIDER=gemini JUDGE_MODEL=gemini-flash-latest GEMINI_API_KEY=$GEMINI_KEY \
BENCH_TF_ROOT=./tf \
uv run devops-bench ./tasks/my-provisioned-task --agent-type <your-agent>
```

What happens, in order: resolve the stack under `BENCH_TF_ROOT` → `tofu init`
and `apply` → substitute placeholders → run the agent against the prompt →
judge (`OutcomeValidity` on the final text, `ToolInvocation` on the
trajectory) → `tofu destroy` (because `teardown: true`). Results land in
`results/run_*/results.json` (override the root with `RESULTS_ROOT`).

Notes:

- `PROJECT_ID` and `CLUSTER_NAME` are **required in infra mode**, even for
  local kind stacks (`PROJECT_ID=local` is fine there). Flags `--project` /
  `--cluster` are interchangeable with the env vars.
- Omit `BENCH_TF_ROOT` and relative stacks fail with
  `TF stack not found under .../site-packages/tf` — under a pip install there
  is no default stack root; the env var *is* the wiring.
- No-infra tasks (`deployer: noop`) need none of the above: just
  `--no-infra` (or `BENCH_NO_INFRA=true`), the agent connection, and the judge
  key.

## 6. Single runs vs `--parallel`: where tofu executes

Identical to the devops-bench repo:

- **Single runs execute tofu in your source stack directory.** `.terraform/`,
  `.terraform.lock.hcl`, and `terraform.tfstate` are written next to your
  `.tf` files. Ignore them (this repo's `.gitignore` covers `.terraform/`,
  `*.tfstate*`, and the lock file — copy those entries; commit none of them).
- **`--parallel` runs are isolated.** Each run gets a private copy of the
  *whole* `tf/` tree in a scratch dir keyed by `TF_DATA_DIR`, so concurrent
  runs of the same stack never contend on lock or state files. This is also
  why the tree must travel whole: module references resolve inside the copy.

Two constraints follow: keep `tf/` lean (every file is copied per isolated
run), and never point `BENCH_TF_ROOT` at an ancestor of the run scratch dir.

One local-machine caution: kind stacks write to `~/.kube/config` by default
(`kubeconfig_path`). On a workstation whose kubeconfig you care about, set
`KUBECONFIG` to an isolated path for eval runs.

## 7. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `'<agent>' is not registered in the 'agents' registry` | venv has a devops-bench predating entry-point discovery (or your entry point isn't installed) | check the pin is ≥ `cdf2c20`, drop `--no-sync`, re-run `uv sync` |
| `TF stack not found in repo: ... (checked .../site-packages/tf/...)` | stale pre-`BENCH_TF_ROOT` devops-bench in the venv — the env var is being ignored | same as above; the fixed build's error names your real stack root instead |
| `TF stack not found under <your root>: <stack>` | library is fine; the stack directory genuinely doesn't exist under `BENCH_TF_ROOT` | create `tf/<stack>/` or fix the task's `stack:` value |
| `PROJECT_ID and CLUSTER_NAME must be set (or pass --no-infra)` | infra-mode gate | set both env vars (or flags); `PROJECT_ID=local` for kind |
| `stack '<name>' requires an explicit provider` | relative stack not named `kind` and no provider given | add `provider:` to the task or set `INFRA_PROVIDER` |
| state/lock files appear under `tf/` | normal for single runs (§6) | keep them gitignored; use `--parallel` for isolation |
