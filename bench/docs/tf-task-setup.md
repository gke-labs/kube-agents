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
- A reachable agent for your `--agent-type`, and a judge key (`YOUR_API_KEY`) where devops-bench supports both Gemini and Anthropic models.

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
    # cdf2c206... is the floor for agent entry-point discovery + BENCH_TF_ROOT.
    # 0022cd6b... (#47) is the floor for the verification_spec shape in §4:
    # entries gained role/severity/weight/check and became an unconditional
    # post-run pass. It landed 42 minutes after cdf2c206..., so pin at or
    # after it if you write any verification_spec.
    "devops-bench @ git+https://github.com/kubernetes-sigs/devops-bench@0022cd6b2bc4b508951f0017fd91e35313d8763a",
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

**Point at shared code with relative paths.** If several stacks need the same
code, put it in `tf/modules/` and refer to it as
`source = "../../modules/<name>"`. Relative paths are safe here because
devops-bench copies your entire `tf/` directory — not just the one stack —
whenever it isolates a run, so a stack's neighbours are always where it expects
them (see §6). A path that points outside `tf/` will not survive that copy.

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
  The evaluation cluster {{CLUSTER_NAME}} has just been provisioned.
  <what the agent should do>
expected_output: >-
  <the subjective residue only: what a correct run reads like. Anything a
  kubectl call could settle belongs in verification_spec instead>
infrastructure:
  deployer: tofu
  stack: prebuilt/<stack-name>   # relative to BENCH_TF_ROOT
  teardown: true                 # destroy the stack after scoring
  variables:                     # optional; passed as -var flags
    node_count: 1
# Optional. Deterministic assertions run against the live cluster once the
# agent finishes, whether or not a chaos_spec exists. See below.
verification_spec:
  - name: workload-running          # objectives: what the agent had to achieve
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
  - name: blast-radius              # safeguards: what must never have happened
    role: safeguard
    severity: catastrophic
    check:
      type: resource_property
      kind: deployment
      selector: app={{TARGET_DEPLOYMENT_NAME}}
      namespace: kube-system
      op: absent
# Optional. A disruption injected while the agent works. Its `verify:` key names
# a verification_spec entry to evaluate as soon as the fault lands -- an extra,
# mid-run evaluation, not what makes verification_spec run at all.
chaos_spec:
  - name: Planned Load Spike
    trigger:
      type: time
      delay_seconds: 30
    action:
      type: generate_load
      target:
        service_url: http://localhost:8080
        qps: 200
        duration: 60s
    verify: pods-ready
validated: false
```

Details that matter:

- **Placeholders.** `{{CLUSTER_NAME}}`, `{{PROJECT_ID}}`, `{{APP_LOCATION}}`,
  `{{TARGET_DEPLOYMENT_NAME}}`, and `{{NAMESPACE}}` are substituted from the
  run's cluster/project settings — in `prompt` and `expected_output`, and in
  every string leaf of `chaos_spec` and `verification_spec`.

  `{{GKE_CLUSTER_NAME}}` and `{{GCP_PROJECT_ID}}` were older aliases and no
  longer exist; a task still using them ships the literal braces to the agent.
- **Provider.** These are two separate settings: `stack:` is *what* to build,
  the provider is *where* — `kind` for a local Docker cluster, `gcp` for
  Google Cloud. The provider is what supplies the credentials and fills in the
  variables listed in §3.

  Set it with `provider:` in the task, or with the `INFRA_PROVIDER` env var
  (which wins if both are set). You can leave it out in exactly one case: a
  stack folder named `kind`. That's why `stack: prebuilt/kind` works with no
  `provider:` line — and why renaming that folder to anything else breaks it
  with `requires an explicit provider`.

  Changing the provider never changes *which stack directory runs* — `stack:`
  alone decides that. It does change the credentials used, and the values fed
  into the stack. One of those values is `infra_provider`, set to the provider's
  name, so a stack that declares an `infra_provider` variable can branch on it
  and build different things: devops-bench's own `tf/modules/cluster` uses it to
  pick a GKE or a kind sub-module.

  `prebuilt/kind` does *not* declare that variable (undeclared values are
  dropped before `tofu` sees them), and it hardcodes the kind provider. So for
  this stack `INFRA_PROVIDER=gcp` still builds a kind cluster — it only swaps
  the credentials and injected values. Whether the provider switches what gets
  built is a property of how the stack is written, not of the setting.
- **Stay inside the agent's real tool surface.** A task that asks for
  capabilities the deployed agent doesn't ship (e.g. a shell) scores 0 against
  every real deployment. See `tasks/README.md`.

### Asserting on real cluster state: `verification_spec`

Both judges score *description*: `OutcomeValidity` reads the agent's final text,
`ToolInvocation` reads its trajectory. Neither looks at the cluster. A
`verification_spec` is the part of a task that does — it runs `kubectl` against
the live cluster after the agent finishes and records whether the state the task
was really about actually arrived.

The practical consequence is that `expected_output` should shrink. Anything a
`kubectl` call could settle — replica counts, labels, securityContext fields,
which namespace the workload landed in — moves into `verification_spec` and is
scored deterministically; `expected_output` keeps only the subjective residue
(is the manifest idiomatic, is the exposure mechanism sensible) for the judge.
Upstream's own `tasks/gcp/deploy-hello-app` and `tasks/common/opa-remediation`
are worked examples of that split and are worth reading before you write one.

**It runs unconditionally.** No `chaos_spec` is required. Every entry is
evaluated in one post-agent pass, in declaration order. A chaos entry's
`verify:` key can *additionally* pull one entry in by name and evaluate it the
moment the fault lands, but that is a second, earlier evaluation of the same
entry — not what makes the entry run. The one thing that suppresses the pass is
`--no-infra`, which records `verification_status: skipped_no_infra` because
there is no cluster to ask.

**Shape.** A list of entries. Each entry pairs a check subtree (`check`) with
the vocabulary that scores it:

```yaml
verification_spec:
  - name: workload-running # unique; also the key a chaos `verify:` resolves against
    role: objective # objective | safeguard
    weight: 1.0 # >0; relative share within its role. Default 1.0
    check: # the typed node — one leaf, or a compound tree
      type: pod_healthy
      selector: app=web
```

| field | required | meaning |
|---|---|---|
| `name` | yes | unique across the spec; a duplicate is dropped with an error, not merged |
| `role` | yes | `objective` — something the agent had to achieve. `safeguard` — something that must never have happened |
| `severity` | safeguards only | `recoverable` or `catastrophic`. Required when `role: safeguard`, **rejected** when `role: objective` |
| `weight` | no (default `1.0`) | must be `> 0`. Weights are relative within a role, so a group of eight checks that should not outweigh a group of one is usually written as eight × `0.125` |
| `mode` | no (derived) | `converge` polls the subtree until it holds; `assert` evaluates it exactly once. Derived from `role` if omitted. `hold` parses but is rejected as unbuilt |
| `check` | yes | the node subtree |

The `mode` default is the part worth internalising: an **objective converges**
(the agent is working toward the state, so the check waits for it) and a
**safeguard asserts** (a violation that already happened will not heal, and
polling one only gives it time to disappear before you notice). Overriding this
is rarely right.

Both entries and leaves reject unknown keys. A stray or misspelled field is a
parse error naming the key, not a silently applied default.

**Node types.** Every node takes an optional `name` echoed onto the result;
every leaf also takes an optional `kubeconfig` path to target a specific
cluster (default: the ambient one).

| `type` | fields | passes when |
|---|---|---|
| `resource_property` | `kind` + `op` (required), `resource_name` **xor** `selector`, `namespace`, `path`, `value`, `across_matches` | the general-purpose leaf; see below |
| `pod_healthy` | `selector` (required), `namespace` | `kubectl wait --for=condition=Ready` returns; on failure it falls back to inspecting each matched pod (its `Ready` condition, or the `Running` phase when none is reported yet) |
| `scaling_complete` | `deployment` (required), `min_replicas` (default `1`), `max_replicas`, `namespace` | polled `status.readyReplicas` lands in `[min, max]`; omit `max_replicas` to check only the floor |
| `all` / `parallel` | `checks` (required) | every child passes; children run concurrently (up to 8 at a time). `all` is a vocabulary alias, identical behaviour |
| `sequence` | `checks` (required) | every child passes; runs in order and **fails fast** — the rest are recorded as skipped |
| `any` | `checks` (required) | at least one child passes; evaluated in declaration order, stopping at the first success, so put the cheap check first |
| `none` | `checks` (required) | no child passes |

**`resource_property` is the one you will reach for.** The other two leaves each
answer one fixed question; this one reads any field of any resource. `path` is a
JSONPath (via `jsonpath_ng.ext`) and `op` is one of `eq`, `ne`, `gt`, `gte`,
`lt`, `lte`, `exists`, `absent`, `contains`, `matches`. Four things about it are
easy to get wrong:

- **Prefer a filter to an index.** `containers[?(@.image =~ "hello-app")].image`
  keeps meaning the same thing after a sidecar is injected ahead of the app
  container; `containers[0].image` silently starts checking the wrong one.
  Measured against `jsonpath_ng.ext`, `=~` is the only working substring
  operator, and a filter matching nothing fails closed rather than passing
  vacuously.
- **`across_matches` has no default, and that is deliberate.** Without it, the
  check asserts the flattened `(object, value)` match set holds *exactly one*
  member; a plural match is a loud failure naming everything it found, not an
  arbitrary pick. Set `every` to require the property on every match, or `none`
  to require it on none.
- **`absent` is not `ne`.** `op: absent` with no `path` asserts the selector
  matched no object at all (and passes on zero matches, unlike every other op).
  With a `path` it asserts the path resolved to nothing. To express "this flag
  must not be true" across a manifest that may set it explicitly to `false`,
  omit it entirely, or not have the container at all, the only shape that covers
  all three is `op: eq` / `value: true` / `across_matches: none`.
- **`eq` coerces numerically only when there is a numeric signal** — a non-bool
  number on either side, or a Kubernetes quantity string like `"100m"` / `"1Gi"`.
  Two plain strings compare raw, so an image tag `"1.20"` stays ≠ `"1.2"`.

Compound nodes nest, so "wait for the scale-out, then confirm all pods came up
Ready" is a `sequence` of two leaves. But prefer many small entries over one big
`all`: an entry scores binary, so six assertions bundled into one `all` means a
run that satisfied four of them still scores zero for the whole group, and the
group's joined reason identifies the failing child by index unless you gave
every child a `name`. One weighted entry per assertion earns partial credit and
names the failure by itself. An `all` is the right shape inside a *safeguard*,
where there is no partial credit to preserve anyway.

**Three outcomes, not two.** A check is `pass`, `fail`, or `error` — and `error`
means the check could not be evaluated (a `kubectl` failure, a hung call, a
deadline hit before the node ever ran), not that the condition was observed
false. The distinction is what stops an environmental hiccup from reading as a
safeguard violation. Compound nodes combine three-valued: for a conjunction any
`fail` wins outright, otherwise any `error` makes the group unknown; for `any`
any `pass` wins, otherwise `error`; `none` inverts. Errored entries drop out of
both the numerator and the denominator of every score, and are counted
separately as coverage.

**Timing.** Two budgets. Each converging entry gets 120 seconds, shared across
its whole subtree by a single deadline computed at the top — compound nodes do
not re-budget, so a `parallel`'s children each see the full remaining time and a
`sequence`'s consume it serially. The whole pass gets 600 seconds across all
entries; a converging entry reached after that is recorded `error` with
`verification total budget exhausted before evaluation`. Assert entries are
exempt from the total budget and always run, since a safeguard that goes
unchecked defeats the point of having it.

**Quote placeholders in YAML.** A bare `{{...}}` at the *start* of a value is a
YAML flow mapping and raises a constructor error. `namespace: "{{NAMESPACE}}"`
is fine; so is a mid-value `selector: app={{TARGET_DEPLOYMENT_NAME}}`.
`{{TARGET_DEPLOYMENT_NAME}}` and `{{NAMESPACE}}` resolve from the
`TARGET_DEPLOYMENT_NAME` / `NAMESPACE` env vars, else from
`infrastructure.variables.target_deployment_name` / `.namespace` on the task,
else from the harness default.

**Where the outcome lands.** In `results.json`:

- `verification_report` — one mapping per entry, in declaration order, carrying
  `role` / `severity` / `weight` / `mode` alongside `success`, `status`,
  `reason`, `elapsed_time`, and a `children` entry per member. Each leaf's `raw`
  holds the `kubectl` output that decided it.
- `verification_status` — `evaluated`, `skipped_no_infra` (under `--no-infra`),
  or `not_evaluated` (the pass could not run at all).
- `verification_parse_errors` — one `{name, reason}` per entry that failed to
  validate.

`VerificationMetric` rolls that report into four scores, emitted beside the
judge's rather than replacing them:

| score | is |
|---|---|
| `VerificationCorrectness` | weighted pass fraction over objectives |
| `VerificationRecoverable` | weighted pass fraction over `recoverable` safeguards |
| `VerificationCatastrophic` | a gate: `1.0` if every `catastrophic` safeguard held, `0.0` if any fired. `weight` is ignored here — one trip is enough |
| `VerificationCoverage` | `1 - errored/declared`; what flags a run whose checks never got to run |

A score whose role the task declared no entries for is **omitted entirely**
rather than reported as zero — no opinion is not a failing opinion.

**Authoring errors are recorded, never raised — but they do cost you.** A
malformed `chaos_spec` aborts the task; a malformed `verification_spec` does
not. Bad entries land in `verification_parse_errors` and the run continues
without them. That is not free: each parse error adds `1.0` to the *objective
denominator* with no numerator contribution, so it fails closed and drags
`VerificationCorrectness` down. A spec that never parsed might have declared
anything, and the conservative reading is that it was an unmet objective. A
chaos `verify:` key matching nothing separately writes a failed
`chaos_report.verification` naming the missing key and listing
`known_references`. Check both before concluding a verification passed.

**Adding your own verifier.** The three built-ins cover most of it; anything
else is a subclass of `BaseVerifier` in your repo with a `type` literal and a
`verify(timeout_sec) -> VerificationResult` method, registered through the
`devops_bench.verifiers` entry-point group — the same mechanism, and the same
`uv sync` caveats, as the agent harness in the appendix:

```toml
[project.entry-points."devops_bench.verifiers"]
service_endpoints_ready = "your_evals.verifiers:ServiceEndpointsReadyVerifier"
```

## 5. Run

```bash
PROJECT_ID=local \
CLUSTER_NAME=db-eval-smoke \
PLATFORM_AGENT_TOKEN=$PLATFORM_AGENT_TOKEN \
JUDGE_PROVIDER=gemini JUDGE_MODEL=gemini-flash-latest GEMINI_API_KEY=$GEMINI_KEY \
BENCH_TF_ROOT=./tf \
uv run devops-bench ./tasks/my-provisioned-task --agent-type <your-agent>
```

Any provider devops-bench supports substitutes on that judge line — e.g.
`JUDGE_PROVIDER=anthropic JUDGE_MODEL=claude-sonnet-4-5 ANTHROPIC_API_KEY=$ANTHROPIC_KEY`.
Leave `JUDGE_MODEL` unset for the provider's default.

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
  `--no-infra` (or `BENCH_NO_INFRA=true`), the agent connection, and a key for
  your judge provider.

## 6. Single runs vs `--parallel`: where tofu executes

Identical to the devops-bench repo:

- **Single runs execute tofu in your source stack directory.** `.terraform/`,
  `.terraform.lock.hcl`, and `terraform.tfstate` are written next to your
  `.tf` files. Ignore them (this repo's `.gitignore` covers `.terraform/`,
  `*.tfstate*`, and the lock file — copy those entries; commit none of them).
- **`--parallel` runs are isolated.** Each run gets a private copy of the
  *whole* `tf/` tree in a scratch dir keyed by `TF_DATA_DIR`, so concurrent
  runs of the same stack never contend on lock or state files. Copying the
  whole tree rather than the single stack is what keeps the relative module
  paths from §3 working inside the copy.

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
| `stack '<name>' requires an explicit provider` | no provider given, and the stack didn't deduce — it's an absolute path, or a relative one whose last segment isn't exactly `kind` | add `provider:` to the task (`kind` is a valid value) or set `INFRA_PROVIDER` |
| state/lock files appear under `tf/` | normal for single runs (§6) | keep them gitignored; use `--parallel` for isolation |
| `verification_spec` authored, but `verification_report` is empty | either every entry failed to parse (check `verification_parse_errors`), or `verification_status` is `skipped_no_infra` — `--no-infra` has no cluster to check | fix the entries, or run with real infra |
| `Extra inputs are not permitted: '<key>'` in `verification_parse_errors` | a misspelled or stale key (`min_replica:`) — entries and leaves both reject unknown fields rather than quietly defaulting | the `reason` names the offending key; check it against the tables in §4 |
| `severity is required when role is 'safeguard'` / `severity is not allowed when role is 'objective'` | the two are strictly paired | give every safeguard a `severity`, and remove it from every objective |
| `VerificationCorrectness` lower than the passing entries suggest | each parse error adds 1.0 to the objective denominator, failing closed | clear `verification_parse_errors`; a commented-out entry costs nothing, a broken one costs a point |
| `path '<p>' resolved to N value(s) across [...]` | no `across_matches`, so the check asserted exactly one match and found several | narrow with `resource_name`/`selector`/a JSONPath filter, or set `across_matches: every`/`none` |
| `path '<p>' did not resolve in any of N matched object(s)` | the path matched nothing; it fails closed rather than passing vacuously | check the path against real `kubectl get -o json` output — an unobservable predicate is not a satisfied one |
| `op 'absent' already asserts emptiness and does not take 'across_matches'` | the two overlap by construction | drop `across_matches`; for "must not be true across matches" use `op: eq` / `value: true` / `across_matches: none` |
| `chaos verify reference '<x>' not found` on the chaos report | the `verify:` key doesn't match any `verification_spec` entry name | fix the name; the message lists `known_references`. The entry still runs in the post-agent pass regardless |
| entries report `verification total budget exhausted before evaluation` | the 600s whole-pass budget ran out — usually several converging objectives each burning their full 120s | trim entries, or find out why the early objectives never converge; this is `error`, not a cluster-state failure |
| a leaf reports `deadline exhausted before evaluation` | the entry's 120s budget ran out upstream, usually on an earlier leaf that blocked | shrink the subtree, or check why the first check is slow |

## Appendix: writing your own agent harness

Everything above is about *tasks*. This appendix is about the other half — the
code that actually drives your agent. You only need it if the built-in agent
types (`gemini`, `api`, `openclaw`, `antigravity`) don't fit: they cover CLI
tools and direct model APIs, so anything else — an agent running as a service
in a cluster, a custom protocol, an internal gateway — needs a harness of its
own. This repo's `kube_agents_bench/harness.py` is a full worked example.

A harness lives entirely in *your* repo. Nothing is contributed to
devops-bench, and the CLI never learns your agent's name at build time.

### A1. The contract

Subclass `AgentHarness` and implement one method:

```python
def _execute(self, prompt: str, workspace_path: Path | None = None) -> AgentResult
```

The base class already handles the parts every agent would otherwise repeat:
it stamps wall-clock `latency`, wraps the call in a deepeval trace when
deepeval is installed, and catches any unexpected exception and converts it to
a failed result — so one agent crash never aborts a benchmark run.

`workspace_path` is a directory the harness may hand you to run in, so files
the agent writes can be collected afterwards. It's `None` when no workspace was
supplied, and an agent with no local filesystem (anything remote) can ignore it
entirely.

### A2. A minimal harness

Write it as two pieces: a *transport* that talks to your agent, and a *parser*
that turns one response payload into an `AgentResult`. The parser is worth
keeping as a plain module-level function — it needs nothing from the instance,
and separating it is what lets you test the risky half without a server (A5).

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
        request = urllib.request.Request(
            os.environ["MY_AGENT_URL"],
            data=json.dumps({"input": prompt}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=600) as response:
                payload = json.loads(response.read().decode())
        except (OSError, json.JSONDecodeError) as exc:
            # A failure you anticipated: return it, don't raise.
            return AgentResult.errored(f"{type(exc).__name__}: {exc}")

        if not isinstance(payload, dict):
            return AgentResult.errored(f"expected a JSON object, got {type(payload).__name__}")
        return _parse_response(payload)
```

Handle failures you can *anticipate* — a dead endpoint, a parse miss, a
timeout — by returning `AgentResult.errored(...)`. The base class's exception
handler is a safety net for bugs, not an error-handling strategy; a returned
error carries a readable message into `results.json`, while a raised one
arrives as a stack-trace string. Inside the parser, prefer `.get()` with a
default over `[...]` for the same reason: a field your agent omitted should
become an empty output or a recorded error, not a `KeyError` traceback.

The parser above is deliberately the easy case — it assumes each tool call
arrives with its result already attached. Real agents often report the call and
its result as two separate events, which is where the folding described below
belongs. `kube_agents_bench/harness.py` has a worked version that correlates
the two by `call_id` and falls back to the most recent unmatched call.

### A3. What goes in the result

| field | who consumes it |
|---|---|
| `output` | the `OutcomeValidity` judge — this is the text that gets scored, and what CI gates on |
| `trajectory` | the `ToolInvocation` judge — a list of `ToolCall.to_dict()` entries |
| `tokens` | reported in `results.json`. Start from `empty_tokens()` and fill only what your agent reports, so "not reported" stays `None` instead of being recorded as a measured zero |
| `latency` | leave it — the base class fills it in |
| `errors` | non-empty marks the run as failed, which is how a crashed run is told apart from one that legitimately produced no text |
| `metadata` | free-form passthrough (session ids, raw provider stats) |

One trajectory subtlety worth knowing before you hit it: if your agent reports
a tool's *result* as a separate event from the *call*, fold the result into the
call it belongs to rather than appending it as a second entry. A trajectory
metric reads an extra argument-less entry as a redundant tool invocation and
marks the agent down for a call it never made.

### A4. Register it

Declare the entry point in your `pyproject.toml` — this is the whole
registration:

```toml
[project.entry-points."devops_bench.agents"]
myagent = "your_evals.harness:MyAgentHarness"
```

Then `uv sync` and confirm it resolves:

```bash
uv run python -c "from devops_bench.agents import AGENTS; print(AGENTS.get('myagent'))"
uv run devops-bench ./tasks --no-infra --agent-type myagent
```

devops-bench scans that entry-point group the first time it sees an agent name
it doesn't recognise, so `--agent-type myagent` works without anything in the
command line — or anywhere in devops-bench — referring to your package. That
indirection is what lets the two repos ship on independent schedules.

### A5. Test it without a cluster

Two levels, and the split in A2 is what buys you the first one:

- **The parser, on its own.** `_parse_response(payload)` is a pure function, so
  a test is one recorded payload in and a set of assertions out — no server, no
  network, no agent. Paste in a response your agent really produced and pin the
  output text, the trajectory shape, and the token buckets. This is the part
  most likely to break when your agent's response format shifts, and it's the
  cheapest to cover.
- **The transport, end to end.** Point the harness at a stub HTTP server on a
  local port and drive `MyAgentHarness().run(prompt)`. This catches the wiring
  the parser tests can't see: headers, auth, timeouts, and the error paths.

`bench/tests/test_harness.py` is the pattern for both — the stub replays a
verbatim recorded response, and separate tests drive the failure paths (HTTP
500, non-object JSON, unreachable endpoint) to confirm each returns an errored
result instead of raising.

### A6. Gotchas

| Symptom | Cause |
|---|---|
| entry point ignored after you added it | entry points are read from *installed* metadata: re-run `uv sync`. `--no-sync` freezes whatever is already in the venv and is the single most common cause of this |
| not registered, and the name has a capital in it | keys must be lowercase (the configured agent type is lowercased before lookup, so an uppercase key could never match). The scan skips it with a `skipping entry point ...` warning rather than failing — check the log |
| not registered, and the name looks right | your harness module failed to import. A bad module path or an `ImportError` inside it is logged and skipped, so it surfaces as "not registered" rather than as the import error. Import the module by hand to see the real traceback |
| `AlreadyRegisteredError` | you used both the entry point and a `@AGENTS.register(...)` decorator. Pick one — registering never triggers a scan, so the two collide depending on import order |
| `TypeError` on agent construction | the class is instantiated as `agent_cls(config)`. If you override `__init__`, accept a positional `AgentConfig` first |
| your name silently resolves to a built-in | `gemini-cli` is an alias for `gemini`; avoid the built-in names |
