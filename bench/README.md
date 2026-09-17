# bench

Evaluation harness that runs [kubernetes-sigs/devops-bench](https://github.com/kubernetes-sigs/devops-bench) against the Platform Agent, with devops-bench consumed as a pip-installed library (pinned git SHA — no PyPI release yet) instead of the legacy evaluator baked into the eval image. Tasks and the agent transport live here, so kube-agents and devops-bench ship independently.

## Layout

- `kube_agents_bench/harness.py` — the `kubeagents` agent harness: establishes `kubectl port-forward` to `svc/platform-agent` when the local port is closed, POSTs the task prompt to `/v1/responses`, and waits out any work the agent delegates to a subagent. That transport needs either a standard-runtime install or a relay pre-opened on its local port — see [Sandboxed installs](#sandboxed-installs). `AGENT_TRANSPORT=a2a` swaps the HTTP door for a direct publish on the A2A bus, a diagnostic (see [Transports](#transports)). Environment variables are documented in the module docstring.
- `kube_agents_bench/a2a_transport.py` — the bus half of that second transport: envelopes, subjects, the event fold, and one submit-and-await exchange over `nats-py`.
- `kube_agents_bench/parsing.py` — pure payload and trajectory reading: maps a response onto devops-bench's canonical `AgentResult`, and reads back which kanban cards a turn filed, what statuses it reported, and what a finished card delivered.
- `kube_agents_bench/cuj.py` — black-box CUJ evaluator for the portal's shared
  `/api/v1` interaction contract. It waits for aggregate terminal state before
  producing assertions.
- `kube_agents_bench/verifiers.py` — the leaf verifiers this repository adds to devops-bench's own, published through the `devops_bench.verifiers` entry-point group.
- `kube_agents_bench/fleet.py` — resolves a seeded-fleet fixture ROLE to the kubeconfig that reaches it. Fails loudly rather than falling back to the ambient config; see [tf/fleet/README.md](tf/fleet/README.md).
- `kube_agents_bench/cases.py`, `scoring.py`, `baselines.py`, `gate.py` — the presubmit's verdict, described under [The gate](#the-gate) below. Nothing devops-bench calls; these read the records it writes.
- `tasks/` — task definitions. `agent-kanban-smoke` is a no-infrastructure smoke task that exercises the whole pipeline using only toolsets the deployed agent actually ships with. The rest are the Phase 2 domain scenarios; [`tasks/DRAFTS.md`](tasks/DRAFTS.md) is their status page.
- `baselines/` — screening evidence and `VERSIONS.json`, one append-only JSONL file per case, one batch of runs per line, each keyed on the five software versions a score depends on. Written by runs on `main`, read by every pull request. See [baselines/README.md](baselines/README.md).
- `scenarios/` — evaluation matrices using `Agent + Persona + Scenario + Goals
-> Run -> Assertions` terminology.
- `tests/` — offline tests: the harness against a local HTTP stub, and the gate against real run records captured from a live cluster (`tests/fixtures/runs/`).
- `tools/` — operator-run scripts that are neither tasks nor tests. `live_check_fleet_safeguards.py` drives every `fleet_resource_property` check in the cluster-debugging cases against a live cluster, through the real verifier, without running an agent.

To add a task or plug in a different agent, see
[CUSTOM-TASKS.md](CUSTOM-TASKS.md). To contribute a case to this repository — the format
it is held to, the fixture-sanitization rule, who owns it when it flakes, and how it earns a
seat on the merge-blocking roster — see [CONTRIBUTING.md](CONTRIBUTING.md).

**Domain coverage.** `docs/designs/domains.yaml` lists eleven domains and an `allowlist` of the ones known to be uncovered; `scripts/test_domain_coverage.py` fails the build both for an uncovered domain missing from that list and for a listed domain that is in fact covered, so the list cannot rot in either direction. A domain counts as covered only when a task carries its `domain:` slug **and** a non-empty `verification_spec` **and** is an entry in `hack/eval/presubmit-cases.txt` — covered means running on every pull request.

All eleven are covered and the allowlist is empty, which is Phase 2's exit criterion: `chat-and-routing` by the two kanban probes, `cluster-debugging` by `cluster-agent-crashloop-debug` (#939), `reliability`, `capacity`, `security`, `upgrades`, `consistency` and `cost` by the six domain probes, `fleet-audits` by the `compliance-rbac-overgrant` canary — the probe-plus-canary recast the 2026-08-26 smoke run forced, after it priced a full audit at 600–1300s ([`tasks/DRAFTS.md`](tasks/DRAFTS.md) has the run and the reasoning) — `remediation` by `rca-remediation-pr`, and `incident-triage` by `autoops-warning-event-triage`, which #1045 activated by giving it a scenario driver (`tf/prebuilt/autoops-incident`) that plants its own incident.

## Sandboxed installs

The harness reaches the agent with `kubectl port-forward`, which cannot reach a pod running under GKE Sandbox (gVisor) — the forward is set up in the host-side network namespace, and the listener lives in the sandbox's own. [`platformagent-crd.md`](../docs/site/src/content/docs/operator/platformagent-crd.md#specharness) is canonical on the constraint. `ENABLE_GVISOR` defaults to `true`, so a stock install is sandboxed.

The forward comes up anyway. `kubectl port-forward` binds its local port before anything dials the pod, so the port opens and the harness takes the tunnel for established; each request then dies inside the sandbox, and the harness treats that as it treats any dropped connection — three attempts with a tunnel reset between them, then a run recorded as infrastructure:

```
opening turn failed in transport (1/3): RemoteDisconnected: Remote end closed connection without response
```

What that leaves in `results.json` is an empty answer with `KUBE_AGENTS_INFRA_FAILURE` at the head of `errors`, which the judge grades as the agent's non-answer. ([The gate](#the-gate) reads the marker and calls the repetition infrastructure, but that is the presubmit, not a local run.) Nothing in it names the sandbox, and an agent pod that went away mid-run reports the same thing.

To run against a sandboxed install, pre-open the harness's local port with the `kubectl exec` relay:

```bash
python3 ../scripts/hermes-dashboard-tunnel.py \
  --container agent-api-auth --remote-port 8643 --local-port "${AGENT_LOCAL_PORT:-8642}"
```

Leave it running alongside the eval. `_ensure_port_forward` treats an already-open port as a no-op — it never assumes it owns the transport — so the harness rides the relay instead of spawning a forward that cannot work. The script is named for the dashboard, but the relay under it ([`scripts/exec_tunnel.py`](../scripts/exec_tunnel.py)) is general, and these flags point it at the credential-proxy sidecar that fronts the agent API.

The relay resolves the pod against whichever kubectl context is current, where the harness pins `AGENT_CLUSTER_CONTEXT` on every call, so point the current context at the agent's cluster before starting it. A task that provisions a cluster then repoints that context under you — `gcloud container clusters get-credentials`, the hazard `AGENT_CLUSTER_CONTEXT` exists for — and since the relay opens a `kubectl exec` per connection, every request from there on lands on the task cluster and fails the way the sandbox does. Give those tasks an unsandboxed agent.

Above one replica, pass `--pod` too. A pod that does not hold the leader lease never starts the gateway, and the operator reports it Ready on purpose — its probe counts a refused connection as healthy while leader election is on — so the relay, which takes the first Ready pod matching the label, can settle on one with nothing listening.

To run the agent unsandboxed: `ENABLE_GVISOR=false` at install time, or in `install.env` before a full `upgrade.sh`, which re-renders the runtime class from it — read [`scripts/installer/README.md`](../scripts/installer/README.md) on what else that apply rewrites before you run it on an install you care about. CI reaches the same place by overriding the chart value directly, which a throwaway cluster can afford: `hack/ci-deploy.sh` pins `runtimeClassName` empty before `hack/ci-eval-pr.sh` runs, and says why.

## Running evals

```bash
cd bench
uv sync
export PROJECT_ID=<gcp project> CLUSTER_NAME=<cluster> AGENT_CLUSTER_CONTEXT=<kubectl context>
export BENCH_TF_ROOT=./tf
PLATFORM_AGENT_TOKEN=$(kubectl get secret platform-agent-secrets -n <namespace> \
  -o jsonpath='{.data.API_SERVER_KEY}' | base64 --decode) \
  JUDGE_PROVIDER=<provider> JUDGE_MODEL=<model> \
  uv run devops-bench ./tasks/<id> --agent-type kubeagents
```

This is the stock `devops-bench` CLI — there is no wrapper command. `source` is positional, and `./tasks` runs every case. The exports are what `hack/ci-eval-pr.sh` sets, so a local run grades the way the presubmit does; a case with `fixtures:` also needs `BENCH_FLEET_KUBECONFIG_DIR` from `hack/fleet-kubeconfigs.sh`. `--no-infra` smokes the agent path only: it skips the deterministic checks, so such a run can neither pass nor fail the gate. [`.agents/rules/eval_driven_development.md`](../.agents/rules/eval_driven_development.md) is the loop that uses this. See `--help` for the rest.

## Transports

The harness reaches the agent through one of two doors, selected by `AGENT_TRANSPORT`. Everything else about a run is the same: the same `KubeAgentsHarness`, the same `AgentResult`, the same transcript stash the verifiers read.

- `api` (the default): `kubectl port-forward` to `svc/platform-agent` and `POST /v1/responses`, followed by status turns on the same conversation while delegated cards settle. This door is identical under `spec.mode: today` and `spec.mode: next`, so a run through it says nothing about the next stack.
- `a2a`: a diagnostic. The prompt is published straight onto the bus as one `message` envelope on `a2a.tasks.<addressee>.<taskId>.in`, and the task's `…events` subject is folded until the terminal status lands. It proves the bus, the `TASKS` stream and the executor, not the auth callout: the `eval` principal and the bridge's `worker` are both static users listed in `auth_users`, so the callout is never on this path and a green run says nothing about it. It skips the gateway: its routing, its session registry, the relay back. Use it to show that the executor answers when the gateway is the thing that is down. It is not the transport next-mode evals will run on; that is the gateway's inject adapter, planned and not yet built, which goes through the gateway's inbound path and keeps the only credential that may publish on every addressee's `in` subject. Until it lands, no transport runs next-mode evals.

The a2a door port-forwards the operator's NATS Service (`<cr>-a2a-nats`, client port 4222) and connects as the operator's `eval` principal, whose password is the `eval-password` key of `<cr>-a2a-nats-creds`. That principal may publish on `platform`'s `in` subject and subscribe on `platform`'s `events` and `supervisor` (the pair a task's terminal may land on: the executor's own, or the one its supervisor writes if the executor dies without one), and nothing else: no JetStream API. The harness never reads the gateway's key. Every envelope it publishes carries `authority: null`; the block is the gateway's to populate and its shape is advisory, so the harness asserts nothing there. Because the principal has no JetStream grant, the subscriptions are core subscriptions taken before the publish with no replay, and a connection that drops before the terminal ends the attempt: the harness resubmits as a new task, up to three attempts, then reports infrastructure. Every task an attempt submitted and lost gets a cancel, from the next attempt or on the way out when there is none, and its id is recorded on the run (`abandoned_tasks`), the infrastructure record included. The await is a function of a task id (`BusClient.await_terminal`), so the same code will await a child task once a parent's events name one.

The addressee defaults to `platform`, which today only the Hermes bridge sidecar answers; a run against an install with no executor ends as infrastructure (nothing accepted the task within `AGENT_A2A_ACCEPT_TIMEOUT`), never as a graded answer. A task an executor took and ended `failed` is graded. The variables are in the harness module docstring.

What each transport gives the verifiers:

| Verifier                                          | `api`                       | `a2a`                                                                                                                                                             |
| ------------------------------------------------- | --------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `report_contains`, `fleet_resource_property`      | yes                         | yes: the `result` artifact's text is the report                                                                                                                   |
| `tool_called`                                     | the delegating turn's calls | only once the executor publishes an `activity` artifact; the bridge does not, so the trajectory holds the task's lifecycle events and the check counts zero calls |
| `worker_commands`                                 | the delegated cards' logs   | same read, but the card ids come from the trajectory, so none are found until `activity` artifacts carry them                                                     |
| `ledger_issue_contains`                           | yes                         | yes                                                                                                                                                               |
| token accounting (`tokens.*`, reported not gated) | the session row             | none: the bus reports no usage, every bucket is null, and `bench-gate` takes the terminal event as the liveness signal instead                                    |

Delegation under the bridge is still a kanban card, and the bridge's terminal means the turn ended, not the work. The kanban poll for delegated cases therefore runs in the harness after the transport returns (`_a2a_delegation_wait`), asking over the bus as a follow-up task on the same context; the transport itself never polls the model. With no card ids in the trajectory the wait settles at once. When agent-initiated delegation becomes a child task on the bus, the poll is deleted and the child's task id is awaited instead.

## The gate

devops-bench scores a run; it does not decide whether a pull request may merge. That decision is `bench-gate`, and `hack/ci-eval-pr.sh` is what drives it (when its step-0 revalidation has not already reused the pull request's prior green verdict): the shell runs each task `EVAL_REPETITIONS` times (default 3) and hands the resulting run **directories** to the scorer, which reads `results.json` for the scores, `manifest.json` for `setupId`, and `rows.json` for `scoringVersion`.

```bash
uv run bench-gate case --task ./tasks/<id>/task.yaml \
  --result <run-dir> --result MISSING --result <run-dir> \
  --json-out case-<id>.json          # exit 0 with a verdict, 2 if it could not grade
uv run bench-gate suite --case-result case-<id>.json --markdown-out verdict.md
                                     # exit 0 green, 1 red
uv run bench-gate record --case-result case-<id>.json   # main only; appends evidence
```

The gate is rate-based, not all-must-pass: at a few hundred cases and realistic per-case reliability, "every case green" is a state the suite reaches on a vanishing fraction of runs, and a gate that reds most pull requests gets switched off. So a case is graded on a ladder — a forbidden action, a declared check that never ran, or a record whose liveness signals are inconsistent reds the job outright (a record showing no run at all — empty trajectory, zero billed tokens — is excluded as infrastructure instead, #1184); a case that merely _fails_ reds it only by failing **every** repetition, and only once the baseline store holds screening evidence that the case is reliable enough to mean something.

That evidence is what `baselines/` holds, and admission is computed from it rather than declared in `task.yaml` — a case cannot admit itself in the same diff that makes it pass. A case with no record at the current version key is reported unadmitted, and one whose record was measured on different software is reported _stale_ rather than silently compared against.

**The loop closes through `record`.** Everything a pull request is compared against comes from lines that a run on `main` appended, so the store fills itself: each nightly run appends its own repetitions, the reader pools the newest lines at a key until it holds 20 runs, and a case is admitted once that pooled evidence clears the bar — seven nights from empty at `EVAL_REPETITIONS=3`, the default the nightly inherits (the pool takes whole lines, so it lands on 21). What that window decides is `EVAL_ADMISSION_MODE`: under `roster`, the default, `BOOTSTRAP_ADMITTED` decides what blocks and the verdict's **Record says** column reports what the record would do (`would-admit`, `would-demote`, `collecting`, `stale`) beside the **Admitted by** column; under `record`, the record decides either way once it holds a full window, and a case that starts failing pushes its own passing history out and stops being able to red the job. `record` refuses to run with `PULL_NUMBER` set, and the shell only calls it on a run whose `JOB_TYPE` is `periodic` or `postsubmit`. Where those lines land is `EVAL_BASELINE_STORE`: unset, they append to `baselines/` in the checkout, which is hermetic and needs no credential but has no way to commit itself from CI; set to `gs://bucket/prefix`, each batch becomes one immutable object under a `roles/storage.objectCreator` grant that cannot overwrite or delete, which is what actually closes the loop on `main`. `VERSIONS.json` stays in git either way. See [docs/designs/eval-scorer.md](../docs/designs/eval-scorer.md).

Two speeds, deliberately: the deterministic `Verification*` scores decide whether a repetition passed, and no judged score can fail a repetition on its own. The captured fixtures are the argument — three byte-identical failing runs scored `OutcomeValidity` 0.9, 1.0 and 0.2 while `VerificationCorrectness` held at 0.5 on all three. The judge is given exactly one job (rung 6): catching a **collapse** in judged quality against main's mean at the same version key, at a margin of two standard errors of that measured spread. At three repetitions it cannot see drift, and widening it is a matter of more repetitions or a less variable metric, not a smaller number.

Thresholds are named environment variables, all with the documented default: `EVAL_REPETITIONS`, `DETERMINISTIC_CORRECTNESS_FLOOR`, `EVAL_ADMISSION_RATE`, `EVAL_ADMISSION_MIN_RUNS`, `EVAL_AGGREGATE_MARGIN`, `EVAL_AGGREGATE_MIN_SCORED`, `EVAL_AGGREGATE_ARMED` (unset by default: the suite aggregate is computed and reported against main's rate but cannot red the job until this is set to `1`, `true` or `yes`), `EVAL_JUDGED_MARGIN`, `EVAL_JUDGED_METRICS`, `EVAL_BASELINE_STORE`, `EVAL_BASELINE_MAX_OBJECTS`, `EVAL_ADMISSION_MODE` (`roster` by default: the list decides and the record is reported; `record`: the record decides once it holds a full window) and `BOOTSTRAP_ADMITTED` (the blocking roster, read from `hack/eval/blocking-roster.txt`; [`docs/eval-gate-roster.md`](../docs/eval-gate-roster.md) has the decision and what the record must show before the mode changes).

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

No cluster or `kubectl` required — the suite drives the full request → parse → `AgentResult` path against a local stub, and grades the gate against run records captured from a live cluster and then mutated. Every gate failure mode is a mutation of a real record rather than a hand-written dict, so a test cannot agree with the scorer about a field devops-bench does not actually emit; `tests/fixtures/runs/README.md` records where the captures came from.
