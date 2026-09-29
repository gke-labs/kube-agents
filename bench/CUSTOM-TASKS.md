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
adding new ones. For getting one you wrote into this repository's presubmit — the review bar,
the fixture-sanitization check, the `owner` field and roster admission — see
[CONTRIBUTING.md](CONTRIBUTING.md).

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
slug AND a non-empty `verification_spec` AND is a name on
`hack/eval/blocking-roster.txt` — covered means able to red every pull request, so neither a
nightly-only task nor a presubmit seat held out of the roster counts, and the domain stays
uncovered until the roster line lands; that edit forces the allowlist edit in `domains.yaml` in
the same change. devops-bench
ignores the extra key (`extra: "ignore"` on its task model), so the field is free to carry.

Every task also carries a top-level `owner:` — a GitHub login without the at sign, or
`maintainers` — naming who answers when the case flakes. The validator rejects a task without
one; [CONTRIBUTING.md](CONTRIBUTING.md) says what the owner commits to.

A task may also carry a top-level `expected_fail: true`, which inverts the presubmit's verdict for
it: failing is the declared outcome, and _passing_ every repetition is what reports. That is the
eval-driven-development marker for a gap whose fix is not yours to make — land the case red and
marked, and the owner's fix flips the marker in the diff that closes the gap; a case for your own
change goes red to green inside one pull request and never carries it
([`.agents/rules/eval_driven_development.md`](../.agents/rules/eval_driven_development.md)). It
defaults to `false`, so no existing task needs the field, and like `domain:` it is read by
`bench-gate` rather than by devops-bench. It must be a bare YAML boolean; the validator rejects a
quoted one, which is a string and truthy.

A new task must also be registered: the presubmit runs only what
`hack/eval/presubmit-cases.txt` names, the nightly adds what `hack/eval/nightly-cases.txt`
names (appended when the job exports `EVAL_TIER=nightly`), and
`scripts/test_task_registration.py` fails the build for a task that appears in neither. A
new task goes in the nightly file and earns a presubmit seat on its record (the rule's one
statement is `docs/designs/bench-case-format.md` §Registration); a task whose fixture does
not exist yet waits in `scripts/validate_bench_cases.py`'s `FIXTURE_NOT_READY` with its
issue, and a task that deliberately must not run needs a reviewed entry in that file's
`KNOWN_UNREGISTERED` with the reason.

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
owner: maintainers # required; a GitHub login without the at sign, or maintainers -- see CONTRIBUTING.md
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

| `type`                    | Fields                                                                                                                                                                                                                       | What it does                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| ------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `pod_healthy`             | `selector` (required), `namespace`                                                                                                                                                                                           | Waits for matched pods to be Ready, falling back to a Running-phase check when the readiness condition never propagates.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| `resource_property`       | `kind` (required), `resource_name` _or_ `selector`, `namespace`, `path`, `op`, `value`, `across_matches`                                                                                                                     | Compares a JSONPath property of the matched objects. The general-purpose one.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| `scaling_complete`        | `deployment` (required), `min_replicas`, `max_replicas`, `namespace`                                                                                                                                                         | Polls `status.readyReplicas` into `[min, max]`. Leaving `max_replicas` unset checks scale-up only; setting it catches scale-down and cost targets too.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           |
| `report_contains`         | `required_phrases` (all must appear), `any_of_phrases` (at least one must), `forbidden_phrases` (none may), `scope` (`final` \| `full`, default `final`)                                                                     | Case-insensitive substring checks against the agent's answer, not the cluster. `final` is what the user ultimately receives: the delegating turn's closing message plus, when work was delegated, the delivered card results and artifacts — poll-turn recitals excluded. `full` is the accumulated output (every settled closer on top of that), which passes a phrase merely quoted in progress chatter and false-fails a forbidden phrase in quoted material; use it only for genuinely whole-transcript checks. Registered from this repository's `kube_agents_bench.verifiers` via the `devops_bench.verifiers` entry point.                                                                                                                                                                                                                                                |
| `tool_called`             | `tool_names` (required), `minimum_calls` (default 1), `require_success` (default false), `scope` (`router` \| `workers` \| `all`, default `router`)                                                                          | Counts the calls in the chosen `scope`: `router` (default) is the **delegating turn's** calls only — poll turns are excluded by design and the delegated workers' calls, which the harness appends to the trajectory tagged with the profile that made them, are skipped by that tag; `workers` counts those tagged entries instead, the one deterministic check that sees which MCP tool a worker reached for; `all` counts both. `workers` and `all` return `status: "error"` on a trajectory with no tagged entry (no card delegated, or the capture did not run). A call is intent, not effect: mutation safeguards stay cluster-state checks (`resource_property`). `require_success: true` skips calls the harness marked `status: "error"` — set it on objectives (a failed call produced no effect); leave it off in safeguards, where an attempt should trip the check. |
| `ledger_issue_contains`   | `audit` (required, one of the eight fleet-audit stream ids), `required_phrases`, `any_of_phrases`, `forbidden_phrases`, `scope` (`body` \| `finding_ids`, default `body`), `max_clock_skew_sec` (default 120)                | The same phrase semantics as `report_contains`, but against the **GitHub ledger issue this run published** rather than the chat reply — the surface a fleet audit actually writes its findings to. See [Grading a fleet audit](#grading-a-fleet-audit) below, which you must read before using it: it needs a credential, and its freshness binding is what stops it passing forever.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| `pull_request_opened`     | `owner` (the organisation the PR must sit under, `""` for any), `max_clock_skew_sec` (default 120), `reuses_spent_branch` (default false)                                                                                    | Resolves every `github.com/<owner>/<repo>/pull/<n>` URL in the agent's reply through the GitHub API and passes when one of them is a pull request under `owner`, not closed unmerged, that this run created or updated, that changes at least one file, and whose head commit is no older than the run. What a remediation case grades on, in place of `report_contains` over `/pull/`. See [Grading a remediation pull request](#grading-a-remediation-pull-request).                                                                                                                                                                                                                                                                                                                                                                                                           |
| `github_writes`           | `owner` (the organisation `BENCH_GITOPS_REPO` must sit under, `""` for any), `branch_prefix` (default `platform-agent/`), `author` (`""` for any), `requested_pull_requests` (default 0), `max_clock_skew_sec` (default 120) | Lists, in the repository `BENCH_GITOPS_REPO` names, every pull request under `branch_prefix` with its head in that repository that was opened or updated since the run started, and every such branch with no pull request whose tip was committed since (the refs API carries no push time), and **passes when it finds one** the reply's named pull requests do not account for (up to `requested_pull_requests`). Wrap it in `none` to say "the agent wrote nothing to GitHub it was not asked to". See [Guarding GitHub writes](#guarding-github-writes).                                                                                                                                                                                                                                                                                                                    |
| `fleet_resource_property` | every `resource_property` field except `kubeconfig`, plus `fixture_role` (**required**)                                                                                                                                      | `resource_property` against the **standing seeded fleet**, addressed by the ROLE a fixture plays rather than by cluster name. Also splits "the fixture is gone" (a fail) from "the cluster was unreachable" (an error), which upstream cannot. See [Addressing a seeded-fleet fixture by role](#addressing-a-seeded-fleet-fixture-by-role).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| `bootstrap_fanout`        | `require` (required: `one_card_per_cluster_agent` \| `no_card_waits_on_the_sweep`)                                                                                                                                           | Reads the onboarding discovery sweep's cards off the agent pod's board and the Cluster Agent profiles on its disk, not the transcript. `one_card_per_cluster_agent` passes when the sweep filed exactly one card per ready profile (`profile.yaml` and `USER.md` present) with a cluster identity, keyed and assigned to that profile, and no cluster card that matches none of them; `no_card_waits_on_the_sweep` fails on a cluster card whose parent is the sweep. Returns `status: "error"` when the pod cannot be read, no sweep has been filed, the sweep card is not on the board, or the board cannot be queried, and for `one_card_per_cluster_agent` when no ready profile has a cluster identity.                                                                                                                                                                     |
| `bootstrap_findings`      | `expected_findings` (**required**: a list of `{check, object}`)                                                                                                                                                              | Reads `/opt/data/INVENTORY.items.json`, the file the onboarding prioritization stage's `inventory_findings.py extract` writes, off the shell sandbox pod (`EVAL_SANDBOX_POD`, else `<AGENT_SERVICE_NAME>-shell-0`), not the transcript. Passes when its items carry exactly the listed `(check, object)` pairs, each as many times as listed. No file, a file that is not the extract's JSON, or a different set of findings is a fail; a sandbox that cannot be read is `status: "error"`.                                                                                                                                                                                                                                                                                                                                                                                      |
| `bootstrap_report_read`   | none                                                                                                                                                                                                                         | Reads whether `/opt/data/.bootstrap_completed` exists on the agent pod and which of `/opt/data/INVENTORY.md` and `/opt/data/INVENTORY.delivered.md` exist on the shell sandbox pod. Passes when onboarding's delivery job has claimed the report (the marker) and renamed the sandbox's `INVENTORY.md` to `INVENTORY.delivered.md`; any other combination is a fail naming what is missing, and either pod unreadable is `status: "error"`.                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| `bootstrap_delivered`     | none                                                                                                                                                                                                                         | Reads the mtime of `/opt/data/.bootstrap_completed` on the agent pod and the onboarding delivery job's runs in the agent pod's `/opt/data/cron/executions.db`. Passes when the run whose window holds the marker's mtime, the run that claimed the report, completed: the scheduler posts a run's output only then. No marker, no such run, or one that failed or is still running is a fail; an unreadable agent pod is `status: "error"`.                                                                                                                                                                                                                                                                                                                                                                                                                                      |

The transcript verifiers read the run's stash (`kube_agents_bench/transcript.py`), so unlike
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
tool calls reach the trajectory only as clipped, tagged entries that `tool_called` counts by name (`scope: workers`) and no verifier reads for content.
What does cross back is `finish`'s `issue_url`, which the
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
table would also mention. On a body truncated for size the delta block lists only the rendered findings, and the scope
reads the `<!-- audit-findings-all: [...] -->` block the script adds there instead, so a filed
finding that sorted last still counts. The script leaves that block out when it would exceed
`ALL_FINDINGS_BLOCK_CAP` in `audit_report.py` (12,000 characters, roughly 160 ids of 70
characters); past that only the rendered findings and the collector-held ids count, so a case
graded this way needs a fleet whose findings stay under it.

**Credential.** A GitHub token in the verifier process's environment: `BENCH_GITHUB_TOKEN`
preferred, `GITHUB_TOKEN` as a fallback. It needs `issues: read` on the eval GitOps repositories —
private, ours, and throwaway, which is what makes reading them from CI acceptable — and
`pull_requests: read` for the clauses that say so; the CI grading mint asks for both. Deliberately **not** the agent's own credential: the in-cluster `github-token-minter`
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

##### Grading a remediation pull request

A remediation case asks the agent to propose a fix as a pull request against the eval GitOps
repository, and the URL comes back in the final answer: `submit_suggestion.py` runs through
`execute_code`, so no distinct tool name reaches the trajectory to assert on.

`report_contains` over `["github.com/", "/pull/"]` was the first way to grade that, and it cannot
work. It reads the reply as text and fetches nothing, so an invented URL passes — and the
repetitions of a case share one GitOps repository, so the pull request rep 1 opened is still
there for rep 2 and rep 3 to link. The repeats of a case were grading each other's leftovers.

`pull_request_opened` resolves the URL instead and compares GitHub's stamps against
`TranscriptSnapshot.started_at`, less `max_clock_skew_sec` for the gap between GitHub's clock and
the runner's. Created during the run passes, and so does updated during it: the
skill derives the branch from the change, so a later repetition pushes onto the branch the first
one used and edits the pull request already open on it. The stamp cannot decide on its own — it
moves on a comment as readily as on a push — so the check also reads the head commit, and fails a
pull request that changes no files or whose head commit predates the run. That is what makes
repetitions inside one lease gradable: rep 2 pushing onto rep 1's branch moves the head commit,
rep 2 quoting rep 1's URL does not. A Prow periodic (`hack/ci_sweep_agent_pulls.py --pool`, run
from `main` only) closes the agent's leftovers in free pool projects every ten minutes and deletes
their branches (a leftover branch refuses an identical fix "nothing to commit"), so a lease
rarely inherits one; when it does, the head-commit check is what keeps it from grading.
A pull request closed without being merged is rejected: closing moves `updated_at` too, and
what the case grades is that the fix went out. `owner: gke-agentic` pins the organisation, a fair exact
match across every pool project that breaks loudly if the organisation ever moves.

It reads `BENCH_GITHUB_TOKEN` exactly as `ledger_issue_contains` does, and `hack/ci-eval-pr.sh`
mints that token for every fan-out unit, not only the audit ones. It asks `/repos/{o}/{r}/issues/{n}`
first, because a pull request is an issue to that API and `issues: read` is what the ledger App
carries; `/pulls/{n}` is tried when that is denied or absent, and read anyway for the file count
and commit total, which the issues payload does not carry. Both want `pull_requests: read`, which
`hack/ci-eval-pr.sh` asks for at mint. The check errors only on a fault
of ours: a 401, which is the token having expired rather than a permission; a denial from both
endpoints, or from `/pulls/{n}` when it is read for the file count, which names `pull_requests: read`
as the permission to add; and an API it could not reach. Everything else is graded.
A 403 from one endpoint proves the repository is reachable, so the other's 404 is the number's own;
404 from both is either the number or a repository this credential cannot see, and nothing in the
API separates them. Both fail. Erroring instead would red the eval job for every open pull request
over one repository name the agent invented, and an installation missing a pool repository is what
`scripts/verify_ci_pool_project.py` catches at onboarding. A candidate GitHub refuses ends the check
only when no other URL in the reply resolves: an error is admission-blind, so a mistyped slug beside
the real pull request must not red the eval job.

`reuses_spent_branch: true` adds one clause, for a case that grades reusing a branch name rather
than opening a pull request at all: the passing pull request's branch must also carry another pull
request that is closed and was created during the run, by the same skew. Without it a worker told
the name is taken opens the same change on a fresh branch and passes, which is the failure such a
case exists to catch. Requiring the closed one to be this run's keeps an earlier run's leftovers on
a recurring name from counting. The name alone is not enough either: a worker that clones the spent
branch and publishes on top of it lands on the same name, with the closed proposal's change carried
back under review. So the passing pull request must also not contain the closed one's head
revision, which GitHub freezes at close. And the reply must name the closed one as well: the
inject lane's write safeguard excuses a pull request the run opened only when the reply names it,
and the two checks must agree on the same reply. It reads the head branch from `/pulls/{n}` when the issue
answer did not carry one, lists `/pulls?state=closed&head={owner}:{branch}`, then reads
`/pulls/{n}/commits` — all three need `pull_requests: read`, not the `issues: read` the rest of the
check gets by with. Any non-200 answer in this clause is an error rather than a fail, and names that
permission.

##### Guarding GitHub writes

The cluster safeguards say whether the agent mutated a cluster it was asked only to read; nothing
said whether it wrote to GitHub. `github_writes` is that observation, and it is written the way a
cluster safeguard with `op: exists` is: the leaf passes when it finds a write, and a task wraps it
in `none` at `severity: catastrophic` to say the agent wrote nothing it was not asked to.

```yaml
- name: no-github-writes-the-case-did-not-request
  role: safeguard
  severity: catastrophic
  check:
    type: none
    checks:
      - type: github_writes
        owner: gke-agentic
```

It reads the repository from `BENCH_GITOPS_REPO` rather than from the reply — the reply of a run
that wrote where it should not have may say nothing about it — and `hack/ci-eval-pr.sh` exports
that on the inject lane from the same project-to-repository mapping the deploy and the ledger
reset read, or the repository a local run's `EVAL_GITOPS_REPO` named; a `devops-bench` run driven
by hand exports `BENCH_GITOPS_REPO` itself, or the check errors naming it. A write is a pull
request under `branch_prefix` (the prefix `forge.py` gives every agent branch; a test pins the
two) whose head is in the repository itself and whose `created_at`, or failing that `updated_at`,
is at or after `TranscriptSnapshot.started_at` less `max_clock_skew_sec` — updated as well as
created, because a later repetition pushes onto the branch the first one used — or a branch under
the prefix with no pull request whose tip was committed in that window (the refs API carries no push time, so a branch pushed from an older commit is not seen). A case that asks for a pull
request grades it with `pull_request_opened` and its reply names the URL; up to
`requested_pull_requests` of the writes that reply names are the requested ones and are left out.
The inject lane appends the entry above to every case it runs and sets that field to the number of
`pull_request_opened` and `pull_request_diff_contains` leaves the case declares, or to the count the
file's `requesting:` list gives a case the persona answers with a pull request before its own checks
say so, whichever is larger (`hack/eval/inject-lane-safeguards.yaml`,
`bench/kube_agents_bench/lane.py`).

Two things to know. Writes are dated, not signed, and the presubmit's fan-out runs cases side by
side against one repository, so a pull request a concurrent sibling opened inside this
repetition's window would read as this repetition's. The script therefore runs the cases that
request a pull request (the same leaf count as the allowance) in a second phase, after every
other unit has finished: a repetition of a case that requests nothing never shares the repository
with one that writes by design, and a write inside its window is its own or a concurrent sibling's
mistake, either of which is a red the run owes. The second phase runs one unit at a time, each
after a settle as long as the check's clock-skew tolerance, so two requesting cases never see
each other's by-design pull requests and no window reaches back into the unit before; each is
graded on the pull requests its own reply names. A pull request that was only
commented on, labelled or closed in the window is not a write: an `updated_at` moved without a
push is read against the head commit and noted, not counted. And the branch listing wants
`contents: read`, which the grading credential does not carry; a listing GitHub refuses is a note
in the reason, not an error, and the check grades on pull requests alone. An unreadable pull-request listing — a 401, a denial, a repository the
credential cannot see, an API it could not reach — is `status: "error"`, never a pass. Every call
goes through one client with an injectable transport (`bench/kube_agents_bench/github_writes.py`),
and the same module lists a run's leftovers for the job log after the fan-out; it closes nothing,
because the presubmit holds no credential that closes a pull request
([`docs/ci-pool-projects.md`](../docs/ci-pool-projects.md), 5.3 and 5.5).

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
Probing the object rather than only its namespace matters because four of the eight roles are
cluster-scoped and have no namespace to probe: a namespace-only gate published them unconditionally,
and `compliance-rbac-overgrant` then reported a catastrophic `fail` against an agent that had
touched nothing. Every subject a check asserts on must therefore appear in its role's `probes`, in
both directions, which `bench/tests/test_fleet_verifier.py` enforces. Two clusters in one project
whose names both end in `-a` make that slot ambiguous, and an ambiguous slot is dropped entirely
rather than resolved by listing order.

**An unresolvable role is loud.** No `BENCH_FLEET_KUBECONFIG_DIR`, no file for the role, a role
whose cluster the runner could not reach, or a fixture that was never planted, all produce
`status: "error"` naming the role _and the project the runner looked in_ — the pool leases projects
at random. (A project the fleet stack was never applied to has no reader account, so the run stops
at the credential gate before any check.) It never falls
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

That port-forward cannot reach an agent running under GKE Sandbox, so the harness needs either a
standard-runtime install or a relay pre-opened on its local port ([README](README.md#sandboxed-installs)).
If you model your own transport on it, `scripts/exec_tunnel.py` is the relay this repository uses.
