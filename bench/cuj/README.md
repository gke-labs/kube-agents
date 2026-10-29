# Critical User Journey tests

This directory contains live, black-box functional tests for complete user
journeys through the admin portal API or the Web Console HTTP API. Each test
talks to Kage as a user and scores only evidence returned by the deployed
system.

From the repository root, run every CUJ with pytest:

```bash
uv run --project bench pytest -s bench/cuj
```

To run the 10 read-only harness and WebUI quality/latency validation journeys
by standing up an ephemeral GKE cluster (`create` mode, which provisions the
cluster with `--enable-web-console`, connects over a private loopback
`kubectl port-forward`, and tears the cluster down via `uninstall.sh` when the
session finishes; requires sibling PR `#2321`, which adds `--enable-web-console`,
`webConsole.enabled`, `svc/kube-agents-web-console`, and the Web Console HTTP
API):

```bash
CUJ_WEB_CONSOLE_CLUSTER_MODE=create \
CUJ_PROJECT_ID=<gcp-project-id> \
CUJ_CLUSTER_LOCATION=us-central1 \
CUJ_MAX_LATENCY_SECONDS=180 \
uv run --project bench pytest -s bench/cuj/harness
```

To deploy the Web Console onto an existing GKE cluster (`existing` mode, which
enables `kube-agents-web-console` via the `webConsole.enabled` Helm chart
value from sibling PR `#2321`, connects over a loopback `port-forward` or a
private `ClusterIP` when `CUJ_WEB_CONSOLE_USE_PRIVATE_IP=true`, and tears the
unauthenticated Web Console down on exit unless deployed via a private IP):

```bash
CUJ_WEB_CONSOLE_CLUSTER_MODE=existing \
CUJ_PROJECT_ID=<gcp-project-id> \
CUJ_CLUSTER_NAME=<existing-cluster-name> \
CUJ_CLUSTER_LOCATION=us-central1 \
CUJ_MAX_LATENCY_SECONDS=180 \
uv run --project bench pytest -s bench/cuj/harness
```

To run against an already-forwarded loopback or private-IP Web Console URL
(public IP addresses are rejected by `validate_private_web_console_url` because
the Web Console has no authentication):

```bash
CUJ_WEB_CONSOLE_URL=http://127.0.0.1:18080 \
CUJ_PROJECT_ID=<gcp-project-id> \
CUJ_MAX_LATENCY_SECONDS=180 \
uv run --project bench pytest -s bench/cuj/harness
```

The suite targets the stock `platform-agent` resource created by
`install.sh`. Set `CUJ_PROJECT_ID` for scenarios that
need a Google Cloud project. `CUJ_TIMEOUT` and `CUJ_POLL_INTERVAL` are optional
overrides shared by both the admin portal path (`cuj.utils.scenario`) and the
Web Console HTTP API path (`cuj.harness.common`); `CUJ_PROFILE` applies to the
admin portal path, while `CUJ_WEB_CONSOLE_URL`, `CUJ_WEB_CONSOLE_CLUSTER_MODE`
(`create`, `existing`, or `port-forward`), `CUJ_WEB_CONSOLE_USE_PRIVATE_IP`,
`CUJ_KEEP_PRIVATE_WEB_CONSOLE`, `CUJ_TEARDOWN_WEB_CONSOLE`, and
`CUJ_MAX_LATENCY_SECONDS` apply specifically to `bench/cuj/harness/`
(`cuj.harness.common`).
Pytest reports every journey independently using its normal test discovery.
Before running a journey, `test_00_agent_responsive.py` requires the configured
agent to be discoverable and complete a minimal `READY` interaction through the
admin portal path (or, when running `bench/cuj/harness` with
`CUJ_WEB_CONSOLE_URL` or `CUJ_WEB_CONSOLE_CLUSTER_MODE` set,
`bench/cuj/harness/conftest.py` manages the Web Console lifecycle, runs the
`READY` preflight directly against the private Web Console HTTP API, and tears
down the Web Console or ephemeral cluster when the pytest session exits).
Each test starts an API-only portal on an OS-assigned loopback port (or, in
`bench/cuj/harness/`, talks directly to `CUJ_WEB_CONSOLE_URL` when configured)
and uses a unique interaction session, so parallel workers do not share ports or
sessions.
Each journey appends its request, each change in the portal interaction
projection (not every poll), acceptance criteria, milestones, and summary to
`interactions.jsonl` in a unique `/tmp/kube-agents-<scenario>-*` directory,
beside a readable `conversation.txt` shaped like the portal's Chat tab. Backend-independent acceptance
criteria determine the pytest result. Backend-specific milestones report
diagnostic progress but do not pass or fail the test. Both report the required
proof and observed evidence.

The `obtainability/` journeys (CUJ1, Day-0 cluster design; CUJ3, Day-2 batch
window planning; CUJ4 and CUJ5, the scheduled and reactive re-checks) are designed in
[`docs/designs/obtainability-journeys.md`](../../docs/designs/obtainability-journeys.md),
whose Scope table also says which of their evidence-scored criteria fail against
a stock install and why.

The `harness/` journeys (`test_01` through `test_10`, detailed in
[`harness/EVAL.md`](harness/EVAL.md)) verify read-only harness
self-inspection, response quality, and turn latency (`CUJ_MAX_LATENCY_SECONDS`,
default `180s`). As in `obtainability/`, each journey's `ac05-*-remains-read-only`
criterion requires complete normalized tool-call evidence (`toolEvidenceComplete=True`
and worker `toolCalls`); when run through the stock admin portal path or a
Web Console transcript that does not project worker `toolCalls`, `ac01`–`ac04`
score the live response while `ac05` records `FAILED` (`missing proof:
portal interaction projection omits toolEvidenceComplete` or `portal task
projection omits worker toolCalls`):

1. `test_01_identity_and_permission_posture.py` — Backing LLM model and cloud
   provider without leaking internal LiteLLM router aliases (`model-default` or
   `custom provider`), coordinator role, and plan-and-delegate permission
   posture.
2. `test_02_fleet_and_host_cluster_scope.py` — In-scope GCP project, visible
   GKE fleet clusters, and host cluster region.
3. `test_03_harness_namespace_and_crd_inventory.py` — `kubeagents-system`
   running pods/services, `*.kubeagents.x-k8s.io` CRDs, node readiness, and
   Kubernetes server version.
4. `test_04_gateway_diagnostics_and_events.py` — CrashLoopBackOff/restart
   audit, recent Warning events, and `platform-agent-gateway` pod status and
   logs (or RBAC boundary).
5. `test_05_namespace_inventory.py` — Active cluster namespaces (`default`,
   `kube-system`, `kubeagents-system`) and `Active` phase status.
6. `test_06_control_plane_and_node_capacity.py` — Kubernetes control plane
   version (`v1.x`), node count and `Ready` status, and allocatable CPU/memory
   capacity.
7. `test_07_kubeagents_service_endpoints.py` — `Service` endpoints, types
   (`ClusterIP`/`LoadBalancer`), and exposed ports in `kubeagents-system`.
8. `test_08_storage_and_pvc_health.py` — `PersistentVolumeClaim` (`Bound`) and
   `StorageClass` health in `kubeagents-system`.
9. `test_09_network_policy_posture.py` — `NetworkPolicy` inventory,
   `Ingress`/`Egress` policy types, and pod selector isolation in
   `kubeagents-system`.
10. `test_10_gitops_and_mutation_refusal.py` — Explicit refusal of direct
    destructive `kubectl delete pod` execution and explanation of the read-only
    / GitOps Pull Request governance boundary.

## Adding a journey

Add a normal pytest module at:

```text
bench/cuj/<area>/test_<NN>_<name>.py
```

Pytest discovers each journey without a registry or custom collector. A test
must:

- run from the repository root without relying on the caller's working
  directory;
- accept live configuration through environment variables;
- act through public interfaces rather than importing production internals;
- write interaction, acceptance, and milestone evidence beneath `/tmp`;
- use ordinary pytest assertions for configuration, transport, and acceptance
  failures.

Reuse acceptance reporting from `cuj.utils.acceptance_criteria`, configuration
from `cuj.utils.scenario`, evidence output from `cuj.utils.evidence`, portal
execution and projection helpers from `cuj.utils.interaction`, portal startup
from `cuj.utils.portal`, and dependency-aware diagnostic reporting from
`cuj.utils.milestones`. Keep only the prompt and scenario-specific acceptance
and milestone checks in the scenario, then expose it as an ordinary pytest
test:

```python
def test_02_example() -> None:
    Scenario(
        "cuj2",
        build_prompt,
        evaluate_acceptance,
        evaluate_backend_milestones,
    ).run_test()
```
