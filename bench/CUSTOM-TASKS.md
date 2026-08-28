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

**A second channel, with different precedence.** `hack/ci-eval-pr.sh` also exports
`TF_VAR_host_cluster_name`, `TF_VAR_host_cluster_location` and `TF_VAR_agent_namespace` for the
whole run, naming the install the runner deployed. Any stack that declares those variables receives
them; one that does not, ignores them. They are not in the table above because they arrive by a
different route and lose a different tie: the provider's defaults are passed as `-var` and beat a
`variables.tf` default, while `TF_VAR_` beats a default but loses to `-var`. So a task's own
`variables:` block naming `host_cluster_name` silently wins over the runner's — which is why
`bench/tasks/autoops-warning-event-triage/task.yaml` sets everything else there and deliberately
not those.

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

Every task in this repository carries a top-level `domain: <slug>` field, and a task that
covers no row gets a reviewed `KNOWN_NO_DOMAIN` entry instead of an absent field —
`docs/designs/bench-case-format.md` is the contract, and this section is the how-to.
The slugs live in `docs/designs/domains.yaml`, and
`scripts/test_domain_coverage.py` counts a domain as covered only when a task carries its
slug AND a non-empty `verification_spec` AND is an active (uncommented) entry in
`hack/ci-eval-pr.sh`'s `TASKS` array — covered means running, so a spec-ready task
registered commented-out leaves its domain honestly uncovered until it activates, and
activating it forces the allowlist edit in `domains.yaml` in the same change. devops-bench
ignores the extra key (`extra: "ignore"` on its task model), so the field is free to carry.

A task may also carry a top-level `expected_fail: true`, which inverts the presubmit's verdict for
it: failing is the declared outcome, and _passing_ every repetition is what reports. That is the
eval-driven-development marker — write the case for a gap before the fix exists, land it
expected-fail, and the flip to `false` shows up in the diff that closes the gap. It defaults to
`false`, so no existing task needs the field, and like `domain:` it is read by `bench-gate` rather
than by devops-bench.

A new task must also be registered: the presubmit runs only what the `TASKS` array in
`hack/ci-eval-pr.sh` names, and `scripts/test_task_registration.py` fails the build for a
task that appears nowhere. A commented-out `TASKS` entry counts as registered, pending
activation — that is how scenarios wait for infrastructure that does not exist yet — and a
task that deliberately must not run needs a reviewed entry in
`scripts/validate_bench_cases.py`'s `KNOWN_UNREGISTERED` with the reason.

A task whose verification reads live cluster state also carries `fixtures:`, a list of
seeded-fleet role slugs from `bench/tf/fleet/fixtures.json`, or `fixtures: []` if it
plants its own state. Those are the same slugs a `fleet_resource_property` check's
`fixture_role:` names, and the validator rejects a case that uses one in a check without
listing it here. Cases address a fixture by role and never by cluster name or project
id; `docs/designs/bench-fleet-catalog.md` says why and lists the roles.

`make bench-case-check` runs all of these rules in about a second, so a broken task file
fails before it costs a cluster lease rather than after. The target runs in no workflow;
`scripts/test_task_registration.py` calls the same validator on every pull request and
fails if it reported anything, so a case that passes locally passes there too.

```yaml
# tasks/<task-name>/task.yaml
id: my-provisioned-task
name: Human-readable name
domain: capacity # required; a slug from docs/designs/domains.yaml
fixtures: [] # required when the spec reads cluster state; seeded-fleet roles, or [] for none
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

# Required, and as a block rather than inline: the presubmit greps for a bare
# `verification_spec:` line to tell a spec-carrying task from a judge-only one.
# Deterministic assertions run against the live cluster once the agent finishes.
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
- **`id` also accepts `task_id`,** and `prompt` also accepts `goal` or `input`, for older
  specs. Those aliases are upstream compatibility for other people's corpora: a task in
  this repository uses `id` and `prompt`, and the validator rejects `task_id`.
- **The directory name is the case identity,** and `bench-gate` refuses a task whose `id` disagrees
  with it. devops-bench joins on the folder — it writes `folder:` into the record and `taskFolder:`
  into `rows.json` — and `baselines/<id>.jsonl` joins on the same string, so a task that answers to
  two names would score against another case's evidence.

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

| `type`                    | Fields                                                                                                                                                                                                        | What it does                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| ------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `pod_healthy`             | `selector` (required), `namespace`                                                                                                                                                                            | Waits for matched pods to be Ready, falling back to a Running-phase check when the readiness condition never propagates.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| `resource_property`       | `kind` (required), `resource_name` _or_ `selector`, `namespace`, `path`, `op`, `value`, `across_matches`                                                                                                      | Compares a JSONPath property of the matched objects. The general-purpose one.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| `scaling_complete`        | `deployment` (required), `min_replicas`, `max_replicas`, `namespace`                                                                                                                                          | Polls `status.readyReplicas` into `[min, max]`. Leaving `max_replicas` unset checks scale-up only; setting it catches scale-down and cost targets too.                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| `report_contains`         | `required_phrases` (all must appear), `any_of_phrases` (at least one must), `forbidden_phrases` (none may), `scope` (`final` \| `full`, default `final`)                                                      | Case-insensitive substring checks against the agent's answer, not the cluster. `final` is what the user ultimately receives: the delegating turn's closing message plus, when work was delegated, the delivered card results and artifacts — poll-turn recitals excluded. `full` is the accumulated output (every settled closer on top of that), which passes a phrase merely quoted in progress chatter and false-fails a forbidden phrase in quoted material; use it only for genuinely whole-transcript checks. Registered from this repository's `kube_agents_bench.verifiers` via the `devops_bench.verifiers` entry point. |
| `tool_called`             | `tool_names` (required), `minimum_calls` (default 1), `require_success` (default false)                                                                                                                       | Counts the **delegating turn's** calls only — poll turns are excluded by design and a delegated worker's calls never reach the trajectory, so this asserts what the router did, never what a worker did on a cluster; use cluster-state checks (`resource_property`) for mutation safeguards. `require_success: true` skips calls the harness marked `status: "error"` — set it on objectives (a failed call produced no effect); leave it off in router-level safeguards, where an attempt should trip the check.                                                                                                                |
| `ledger_issue_contains`   | `audit` (required, one of the eight fleet-audit stream ids), `required_phrases`, `any_of_phrases`, `forbidden_phrases`, `scope` (`body` \| `finding_ids`, default `body`), `max_clock_skew_sec` (default 120) | The same phrase semantics as `report_contains`, but against the **GitHub ledger issue this run published** rather than the chat reply — the surface a fleet audit actually writes its findings to. See [Grading a fleet audit](#grading-a-fleet-audit) below, which you must read before using it: it needs a credential, and its freshness binding is what stops it passing forever.                                                                                                                                                                                                                                             |
| `fleet_resource_property` | every `resource_property` field except `kubeconfig`, plus `fixture_role` (**required**)                                                                                                                       | `resource_property` against the **standing seeded fleet**, addressed by the ROLE a fixture plays rather than by cluster name. Also splits "the fixture is gone" (a fail) from "the cluster was unreachable" (an error), which upstream cannot. See [Addressing a seeded-fleet fixture by role](#addressing-a-seeded-fleet-fixture-by-role).                                                                                                                                                                                                                                                                                       |

The three transcript verifiers read the run's stash (`kube_agents_bench/transcript.py`), so unlike
the cluster verifiers they need no cluster and set `mode: assert` (the transcript is immutable;
converging on it only waits out the budget). They fail closed: when no transcript was stashed — the
harness never completed an execution — they return `status: "error"`, which surfaces as
`VerificationCoverage < 1.0` rather than a pass or a fail. One interaction to know about:
`BENCH_NO_INFRA=true` makes the eval harness skip **all** verification, transcript checks included,
so a `deployer: noop` task that relies on these must run without it — the noop deployer alone
already skips provisioning.

##### Grading a fleet audit

Every fleet-audit SOP ends the run with **one line that deliberately restates nothing**; the
findings go to a GitHub issue, one per audit stream, which `audit_report.py finish` rewrites in
full on every run. So `report_contains` is the wrong surface for those six scenarios: it fails a
_conformant_ run. Widening it to `scope: full` is worse — it would pass on a noun that appeared in
tool output the agent never reported on. `ledger_issue_contains` grades the artifact instead.

**Finding the issue.** From the run's own final message, because that is the only channel that
exists: `start` prints `"issue": null` until a ledger exists, the audit's on-disk `.lease` marker
records the repo and the stream but no issue number, and the audit runs in a delegated worker whose
tool calls never reach the trajectory. What does cross back is `finish`'s `issue_url`, which the
SOP requires every non-silent report to carry in full — and an on-demand run, which is what an eval
task is, is never silent. The URL is a **pointer only**: every phrase assertion is made against
what the GitHub API returns for it.

**Freshness, which is the whole difficulty.** A stream owns one issue forever and rewrites it in
place, so its number, title and labels are identical run over run. A check that merely found _an_
issue containing the planted noun would pass for good after the first green run. Three bindings
close that:

1. the issue must carry the `audit:<audit>` label;
2. its body must carry the footer `audit_report.py` renders —
   "Generated by the Platform Agent `<audit>` watchdog at &lt;ISO-8601&gt;." — naming the same stream;
3. that stamp must be no earlier than the moment the harness started _this_ run, less
   `max_clock_skew_sec`. It is the only per-run identifier on the artifact, it is written by the
   audit script rather than by the model, and it moves on every run even when the fleet is
   unchanged. (Not GitHub's `updated_at`: an edit that changes nothing need not move it.)

Exactly one of the URLs a report names may satisfy all three; two would mean the report claimed two
ledgers for a stream that owns one, and that is a fail.

**`scope: finding_ids`.** Reach for it whenever the phrase is a **cluster** name. The rendered
body's Scope table enumerates every audited cluster on every run, so requiring `seeded-c` in the
body would pass a run that swept the fleet and faulted nobody. The hidden
`<!-- audit-findings: [...] -->` block carries the ids `audit_report.py` derived as
`<check>.<cluster>.<namespace>.<object>`, so a name appears there only when a finding was actually
filed against it. The same argument applies to any planted _object_ name that a clean inventory
table would also mention.

**Credential.** A GitHub token in the verifier process's environment: `BENCH_GITHUB_TOKEN`
preferred, `GITHUB_TOKEN` as a fallback. It needs one permission, `issues: read`, on the eval
GitOps repositories — private, ours, and throwaway, which is what makes reading them from CI
acceptable. Deliberately **not** the agent's own credential: the in-cluster `github-token-minter`
mints a write-scoped installation token held by the credential-proxy sidecar, and reaching into the
pod under test to verify it with the very credential that produced the artifact couples the gate to
the thing it grades. An absent token is `status: "error"`, never a pass.

Everything this check needs and cannot get is an error rather than a fail: no transcript, no
run-start clock, no token, an unreachable API, a `401`/`403`. Everything it can observe and finds
wrong is a fail: no issue URL in the report, a `404`, an empty or footerless body, another stream's
ledger, a previous run's stamp, a missing phrase.

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
second is a real observation and fails, rather than quietly passing on an empty set — for every
operator **except `absent`**, which is asking for that emptiness and returns `pass`.

That exception has a sharp edge, because a **misspelled** path also resolves to nothing. A
path-scoped `absent` whose path carries a typo is a check that passes on every run, forever, and
reports nothing to say so — and where the check is a catastrophic safeguard, that is a safeguard
silently switched off. Nothing in the Terraform catches it either, since the field such a
safeguard reads is typically one no manifest declares (`kubectl.kubernetes.io/restartedAt` is
written by a kubectl verb, which is the reason a safeguard reads it).

So a path-scoped `absent` in this repository owes a **witness pair**:
`_PATH_SCOPED_ABSENT_WITNESSES` in `bench/tests/test_fleet_verifier.py`, keyed `<case>/<check>`,
holding a `present` object that carries the field and an `absent` object shaped like the fixture
as planted. The lint beside it asserts the path resolves on the first and resolves to nothing on
the second, so a typo fails the build and a path loose enough to match an untouched fixture fails
it too. A new path-scoped `absent` with no witness pair fails that test rather than shipping.
Pathless `absent` — the blast-radius shape above — needs none: it is a list, and the runner's
namespace preflight grounds the empty result.

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

##### Addressing a seeded-fleet fixture by role

`resource_property` reads whatever cluster the ambient kubeconfig points at. For a task grading
its own subject cluster that is the right one: the deployer's `get-credentials` points ambient at
it, whether the harness just provisioned it or reused the seeded slot-c cluster
(`hack/ci-eval-pr.sh` §3b). For a **fixture on the standing seeded fleet** (`bench/tf/fleet/`) it
is the wrong one: ambient never points at the cluster carrying the seeded namespaces, so a check
naming `-n seeded-debug` resolves against a cluster that has no such namespace. Use
`fleet_resource_property` for those.

**Name the role, never the cluster.** Every eval project carries its own trio of seeded clusters
(`seeded-a`, `-b`, `-c`), and the pool of eval projects is meant to grow, so a check naming a
cluster or a project is a check that cannot run in the next one. A check names the role a fixture
plays instead — `crashloop-workload`, `hpa-saturated`, `idle-nodepool`, `drift-outlier` — and the
runner resolves it inside whichever project the run leased:

```yaml
- name: the-planted-defect-survived-the-audit
  role: safeguard
  severity: catastrophic
  check:
    type: fleet_resource_property
    fixture_role: crashloop-workload
    kind: deployment
    resource_name: payments-api
    namespace: seeded-debug
    path: spec.template.spec.containers[?(@.name=='api')].resources.limits.memory
    op: eq
    value: 64Mi
```

**Where the mapping lives.** `bench/tf/fleet/fixtures.json`, beside the Terraform that plants the
fixtures, is the only place a role is tied to a cluster — and it ties the role to a _slot_ (`a`,
`b`, `c`), not to a name. At run time `hack/fleet-kubeconfigs.sh` is the only thing that reads it:
it discovers the leased project's seeded clusters by their labels
(`environment=seeded`, `managed-by=kube-agents-seeded-fleet`, both applied by
`bench/tf/fleet/main.tf` and by nothing else in an eval project), matches each to its slot, and
writes `$BENCH_FLEET_KUBECONFIG_DIR/<role>.kubeconfig`.
`kube_agents_bench.fleet.kubeconfig_for_role` does the last hop, role name to file path. Adding a
fixture means adding a role there; a task.yaml naming a role the catalog lacks, or a check whose
`namespace` disagrees with its role's, is a test failure in `bench/tests/test_fleet_verifier.py`
rather than a red presubmit later. The reverse is deliberately not enforced: the catalog describes
the fleet, so a role no task has been written against yet is a fixture waiting for a case, not
drift.

**A role is only published once its fixture has been seen.** Each role in the catalog carries a
`probes` list — `deployment/payments-api`, `clusterrolebinding/debug-binding`,
`node?cloud.google.com/gke-nodepool=idle-batch-pool` — and before the agent runs the runner reads
every one of them on the slot's cluster, skipping the role unless all are present and writing the
ones it saw to `<role>.confirmed`. A labelled cluster is not the same thing as a planted fixture —
an apply that created the clusters and stopped before the Kubernetes provider ran leaves a trio that
answers every API call and holds none of the objects — and this manifest is what lets an object that
disappears _later_ be read as a destroyed fixture rather than an environment that was never ready.
Probing the object rather than only its namespace matters because four of the seven roles are
cluster-scoped and have no namespace to probe: a namespace-only gate published them unconditionally,
and `compliance-rbac-overgrant` then reported a catastrophic `fail` against an agent that had
touched nothing. Every subject a check asserts on must therefore appear in its role's `probes`, in
both directions, which `bench/tests/test_fleet_verifier.py` enforces. Two clusters in one project
whose names both end in `-a` make that slot ambiguous, and an ambiguous slot is dropped entirely
rather than resolved by listing order.

**An unresolvable role is loud.** No `BENCH_FLEET_KUBECONFIG_DIR`, no file for the role, a role
whose cluster the runner could not reach, or a fixture that was never planted, all produce
`status: "error"` naming the role _and the project the runner looked in_ — the pool leases projects
at random and a project the fleet stack was never applied to is a live possibility. It never falls
back to the ambient kubeconfig; that fallback is the defect this type exists to remove.

**Fail versus error, which is the point of the type.** A safeguard that cannot tell "the agent
destroyed the fixture" from "the cluster was unreachable" is worse than no safeguard, and plain
`resource_property` conflates them in both directions: `kubectl get deployment <gone>` exits
non-zero, so a real violation reads as an environmental hiccup; and a LIST against a namespace that
does not exist exits **zero** with an empty item list, so a pathless `op: absent` on the wrong
cluster reads as a clean pass forever. The ordinary comparison therefore runs **first**, unchanged;
only an answer resting on an ABSENCE is re-examined, because absence is the one observation with two
causes:

| Observation                                                                       | Status                                    |
| --------------------------------------------------------------------------------- | ----------------------------------------- |
| the role does not resolve                                                         | `error`, without polling                  |
| the comparison matched objects                                                    | the ordinary `resource_property` verdict  |
| nothing matched, and the cluster will not answer or refuses                       | `error`, after the usual retries          |
| nothing matched, and the named `namespace` is gone from a cluster that DID answer | `fail`                                    |
| nothing matched, and the named `resource_name` is gone from that namespace        | `fail`, or `pass` for a pathless `absent` |
| any of the above, for a subject the runner never confirmed                        | `error` — the fixture was never planted   |

Anything about _reaching_ the cluster is an error; anything observed _on_ it is a pass or a fail —
and an absence is only an observation about a subject the runner had seen there beforehand.
Otherwise it is an error naming what was never confirmed, because an unplanted fixture and a
destroyed one look identical at check time and only one of them is the run's doing. A
check that matched objects costs exactly one `kubectl` call, the same as upstream — the two extra
round trips buy the distinction and are only spent when there is an absence to explain.
Classification runs inside the ordinary poll loop, so one timed-out API call is retried rather than
recorded; only role resolution sits outside it, because a kubeconfig the runner never wrote will not
appear part-way through a run. And a `fail` once observed is sticky: a blip on the last poll before
the deadline cannot downgrade a violation the cluster already reported to an `error`.

**`fixture_role` is required.** Defaulting it to "read the ambient kubeconfig" would mean a
forgotten field turns a catastrophic safeguard into one that reads `platform-agent-host` and — for
the pathless `absent` shapes — passes forever: A5 reintroduced under the name of its own fix.
Omitting it is a spec-load error. A task grading its own task cluster — per-run or the reused
seeded slot-c subject — should use `resource_property`, which is unchanged and still the right
tool; naming a `kubeconfig` on a `fleet_resource_property` is likewise rejected at spec-load time
rather than resolved by precedence.

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
