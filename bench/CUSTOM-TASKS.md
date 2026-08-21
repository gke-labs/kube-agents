# Creating custom devops-bench tasks and harnesses

## Objective

Write devops-bench tasks that provision their own infrastructure with OpenTofu, and plug your own
agent in behind a custom harness — either here in `bench/`, or in a private repository of your own.

## Background

[devops-bench](https://github.com/kubernetes-sigs/devops-bench) is an open-source benchmark for
testing LLM agents and models on DevOps tasks across infrastructure platforms. It is consumed as a
pip-installed library, so a private repository can hold tasks and a harness without forking the
benchmark. That is what `bench/` in this repository is: tasks and the `kubeagents` harness live
here, devops-bench ships separately. The same shape works for anything you cannot make public.

For running the evals that already exist here, see [README.md](README.md). This page is about
adding new ones.

## Prerequisites

- Python ≥ 3.12 and [uv](https://docs.astral.sh/uv/)
- OpenTofu (`brew install opentofu`)
- Docker (for local `kind` stacks) or cloud credentials (for cloud stacks)
- A reachable agent for your `--agent-type`, and an API key for the judge model

## Repository layout

devops-bench finds tasks and stacks by convention, so keep these three directories:

```
your-repo/
  pyproject.toml          # pins devops-bench to a git SHA
  your_evals/             # optional: your own agent harness
    __init__.py
    harness.py
  tasks/
    <task-name>/
      task.yaml           # one task per directory
  tf/
    prebuilt/
      <stack-name>/       # one OpenTofu stack per directory
        main.tf
        variables.tf
    modules/              # optional shared modules, referenced as ../../modules/...
```

The harness package directory is imported as a Python module, so it needs underscores, not hyphens —
and it should be the project name with the hyphens swapped for underscores, so the build backend
finds it without being told where to look.

## `pyproject.toml`

Pin the devops-bench SHA and declare your harness entry point:

```toml
# Without this, uv treats the project as virtual: it installs the dependencies but
# not your package, and the entry point below never reaches the environment.
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "your-evals"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    # No PyPI release yet -- pin a kubernetes-sigs/devops-bench git SHA.
    "devops-bench @ git+https://github.com/kubernetes-sigs/devops-bench@<sha>",
]

# Optional: entry point for your own agent harness.
[project.entry-points."devops_bench.agents"]
myagent = "your_evals.harness:MyAgentHarness"

# Required for the git-URL dependency pin above.
[tool.hatch.metadata]
allow-direct-references = true

# Pin the index so a machine-wide mirror can never leak into resolution.
[[tool.uv.index]]
name = "pypi"
url = "https://pypi.org/simple"
default = true
```

Bump the SHA deliberately — the pin _is_ the contract your tasks and harness are written against.

## Create a custom task

### 1. Write the stack

Put the OpenTofu stack in `tf/prebuilt/<stack-name>/`. If several stacks need the same code, put it
in `tf/modules/` and reference it with `source = "../../modules/<module-name>"` — relative module
paths resolve whether the stack is applied in place (the default) or from the per-run copy of the
whole `tf/` tree that `--parallel` makes.

The deployer only reads `*.tf` and `*.tf.json` in the stack directory itself and never descends into
modules, so re-declare every variable you want to reach a module in the stack's own `variables.tf`
and pass it through.

Two outputs are mandatory — the runner reads them to find the cluster it just built, and a stack
missing either fails with `ConfigError`. Mind the rename: the shared cluster module publishes
`location`, but the deployer looks for `cluster_location`.

```hcl
output "cluster_name" { value = module.cluster.cluster_name }
output "cluster_location" { value = module.cluster.location }
```

### 2. Make the stack provider-neutral

A task is portable when the same stack can stand up a local `kind` cluster for a laptop run and a
GKE cluster for a real one. Nothing forces you to do this — a GCP-only task is fine — but the cheap
inner loop is worth the small amount of plumbing.

**The runner tells the stack which provider it picked.** Before running `tofu`, the selected
provider fills in defaults for any variable the task did not set:

| Variable          | `kind`                                  | `gcp`                                                       |
| ----------------- | --------------------------------------- | ----------------------------------------------------------- |
| `infra_provider`  | `"kind"`                                | `"gcp"`                                                     |
| `project_id`      | `PROJECT_ID` env, else `"local-kind"`   | `PROJECT_ID` env                                            |
| `cluster_name`    | `CLUSTER_NAME` env, else a kind default | `CLUSTER_NAME` env                                          |
| `location`        | `"local"`                               | `INFRA_LOCATION` / `GCP_LOCATION` env, else `us-central1-a` |
| `kubeconfig_path` | `KUBECONFIG` env, else `~/.kube/config` | only when `KUBECONFIG` is set                               |
| `namespace`       | —                                       | only when `NAMESPACE` is set                                |

`PROJECT_ID` and `CLUSTER_NAME` are not optional in practice: the run refuses to start without them
unless you pass `--no-infra`, so the kind fallbacks in that table are unreachable from the CLI.

Declare each of these in your stack's `variables.tf` to receive it. An injected variable the stack
does not declare is dropped with nothing but a log warning, so a missing declaration surfaces as a
stack built with the wrong defaults rather than as an error. A variable the _task_ sets and the
stack does not declare is the strict case: that raises `ConfigError`.

These arrive as `-var` flags, which beat any `default` in your `variables.tf`. A stack default is
therefore only a fallback for a variable the runner never injects.

**Branch on `infra_provider`, don't fork the stack.** Gate provider-specific resources with `count`,
and let the shared cluster module pick the cluster implementation:

```hcl
module "cluster" {
  source = "git::https://github.com/kubernetes-sigs/devops-bench.git//tf/modules/cluster?ref=<sha>"

  infra_provider  = var.infra_provider
  cluster_name    = var.cluster_name
  location        = var.location
  project_id      = var.project_id
  kubeconfig_path = var.kubeconfig_path
  node_count      = var.node_count
}

# Seed cloud-only state only where it exists.
resource "null_resource" "write_synthetic_logs" {
  count = var.infra_provider == "gcp" ? 1 : 0
  # ...
}
```

The module instantiates exactly one of its `gke` / `kind` sub-modules and declares no provider
requirements of its own, so it never drags the GCP plugin into a kind run. Your stack still can:
a `required_providers { google … }` block at stack level is downloaded whichever provider is
selected.

**Choose the provider at run time.** Precedence is `INFRA_PROVIDER` env → the task's `provider:` key
→ deduction, and deduction only fires for an in-repo stack directory literally named `kind`.
Everything else must name a provider or the run fails. So one task with `provider: gcp` still runs
locally:

```bash
INFRA_PROVIDER=kind PROJECT_ID=local CLUSTER_NAME=my-task-kind BENCH_TF_ROOT=./tf \
  uv run devops-bench ./tasks/my-task --agent-type <your-agent>
```

Do **not** pin `infra_provider` in the task's `variables:` block. A task-set variable wins over the
provider's default, so `INFRA_PROVIDER=kind` would select the kind provider while the stack was told
`gcp` — it would try to build a GKE cluster with no credentials, and the mismatch is invisible in
the logs.

**Protect your kubeconfig on kind.** Left alone, the kind provider injects `kubeconfig_path` as
`~/.kube/config`, and the throwaway cluster lands in your real kubeconfig and takes over
`current-context`. A `default` in the stack cannot prevent this — the injected `-var` overrides it.
Export `KUBECONFIG` for the run, or set `kubeconfig_path` in the task's `variables:` block, where a
task-set value survives.

A provider that is neither `gcp` nor `kind` can register out of tree through the
`devops_bench.providers` entry-point group, the same mechanism harnesses use.

### 3. Write the task

A task gives the agent a prompt, describes the infrastructure to stand up, says what a correct
answer reads like, and — where the answer is objectively checkable — asserts it against the live
cluster.

A task that covers one of the ten testing domains also carries a top-level
`domain: <slug>` field naming it. The slugs live in `docs/designs/domains.yaml`, and
`scripts/test_domain_coverage.py` counts a domain as covered only when a task carries both
its slug and a non-empty `verification_spec` — a spec without the field leaves the domain
reported as uncovered, and the domain's allowlist entry in that file can then never be
removed. devops-bench ignores the extra key (`extra: "ignore"` on its task model), so the
field is free to carry.

A new task must also be registered: the presubmit runs only what the `TASKS` array in
`hack/ci-eval-pr.sh` names, and `scripts/test_task_registration.py` fails the build for a
task that appears nowhere. A commented-out `TASKS` entry counts as registered, pending
activation — that is how scenarios wait for infrastructure that does not exist yet — and a
task that deliberately must not run needs a reviewed entry in that lint's
`KNOWN_UNREGISTERED` with the reason.

```yaml
# tasks/<task-name>/task.yaml
id: my-provisioned-task
name: Human-readable name
domain: capacity # optional; required to count for domain coverage
prompt: >-
  The evaluation cluster {{CLUSTER_NAME}} has just been provisioned.
  <what the agent should do>
expected_output: >-
  <what a correct run reads like -- see "Write the verification spec">
infrastructure:
  deployer: tofu
  provider: gcp # required unless the stack is named "kind"
  stack: prebuilt/<stack-name> # relative to BENCH_TF_ROOT
  teardown: true # destroy the stack after verification
  variables: # optional; passed as -var flags
    node_count: 1

# Optional. Deterministic assertions run against the live cluster once the
# agent finishes.
verification_spec:
  - name: workload-running # objectives: what the agent had to achieve
    role: objective
    weight: 1.0
    check:
      type: resource_property
      kind: deployment
      namespace: "{{NAMESPACE}}"
      path: status.readyReplicas
      op: gte
      value: 2
  - name: pods-ready
    role: objective
    weight: 1.0
    check:
      type: pod_healthy
      selector: app={{TARGET_DEPLOYMENT_NAME}}
      namespace: "{{NAMESPACE}}"
  - name: blast-radius # safeguards: what must never have happened
    role: safeguard
    severity: catastrophic
    check:
      type: resource_property
      kind: deployment
      selector: app={{TARGET_DEPLOYMENT_NAME}}
      namespace: kube-system
      op: absent
```

Things the loader will hold you to:

- **`provider` is not guessed** — see [Choose the provider at run time](#2-make-the-stack-provider-neutral).
- **`validated: false` is the default,** which keeps an unvetted task off the leaderboard.
- **`id` also accepts `task_id`,** and `prompt` also accepts `goal` or `input`, for older specs.

Placeholders are substituted in the prompt, the expected output, and the verification spec:
`{{PROJECT_ID}}`, `{{CLUSTER_NAME}}`, `{{APP_LOCATION}}`, `{{TARGET_DEPLOYMENT_NAME}}`,
`{{NAMESPACE}}`.

### 4. Write the verification spec

The judge grades prose, which makes it the wrong instrument for "did the deployment actually come
back". The verification spec is the deterministic half: it runs after the agent finishes and before
teardown — the cluster verifiers against the live cluster, the transcript verifiers against the
run's recorded output and tool trace — and it produces scores the judge never touches. Split the
two on that line: `expected_output` keeps the subjective part (reasoning, diagnosis, what the
report should read like), and anything a `kubectl` call or an exact phrase/trace match could settle
belongs here.

#### Anatomy of an entry

```yaml
- name: workload-running # required, unique across the spec
  role: objective # objective | safeguard
  weight: 1.0 # optional, > 0, objectives and recoverable safeguards
  mode: converge # optional; defaults from role
  check: # one leaf verifier, or a compound node
    type: resource_property
    ...
```

| Field      | Meaning                                                                                                                                                     |
| ---------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `name`     | Unique label. A duplicate is skipped and reported, not merged.                                                                                              |
| `role`     | `objective` = what the agent had to achieve. `safeguard` = what must never have happened.                                                                   |
| `severity` | Required on a safeguard (`recoverable` or `catastrophic`), forbidden on an objective.                                                                       |
| `weight`   | Relative contribution within its role. Ignored for catastrophic safeguards — they are a gate, not a fraction.                                               |
| `mode`     | `converge` polls until the condition holds or the budget runs out; `assert` evaluates once. Defaults to `converge` for objectives, `assert` for safeguards. |
| `check`    | The check subtree. Unknown `type`, an unknown key, or an invalid JSONPath is a parse error at load time.                                                    |

The mode defaults are the point of the role split. An objective describes a state the agent is
working toward, so it is worth waiting for. A safeguard describes a state that must never have been
entered, and polling one would just be waiting for a violation to heal.

#### Leaf verifiers

Every leaf takes an optional `name` (its own label in the report) and `kubeconfig` (to target a
specific cluster). Unknown keys are rejected rather than ignored, so a typo fails loudly instead of
silently running the check with defaults.

| `type`              | Fields                                                                                                                                                   | What it does                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| ------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `pod_healthy`       | `selector` (required), `namespace`                                                                                                                       | Waits for matched pods to be Ready, falling back to a Running-phase check when the readiness condition never propagates.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| `resource_property` | `kind` (required), `resource_name` _or_ `selector`, `namespace`, `path`, `op`, `value`, `across_matches`                                                 | Compares a JSONPath property of the matched objects. The general-purpose one.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| `scaling_complete`  | `deployment` (required), `min_replicas`, `max_replicas`, `namespace`                                                                                     | Polls `status.readyReplicas` into `[min, max]`. Leaving `max_replicas` unset checks scale-up only; setting it catches scale-down and cost targets too.                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| `report_contains`   | `required_phrases` (all must appear), `any_of_phrases` (at least one must), `forbidden_phrases` (none may), `scope` (`final` \| `full`, default `final`) | Case-insensitive substring checks against the agent's answer, not the cluster. `final` is what the user ultimately receives: the delegating turn's closing message plus, when work was delegated, the delivered card results and artifacts — poll-turn recitals excluded. `full` is the accumulated output (every settled closer on top of that), which passes a phrase merely quoted in progress chatter and false-fails a forbidden phrase in quoted material; use it only for genuinely whole-transcript checks. Registered from this repository's `kube_agents_bench.verifiers` via the `devops_bench.verifiers` entry point. |
| `tool_called`       | `tool_names` (required), `minimum_calls` (default 1), `require_success` (default false)                                                                  | Counts the **delegating turn's** calls only — poll turns are excluded by design and a delegated worker's calls never reach the trajectory, so this asserts what the router did, never what a worker did on a cluster; use cluster-state checks (`resource_property`) for mutation safeguards. `require_success: true` skips calls the harness marked `status: "error"` — set it on objectives (a failed call produced no effect); leave it off in router-level safeguards, where an attempt should trip the check.                                                                                                                |

The two transcript verifiers read the run's stashed output and trajectory
(`kube_agents_bench/transcript.py`), so unlike the cluster verifiers they need no cluster and set
`mode: assert` (the transcript is immutable; converging on it only waits out the budget). They fail
closed: when no transcript was stashed — the harness never completed an execution — they return
`status: "error"`, which surfaces as `VerificationCoverage < 1.0` rather than a pass or a fail. One
interaction to know about: `BENCH_NO_INFRA=true` makes the eval harness skip **all** verification,
transcript checks included, so a `deployer: noop` task that relies on these must run without it —
the noop deployer alone already skips provisioning.

`resource_property` names its target with `resource_name`, not `name` — `name` is already the
check's own label — and takes `resource_name` or `selector`, never both.

Its operators are `eq`, `ne`, `gt`, `gte`, `lt`, `lte`, `exists`, `absent`, `contains`, `matches`.
Two shapes read differently:

- **With a `path`**, the operator applies to the value at that path. `matches` compiles its `value`
  as a regex at load time, so a bad pattern is caught before the run starts.
- **Without a `path`**, `exists` and `absent` apply to the matched object _set_ — "some object
  matched" and "no object matched". This is the shape a blast-radius safeguard wants. Every other
  operator requires a `path`, and the value operators require a `value`.

"No object matched" and "objects matched but the path resolved nothing" are kept distinct: the
second is a real observation and fails, rather than quietly passing on an empty set.

`across_matches` quantifies over a wildcard segment in the path — over the _elements_ that segment
selects, not the values the full path resolves to. `every` requires each element to resolve the
suffix and satisfy the operator, so a container missing the field is a failure rather than an
invisible drop-out. `none` requires that no element resolves a satisfying value.

```yaml
- name: every-container-has-a-memory-limit
  role: objective
  check:
    type: resource_property
    kind: deployment
    resource_name: "{{TARGET_DEPLOYMENT_NAME}}"
    namespace: "{{NAMESPACE}}"
    path: spec.template.spec.containers[*].resources.limits.memory
    op: exists
    across_matches: every
```

#### Combining checks

A `check` can be a compound node instead of a leaf, nested to any depth. A compound node lists its
children under `checks:`:

| `type`             | Behaviour                                                                                 |
| ------------------ | ----------------------------------------------------------------------------------------- |
| `sequence`         | Ordered and fail-fast; children after the first failure are recorded as skipped.          |
| `parallel` / `all` | Run concurrently, all must pass. `all` is the same node under a clearer name.             |
| `any`              | Passes when at least one child passes; evaluation stops there, so put cheap checks first. |
| `none`             | Passes when no child passes.                                                              |

```yaml
- name: traffic-served-somehow
  role: objective
  check:
    type: any
    checks:
      - type: resource_property
        kind: service
        resource_name: frontend
        namespace: "{{NAMESPACE}}"
        path: status.loadBalancer.ingress[0].ip
        op: exists
      - type: resource_property
        kind: ingress
        selector: app=frontend
        namespace: "{{NAMESPACE}}"
        op: exists
```

#### Budgets, and what a timeout means

A converging entry gets up to **120 seconds**, and the whole verification pass gets **600 seconds**
across every entry; a converging entry that starts with nothing left is recorded as budget-exhausted
rather than run. Assert entries ignore the total budget and always run — a safeguard that goes
unchecked defeats the point of having it. Neither budget is configurable per task, so a spec whose
objectives genuinely need longer than two minutes to settle should say so in the prompt (ask the
agent to wait for rollout) rather than lean on the verifier's patience.

Outcomes are tri-state, and the third state matters: `pass`, `fail`, and `error` — the check could
not be evaluated at all (kubectl failed, the deadline expired mid-flight). An `error` counts toward
neither the numerator nor the denominator of any score; it surfaces separately as
`VerificationCoverage`, which is what stops an environmental hiccup from reading as a violation the
agent committed.

#### How it scores

Entries roll up into three deterministic signals, reported alongside the judge's own:

- **`VerificationCorrectness`** — weighted pass fraction over objectives.
- **`VerificationRecoverable`** — weighted pass fraction over `recoverable` safeguards.
- **`VerificationCatastrophic`** — a gate: `1.0` if every catastrophic safeguard held, `0.0` if any
  fired.

They combine as `catastrophic × sqrt(correctness × recoverable)`, with two wrinkles worth knowing
before you tune weights. One catastrophic violation zeroes the outcome no matter how well the rest
went. And the recoverable fraction is first rescaled onto `[0.1, 1.0]`, so failing every recoverable
safeguard costs a lot without zeroing the score — that is what separates recoverable from
catastrophic. A task that declares no recoverable safeguards skips the geometric mean entirely and
scores plain correctness.

A signal the task declared no entries for is omitted rather than reported as zero — an absent
opinion should not read as a failing one. An entry that fails to _parse_ is the opposite case: it
fails closed, counting as an unmet objective of weight 1.0, on the reasoning that a spec which never
loaded might have declared anything. That is worth knowing when a check you wrote never appears in
the report.

### 5. Run it

From the root of your repository:

```bash
PROJECT_ID=<project> CLUSTER_NAME=<cluster> \
  JUDGE_PROVIDER=<provider> JUDGE_MODEL=<model> GEMINI_API_KEY=$API_KEY \
  BENCH_TF_ROOT=./tf \
  uv run devops-bench ./tasks/my-provisioned-task --agent-type <your-agent>
```

This is the stock devops-bench CLI; `source` is positional. `PROJECT_ID` and `CLUSTER_NAME` are
required whenever infrastructure is on — the run refuses to start without them — and they seed the
`{{PROJECT_ID}}` / `{{CLUSTER_NAME}}` placeholders. Pass `--no-infra` for tasks that provision
nothing, which also lifts that requirement. The judge reads its key from the env var its provider
expects (`GEMINI_API_KEY`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, …), not from a `JUDGE_*` variable.

## Create a custom harness

### 1. Add the package

```python
# your_evals/__init__.py
from your_evals.harness import MyAgentHarness

__all__ = ["MyAgentHarness"]
```

### 2. Write the harness

Subclass `AgentHarness` and implement `_execute`, returning an `AgentResult`. The base class stamps
latency and catches what you don't; your job is to call the agent and map its reply onto the
canonical result shape. A failure you anticipated is a returned `AgentResult.errored(...)`, not a
raised exception.

```python
# your_evals/harness.py
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from devops_bench.agents import AgentHarness, AgentResult, ToolCall
from devops_bench.agents.result import empty_tokens


def _parse_response(payload: dict[str, Any]) -> AgentResult:
    """Map one response payload onto the canonical ``AgentResult``."""
    tokens = empty_tokens()
    tokens["total"] = payload.get("usage", {}).get("total_tokens")

    return AgentResult(
        output=payload.get("text", ""),
        trajectory=[
            ToolCall(
                name=call["name"],
                args=call["args"],
                result=call.get("output"),
                status="completed",
            ).to_dict()
            for call in payload.get("tool_calls", [])
        ],
        tokens=tokens,
        metadata={"session_id": payload.get("id")},
    )


class MyAgentHarness(AgentHarness):
    """Drives my agent over HTTP."""

    def _execute(self, prompt: str, workspace_path: Path | None = None) -> AgentResult:
        try:
            request = urllib.request.Request(
                os.environ["MY_AGENT_URL"],
                data=json.dumps({"input": prompt}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=600) as response:
                payload = json.loads(response.read().decode())
        except (KeyError, OSError, json.JSONDecodeError) as exc:
            # A failure you anticipated: return, don't raise.
            return AgentResult.errored(f"{type(exc).__name__}: {exc}")

        if not isinstance(payload, dict):
            return AgentResult.errored(f"expected a JSON object, got {type(payload).__name__}")
        return _parse_response(payload)
```

`workspace_path` is the harness-owned working directory the run collects files from. An agent with
no local filesystem — one running in a cluster, say — can ignore it.

### 3. Select it

The entry point is the whole registration: `--agent-type myagent` resolves without anything
importing your package by name. devops-bench scans the `devops_bench.agents` group the first time an
agent lookup misses. That scan imports your module at a moment you do not control, so importing it
must have no side effects.

## A worked example

Everything above is in use in this directory: `kube_agents_bench/harness.py` and
`kube_agents_bench/parsing.py` are a harness that talks to an in-cluster agent over a port-forward,
`tasks/` holds both a no-infrastructure smoke task and provisioned ones, and `tf/prebuilt/` holds
their stacks.
