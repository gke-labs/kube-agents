# bench

Evaluation harness that runs [kubernetes-sigs/devops-bench](https://github.com/kubernetes-sigs/devops-bench) against the Platform Agent, with devops-bench consumed as a pip-installed library (pinned git SHA — no PyPI release yet) instead of the legacy evaluator baked into the eval image. Tasks and the agent transport live here, so kube-agents and devops-bench ship independently.

## Layout

- `kube_agents_bench/harness.py` — the `kubeagents` agent harness: establishes `kubectl port-forward` to `svc/platform-agent` when the local port is closed, POSTs the task prompt to `/v1/responses`, and waits out any work the agent delegates to a subagent. Environment variables are documented in the module docstring.
- `kube_agents_bench/parsing.py` — pure payload and trajectory reading: maps a response onto devops-bench's canonical `AgentResult`, and reads back which kanban cards a turn filed, what statuses it reported, and what a finished card delivered.
- `kube_agents_bench/cuj.py` — black-box CUJ evaluator for the portal's shared
  `/api/v1` interaction contract. It waits for aggregate terminal state before
  producing assertions.
- `kube_agents_bench/verifiers.py` — the leaf verifiers this repository adds to devops-bench's own, published through the `devops_bench.verifiers` entry-point group.
- `kube_agents_bench/fleet.py` — resolves a seeded-fleet fixture ROLE to the kubeconfig that reaches it. Fails loudly rather than falling back to the ambient config; see [tf/fleet/README.md](tf/fleet/README.md).
- `tasks/` — task definitions. `agent-kanban-smoke` is a no-infrastructure smoke task that exercises the whole pipeline using only toolsets the deployed agent actually ships with. The rest are the Phase 2 domain scenarios; [`tasks/DRAFTS.md`](tasks/DRAFTS.md) is their status page.
- `scenarios/` — evaluation matrices using `Agent + Persona + Scenario + Goals
-> Run -> Assertions` terminology.
- `tests/` — offline tests against a local HTTP stub.

To add a task or plug in a different agent, see
[CUSTOM-TASKS.md](CUSTOM-TASKS.md).

**Domain coverage.** `docs/designs/domains.yaml` lists eleven domains and an `allowlist` of the ones known to be uncovered; `scripts/test_domain_coverage.py` fails the build both for an uncovered domain missing from that list and for a listed domain that is in fact covered, so the list cannot rot in either direction. A domain counts as covered only when a task carries its `domain:` slug **and** a non-empty `verification_spec` **and** is an **uncommented** entry in `hack/ci-eval-pr.sh`'s `TASKS` array — covered means running.

Nine of the eleven are covered: `chat-and-routing` by the two kanban probes, `cluster-debugging` by `cluster-agent-crashloop-debug` (#939), `reliability`, `capacity`, `security`, `upgrades`, `consistency` and `cost` by the six domain probes, and `fleet-audits` by the `compliance-rbac-overgrant` canary — the probe-plus-canary recast the 2026-08-26 smoke run forced, after it priced a full audit at 600–1300s ([`tasks/DRAFTS.md`](tasks/DRAFTS.md) has the run and the reasoning). Two remain allowlisted — `remediation` (`rca-remediation-pr` is registered but parked until it gets one clean measured run; the 2026-08-26 job deadline expired before reaching it) and `incident-triage` (its scenario has no driver to apply the incident workload; #954). Phase 2's exit criterion is an empty allowlist.

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

## Portal CUJ evaluations

The portal evaluator is the black-box path for conversational CUJs with
asynchronous work. It creates an interaction, observes approvals according to
the Persona, waits until the root run and delegated tasks are terminal, and only
then evaluates Goals. It does not modify kube-agents to signal test completion.

The matrix terms are:

- **Agent** — portal API endpoint, black-box agent ID, and profile.
- **Persona** — the complete user role, actor identity/credential reference,
  description, and approval policy.
- **Scenario** — prompt, timeout, polling policy, and ordered Goals.
- **Tool Goal** — requires trusted `toolCalls` evidence. Response prose or a
  promise to act cannot pass it.
- **Message Goal** — required/forbidden response signals plus an optional
  semantic rubric.
- **Soft Goal** — quality rubric with deterministic limits and an injected
  semantic judge. Without a judge its assertion is inconclusive, never passed.
- **Run** — one observed conversation and terminal interaction projection.
- **Assertion** — pass, fail, or inconclusive evidence for completion or one
  Goal, including repair diagnostics.

When a Persona's credential reference resolves to a token, its Agent endpoint
must use HTTPS, except on a loopback host (`127.0.0.1`, `::1`, `localhost`),
where the token never leaves the machine. The evaluator rejects redirects
instead of forwarding the credential; configure the Agent with the final
canonical API URL.

Run the checked-in read-only smoke matrix against a locally running portal.
Every portal `/api/v1` request requires the portal's launch capability, so set
`KUBE_AGENTS_PORTAL_API_TOKEN` (at least 32 characters) before starting
`scripts/admin_portal.sh` — otherwise the portal generates a random token the
evaluator cannot know — and run the matrix with the same value:

```bash
cd bench
KUBE_AGENTS_PORTAL_API_URL=http://127.0.0.1:8501/api/v1 \
KUBE_AGENTS_PORTAL_API_TOKEN=<the portal's token> \
EXPECTED_PROJECT=<project> \
EXPECTED_CLUSTER=<cluster> \
EXPECTED_LOCATION=<location> \
uv run python -m kube_agents_bench.cuj scenarios/portal-readonly-smoke.json
```

The command prints the real user and assistant messages plus the complete
interaction and assertions as JSON. Exit status is zero only when the
interaction completed and every Goal passed. Portal coverage exercises the Chat
Agent front door and its delegation chain; Google Chat Pub/Sub and Slack ingress
remain separate transport Scenarios.

`hack/ci-eval-pr.sh` exports `PLATFORM_AGENT_TOKEN` for you in CI. The harness also honours the same `AGENT_*` variables as the legacy runner.

Tasks that provision infrastructure name their OpenTofu stack relative to `BENCH_TF_ROOT`; point it at a stack directory in this repo so the eval never depends on stacks bundled with the library:

```bash
AGENT_CLUSTER_CONTEXT=gke_<project>_<location>_<agent-cluster> \
  PROJECT_ID=<project> CLUSTER_NAME=<task-cluster> \
  BENCH_TF_ROOT=./tf uv run devops-bench ./tasks --agent-type kubeagents
```

`PROJECT_ID` and `CLUSTER_NAME` are required once infrastructure is on; without them the run exits before provisioning. Set `AGENT_CLUSTER_CONTEXT` for these too. Bringing up a task cluster — provisioned per run, or an existing one reused via a stack's `reuse_existing_cluster` — runs `gcloud container clusters get-credentials`, which repoints kubectl's current context at it; without the pin, the harness port-forwards into the task cluster, where the agent does not run.

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
