# The Credential Broker Scales Out: an Exec Tier Beside One Stateful Broker

> **STATUS — proposal; not implemented.** The first measurement run is in §8.3. 16 and 32 clusters and the exec tier are not measured yet.
> Nothing is built. This document proposes how the credential broker grows past one pod. It is
> meant to replace [`credential-proxy-single-pod.md`](credential-proxy-single-pod.md) once the
> measurements in §8 exist and the design is reviewed. Issue: #4082. Partner: the sizing tiger
> team (#3753).

**Scope:** how many broker pods an install runs, which routes run where, how callers reach them,
how admission orders interactive and background work, and the contract a fleet cache must meet
to take reads off the broker.
**Owns:** the exec tier, the stateful broker's remaining role, priority admission, the cache
contract, and the recommendation on per-profile or per-project brokers.
**Does not own:** the child memory budget and slot cap of one pod
([`credential-proxy-child-memory-budget.md`](credential-proxy-child-memory-budget.md)); the
fleet cache itself; sweep width (#4081, #4084); per-task worker credentials (#3889).

Number labels used in this document:

- **[measured]**: from a run, with its source.
- **[code]**: read from `main` (file and line).
- **[predicted]**: arithmetic from a stated assumption. It is not a measurement.
- **[doc, file]**: read from another design document, not from code.
- **[#3760 head]**: read from the head of #3760.

## 1. The shape in one picture

**The one change:** commands (`kubectl`, `gcloud`, the `/v1/exec` route) move out of the single
broker pod into a new exec tier that can run N copies, and so does the Cloud API relay
(`/v1/gcp/`), which keeps no state (§3.1). Everything else stays on today's pod.

Both pictures use the same layout. Callers are on the left; the broker pods are on the right.

```
TODAY: one pod does everything

  commands (all callers)  ───────────┐
    interactive shell                │
    session pods                     │      ┌───────────────────────────────────┐
    sweeps, reconcile, stall watch   ├────► │  credential broker   × 1 pod      │
                                     │      │   exec: 8 slots, one FIFO queue   │
  chat relays, workspace,  ──────────┘      │   relays, workspace, vcs/forge,   │
  vcs/forge, watchers                       │   watchers                        │
                                            └───────────────────────────────────┘
                                              ▲ the 8 slots and 1 CPU are shared by
                                                every caller, with no priority


PROPOSED: commands get their own tier

  commands (all callers)  ───────────┐      ┌───────────────────────────────────┐
    interactive shell   [interactive]│      │  NEW  exec tier   × N pods        │
    session pods        [interactive]├────► │   exec: 8 slots per pod; /v1/gcp/ │
    sweeps, reconcile,  [background] │      │   background ≤ admitted − 2       │
    stall watch                      │      └───────────────────────────────────┘
                                                 via NEW CREDENTIAL_PROXY_EXEC_URL

  chat relays, workspace,  ─────────────────► ┌───────────────────────────────────┐
  vcs/forge, watchers                         │  credential broker   × 1 pod      │
                                              │   relays, workspace, vcs/forge,   │
                                              │   watchers; /v1/exec at 0 replicas│
                                              └───────────────────────────────────┘
                                                 via CREDENTIAL_PROXY_URL (unchanged)
```

What changes, side by side:

|                                             | Today                                        | Proposed                                                                                                                                                      |
| ------------------------------------------- | -------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Who runs commands                           | The one broker pod                           | **NEW:** the exec tier, N pods                                                                                                                                |
| Command capacity                            | 8 slots in total                             | **8 slots × N**                                                                                                                                               |
| Priority                                    | None: one first-in, first-out queue          | **NEW:** 2 classes the broker sets from the caller's role (§4.1); the shell role counts as interactive until the turn token exists; background ≤ admitted − 2 |
| Chat relays, workspace, vcs/forge, watchers | The one broker pod                           | Same pod, unchanged                                                                                                                                           |
| Cloud API relay (`/v1/gcp/`)                | The one broker pod                           | **NEW:** the exec tier, with the commands; the stateful pod keeps it while 0 replicas is legal (§3.1, §9)                                                     |
| How callers reach commands                  | `CREDENTIAL_PROXY_URL`                       | **NEW:** `CREDENTIAL_PROXY_EXEC_URL`; if it is not set, as today                                                                                              |
| Upgrade of the command path                 | `Recreate`: commands stop during the restart | `RollingUpdate` with a long drain, a PDB at N ≥ 2 (§9 step 0): no outage at N ≥ 2; the stateful pod keeps `Recreate` (30 s drain)                             |
| Identity and permissions                    | One KSA / GSA                                | Same KSA / GSA, no new IAM                                                                                                                                    |
| Code                                        | `credential_proxy.py`                        | Same file and image, with `CREDENTIAL_PROXY_ROLE=exec` on the new pods                                                                                        |

The shell-role classes in the picture apply only once the turn token exists (§4.1, open question
9). Until then, shell-role requests count as interactive (§12).

Later, with a fleet cache (§6, contract only in this round): fleet-wide sweeps read the cache
first, and fall back to the exec tier when the cache is stale or missing.

The rule in one line: **the relays and the content workspace stay on one pod, exec scales out,
and each caller routes to the right one.**

## 2. The current shape and its limits

### 2.1 What one pod admits [code]

- Slot cap: `DEFAULT_MAX_CONCURRENT_COMMANDS = 8` (`credential_proxy.py:261`). The operator pins
  it, and a CR cannot override it (`platformagent_manifests.go:82`).
- Child memory budget: (limit − 320 MiB) / 176 MiB per request. That gives 9 at the 2 GiB default
  and 4 on Autopilot without bursting (limit clamped to the 1 GiB request). The smaller of the
  two numbers binds.
- Session role: its own cap of 2, refused at once and not queued (`credential_proxy.py:322`).
- Admission is one arrival-order queue with a 60 s timeout, then `503 CREDENTIAL_PROXY_BUSY`
  (`_admit`, `credential_proxy.py:6354`).
- **No priority.** A caller authenticates with a projected ServiceAccount token. The broker
  learns the workload and a role (shell, chat, a2a-chat, session, watcher, mcp) from it. The
  `caller` field of `Principal` is always `None` (`credential_proxy.py:2182`). Background work
  reaches the broker as the shell role, the same as an interactive command:
  - the fleet audits are agent cron jobs (`agents/platform/cron/jobs.json`, skill `fleet-audit`).
    The model runs their collectors in the sandbox shell, and they call `kubectl` and `gcloud`
    through the shim and the Cloud API relay through `ApiSession`;
  - the no-agent watches (stall watch, service-account role watch, upgrade readiness watch) and
    the hourly reconcile run through `sandbox_exec` and then the shim. `sandbox_exec` also runs
    the platform MCP server's interactive tools (`platform_mcp_server.py:590`).

  **The broker cannot tell interactive work from background work today.**

### 2.2 What blocks more replicas [doc, `credential-proxy-single-pod.md` §2 and §3]

| Route             | State                                                             | Two replicas behind one Service                                                                |
| ----------------- | ----------------------------------------------------------------- | ---------------------------------------------------------------------------------------------- |
| Exec (`/v1/exec`) | Caches only: managed kubeconfigs, gcloud config                   | [measured] Works: 24 sandbox calls succeeded; all shell exec traffic in the window split 69/51 |
| Google Chat relay | `receipt → ack_id` in the process                                 | [measured] Ack reached the wrong pod about half the time                                       |
| Slack relay       | One Socket Mode connection per pod, in-process queue and receipts | Not run; breaks by construction (§2 there)                                                     |
| Content workspace | Handles in the pod's emptyDir                                     | Not run; breaks by construction (`NoSuchHandle`)                                               |

### 2.3 What the load looks like [measured, #3935 Phase 1]

Source: `bench/scale/harness-limits.json` on #3935, 46 runs, `spec.mode: next`, image
`eeb8a95c`, Standard install, broker at 1 CPU and 2 GiB, 2 to 8 test clusters.

- **CPU was the first limit.** Broker CPU peaked at 1.05 cores (onboarding, 8 clusters), 1.06
  and 0.94 (upgrade readiness, 8 clusters) and 0.90 (fan-out, 8 clusters), against a 1-core
  limit. The broker's memory was at most 712 MiB of 2 GiB.
- **Slot waits were rare.** One run of 46 had an admission wait above 0.1 s: onboarding at 8
  clusters, 10 to 15 s (a histogram bucket). The upgrade readiness run at 1.06 cores had no wait.
  No run had a refusal.
- **The sweep load is not measured.** The fleet audits at 8 clusters cancelled or were declined
  (#3933, and no GitOps repository), and broker CPU in those runs was 0.01.
- **Interactive bursts hit the agent first.** With 15 questions at once, the agent's 10 run slots
  refused 5 (#3724), while the broker wait stayed at 0.1 s or less.

Phases 2 and 3 (#4107) measured the same install on 2 to 22
test clusters, with the broker's limits raised to 4 CPU and 4 GiB at 2 and 16 clusters. As that
pull request reports them [measured, #4107]:

- **CPU.** The broker is the first bottleneck. With the four first-run audits running together it
  sits at its 1-core limit at every fleet size. With the limit raised it needed 1.6 cores at 2
  clusters and 2.5 at 16.
- **Waits.** Commands waited 30 to 60 s at 16 clusters (audits together) and at 22 clusters
  (onboarding), next to the 60 s refusal bound. No command was refused yet.
- **Memory.** 1.5 GiB at 16 clusters and 1.8 GiB at 22, against the 2 GiB limit.
- **The command path, measured before #4104.** With no model call, workers in the sandbox ran
  `kubectl get pods --all-namespaces` through the shim. Throughput stopped at about 1.5 commands
  per second from 4 workers, with the sandbox shell at its 2 cores and the broker near 0.8 cores
  with a median wait of 0.1 s. Each command cost 0.8 to 1 s of shell CPU in the shim's Python
  client (#4027). So past a few commands at once, the caller side limited the path before the
  broker did. #4104 has since fixed #4027 on `main`; §8.3 has the run after the fix.

§8 uses #4107's runner features (`proxy_load`, raised limits).

So the single-pod doc's reopen condition (§6 there: reopen when either criterion holds) is met:
one pod's CPU saturates at 8 test clusters. The other criterion, a median wait over 10 s with
refusals, was not seen. The product target
is 1,000 to 2,500 clusters. One pod cannot get there vertically: the slot cap is fixed at 8, and
one pod's CPU is one limit.

## 3. The exec tier

### 3.1 Which routes move

| Route                                                       | Where                                                         | Why                                                                           |
| ----------------------------------------------------------- | ------------------------------------------------------------- | ----------------------------------------------------------------------------- |
| `POST /v1/exec`                                             | Exec tier (and stateful broker while 0 replicas is legal, §9) | No state across calls (§11, question 1, closed); most of the slot-taking load |
| `GET /v1/exec/admission` (#4084)                            | Exec tier                                                     | Reports the tier's capacity (§3.5)                                            |
| `/v1/gcp/` Cloud API relay                                  | Exec tier (and stateful broker while 0 replicas is legal, §9) | No state across calls; #4199 moved the reconcile's GKE cluster reads onto it  |
| `/v1/vcs/<verb>`, `/v1/forge/refresh`, `/v1/github/refresh` | Stateful broker                                               | Keeps one forge-token coalesce cache, so GitHub mints do not multiply by N    |
| `/v1/workspace/*`                                           | Stateful broker                                               | **Needs one pod:** handles live in one in-memory store and one emptyDir       |
| `/v1/chat/*` relays, `/v1/chat/a2a/*` included              | Stateful broker                                               | **Needs one pod:** in-process receipt maps, one Socket Mode connection        |
| `/v1/exec` for the `scope` role (#4119)                     | Stateful broker                                               | **Needs one pod:** a 2-hour in-memory member window                           |
| `/v1/mcp/*`, `/v1/pubsub/*`                                 | Stateful broker (first step)                                  | Safe on N pods: no state across calls                                         |
| `/v1/watcher/*`, `/v1/k8s/*`, `/v1/feedback/submit`         | Stateful broker (first step)                                  | Safe on N pods: `/v1/k8s/*` keeps only a 300 s endpoint cache                 |

Three route groups need the single pod: the content-workspace handles, the chat relays' event,
ack and nack routes, and the `scope` role's `/v1/exec` calls (#4119), whose member window lives in
one process. The rest stay on it as a first step, not because they keep state:

- **The A2A Google Chat relay stays a broker route in `next` mode.** With Chat on, the install's
  subscription goes to the broker's A2A relay instance, and the legacy instance is not built
  (`platformagent_manifests.go:4587-4597`). The consumer moved to the A2A gateway; the Pub/Sub pull
  and its receipt map did not. So the stateful pod stays in both modes.
- **Slack leaves the broker only in `next` mode with Slack on and Chat off**
  (`a2aSlackArmed`, `platformagent_a2a_manifests.go:1398-1399`). With both on, Slack stays on the
  broker's relay.
- **The watchers keep no broker state.** The event watcher and the drift detector run in the
  gateway pod's `agent-api-auth` sidecar (`platformagent_manifests.go:4154-4161`), and the gateway
  is capped at 1 replica (`supportedGatewayReplicas`, `manifest_helpers.go:84`). The broker only
  relays their calls; `/v1/k8s/*` keeps a 300 s endpoint cache (`WATCHER_ENDPOINT_TTL_S`,
  `credential_proxy.py:466`). `/v1/mcp/*`, `/v1/pubsub/*` and `/v1/feedback/submit` keep no state
  across calls either.

The stateful broker keeps a slot cap and an admission queue: `/v1/vcs/*` and
`/v1/feedback/submit` take slots there. The Cloud API relay, which the fleet audits and (since
#4199) the reconcile load, moves to the exec tier. So "most of the load moves" is a claim for §8 to
check, because CPU, not slots, was the first limit (§2.3, open question 4).

The exec tier runs the same image and the same `credential_proxy.py`. `CREDENTIAL_PROXY_ROLE`
exists today (`combined`, `broker`, `api-proxy`; `resolve_role()` in `credential_proxy.py`), and
the stateful pod runs `broker`. Today the value selects which processes start. The design adds
`exec`, which serves only the exec routes that §12 lists and answers 404 to the rest. This keeps one code
base and makes a wrong route fail loudly.

### 3.2 How each caller routes

| Caller                                          | Today                                                                                          | Proposed                                                                                           |
| ----------------------------------------------- | ---------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------- |
| Sandbox shim, `execute()`                       | `CREDENTIAL_PROXY_URL`                                                                         | `CREDENTIAL_PROXY_EXEC_URL` when set, else `CREDENTIAL_PROXY_URL`                                  |
| Sandbox shim, `Workspace`, vcs, forge           | `CREDENTIAL_PROXY_URL`                                                                         | Unchanged                                                                                          |
| Session pods (under the cluster-view flag only) | `CREDENTIAL_PROXY_URL`, from the gateway's `A2A_CREDENTIAL_PROXY_URL` (`a2a/gateway/spawn.go`) | Also get `CREDENTIAL_PROXY_EXEC_URL`, through a new `A2A_CREDENTIAL_PROXY_EXEC_URL` on the gateway |
| Fleet audits (agent cron jobs)                  | The model's shell, then the shim                                                               | Same path; background by default once the turn token exists (§4.1); until then, interactive (§12)  |
| Watches, reconcile                              | Through `sandbox_exec` and the shim                                                            | Same path; background by default once the turn token exists (§4.1); until then, interactive (§12)  |
| Cloud API relay callers (`ApiSession`)          | `CREDENTIAL_PROXY_URL`                                                                         | `CREDENTIAL_PROXY_EXEC_URL` when set, else `CREDENTIAL_PROXY_URL`                                  |
| Gateway relays, watchers                        | Their own URL variables                                                                        | Unchanged                                                                                          |

The fallback means an exec tier that is off, or a new shim against an old operator, behaves as
today. The shim also falls back when it cannot connect to the exec URL (no TCP connection, for
example a Service with no endpoints). No request reached a broker then, so nothing ran, by the
same reason as §4.3's retry. A failed connect can take the shim's whole connect bound, 10 s
(`BROKER_CONNECT_TIMEOUT_SECONDS`): a missing egress peer hangs the dial rather than refusing it.
So the shim writes one stderr line on each fallback, and §5's `broker_tier` audit field is the
check: exec-role commands on `stateful` while replicas > 0 mean the exec path is broken. The sandbox forwards only the variable names in `SANDBOX_FORWARDED_ENV_NAMES`
(`deploy/sandbox/entrypoint.sh`), so `CREDENTIAL_PROXY_EXEC_URL` is added there. Without that, the
shim always takes the fallback and nothing reports it.

### 3.3 Deployment

- A second Deployment, `<agent>-credential-proxy-exec-tier`, and Service, port 8765. Replicas from a new
  CR field (§9). Strategy `RollingUpdate`: no exec call keeps state across calls (§11, question
  1, closed). Step 0's long drain on SIGTERM (§9) lets a stopping replica finish the commands in
  flight, while `RollingUpdate` keeps another replica serving. This removes the exec outage
  during an upgrade at 2 or more replicas. A PodDisruptionBudget, set only when replicas are 2 or
  more, guards voluntary evictions such as a node drain; it has no part in a rollout.
- Same KSA, so the same Workload Identity GSA and the same grants. No new IAM.
- Own selector and labels. The exec pods do not reuse `credentialProxySelector`
  (`credential_proxy_manifests.go`), so today's Service does not send chat and workspace calls
  to them, where they would get a 404. They do not carry `kubeagents.x-k8s.io/has-credential-proxy`,
  which opens the github-token-minter to a pod. The exec builder mounts no forge credentials and no
  Slack tokens, so scaling out makes no copies of secrets that exec never uses.
- Every chart or operator object that selects today's broker labels (`app: <agent>-credential-proxy`
  or `kubeagents.x-k8s.io/component: credential-proxy`) gets an exec-tier counterpart. The builder
  finds them by a search for both labels. On `main` they are:
  - the broker PodMonitoring and its ServiceMonitor and VMPodScrape forms [code,
    `platform-agent-monitoring.yaml:81-88`];
  - the `<agent>-cred-metrics` Service that the ServiceMonitor selects [code, `:194-205`];
  - the `<agent>-cred-mon-netpol` NetworkPolicy that opens port 8766 to a scraper outside GMP, when
    `platformAgent.monitoringScraperNamespace` is set [code, `:415-434`];
  - the operator's usage-counter poller [code, `usage_counters_poller.go:646`].

  Without these, nothing scrapes the tier that runs every `/v1/exec`: §8.1's slots gauge and
  per-replica series, and the `toolExecutionsTotal` usage counter, would miss it.

- Resources per replica, on every cluster mode: requests 2 CPU / 2 GiB, limits 2 CPU / 2 GiB.
  Today's broker requests 500m CPU / 1 GiB with limits 1 CPU / 2 GiB [code,
  `credential_proxy_manifests.go:140-149`]. The 2 GiB request is the minimum from §4.2, and the
  2-CPU limit follows step 0 (§9). The CPU request equals the limit, so the scheduler reserves
  the 2 cores that §8.4's CPU term plans with. On Autopilot without bursting the operator already
  sets limits equal to requests, so the same values hold there. The slot cap and the memory
  budget stay per replica.
- A NetworkPolicy target for the exec tier: ingress on 8765 from the sandbox, from session pods
  only under the cluster-view flag (the condition the stateful broker has today), and the metrics
  port from GMP. The egress rules that select the broker's pod labels also need the exec tier as a
  peer: the sandbox (`shell_sandbox_manifests.go`) and session pods
  (`platformagent_a2a_manifests.go`). `/v1/exec` admits the shell, session and profile roles
  (#4257). So the exec tier also takes ingress on 8765 from profile worker pods in AgentProfile
  namespaces, and the egress rule that `agentprofile_namespace.go` renders toward the broker gets
  the exec tier as a peer. The agent pod gets no egress to it.
- The chart's footprint and the quota preflight add N × the exec replica's requests: 2 CPU /
  2 GiB each, on every cluster mode.

### 3.4 Identity and tokens per replica

- **Google access tokens:** each replica gets its own from the metadata server through Workload
  Identity. No coordination is needed.
- **Managed kubeconfigs:** each replica fills its own cache, and keeps a kubeconfig until the pod
  restarts (`_ensure_managed_kubeconfig`, `credential_proxy.py:7878`). The exception is a file
  written after a failed `describe`: it is provisional, and the replica fills it again after 60 s
  (`PROVISIONAL_RETRY_SECONDS`). So while a cluster's `describe` fails, each replica refetches it
  once a minute while requests for it arrive. A cold fill runs two
  `gcloud` children: a `describe` (`_endpoint_decision`) and then `get-credentials`. So a cluster
  costs up to N−1 extra cold fills per pod lifetime. The `get-credentials` calls that callers send
  each land on one replica, so they do not multiply. After a rollout the caches fill lazily, as
  clusters are read; a sweep that starts right after a rollout turns that into a burst.
  [predicted] At §8.4's 1,000 clusters and about 28 replicas, a sweep that reaches every cluster
  on every replica runs up to 1,000 × 28 × 2 = 56,000 `gcloud` children after each rollout. At
  1.6 to 2.0 broker core-seconds each (§8.3), that is about 90,000 to 112,000 core-seconds across
  the tier: 3,200 to 4,000 per replica, or 27 to 33 minutes of a replica's 2 cores. So open
  question 5 (build the kubeconfig from the cluster's endpoint and CA without `gcloud`) must be
  decided before default on (§9 step 4).
- **Scoped service account tokens:** with the scoped service account pool on
  (`scoped_sa_pool.py`), each replica keeps its own token cache, so token mints grow N times.
- **TokenReview cache:** per replica; one extra review per caller per replica. Small.
- **Profile callers (#4257):** each exec replica verifies the dispatcher-signed `proxyToken`
  itself, on every request. So the exec builder mounts the same operator-rendered
  `CREDENTIAL_PROXY_PROFILE_IDENTITIES` map and the task-credential verification key as the
  stateful broker. Both are configuration with a public key, and no secret is copied. The
  verification is per request, so nothing is cached across replicas.
- **Forge tokens:** not on the exec tier (§3.1), so mints do not multiply.

### 3.5 Capacity the tier reports

The admission route `GET /v1/exec/admission` in #4084 will report
what one pod admits, and the sweeps will size their pools from it. Behind a Service, one replica
answers. With N replicas it must report the tier, or the sweeps under-read by N. The operator
writes the replica count to a ConfigMap that the exec pods mount, and the route reads it on each
request. A scale change then does not change the pod template, so it does not restart the tier
and refill its caches (§3.4). The route reports `B × replicas` as what a sweep may use (the
background share, §4.2), with `admitted × replicas` and the per-replica numbers beside it. It
also reports the profile share, `P` per replica and `P × replicas`, with the profile commands in
use; a sweep sizes to `B × replicas`, which profile work shares. A
sweep sized to `admitted × replicas` would run past its share. It is a target, not a promise: a
replica that is not ready lowers the real number until the rollout ends.

### 3.6 Relation to the k8s-api-proxy (#3760, epic #4128)

#3760's Kubernetes API proxy is a separate proxy (its design: `docs/designs/kube-proxy.md` on that
branch). Epic #4128 splits that work into six steps: CI and hosting (done), the core library, the
image, the operator behind an opt-in switch that is off by default, the wiring of sandbox
`kubectl` through the proxy (with the eval loop), and default on, which retires the broker's
`kubectl` route. The required check and the release lines sit beside the first step.

- **Until #4128's default-on step, the broker carries all `kubectl`.** The opt-in switch is off by
  default, so every install keeps today's path until then. This design does not wait for #4128,
  and #4128 does not wait for this design.
- **The two efforts are independent in code.** Both change `credential_proxy_client.py` (the shim),
  so the second change resolves that small conflict.
- **The measurement plan sizes the broker twice** (§8.2): with the `kubectl` share, as installs run
  until default on, and without it, as they run after.

After default on, the split of the load changes:

```
  sandbox kubectl (every profile) ──► k8s-api-proxy  × 1 pod   (#3760, #4128)
  sandbox gcloud                  ──► exec tier      × N pods  (this design)
  session pods' /v1/exec          ──► exec tier      × N pods  (unless #4128 extends to them)
  Cloud API relay                 ──► exec tier      × N pods  (this design)
  relays, workspace, vcs/forge    ──► stateful broker × 1 pod
```

What moves, as #3760 is written [#3760 head]; #4128's wiring step may change it:

- **All `kubectl` from the sandbox pod.** The sandbox image drops the `kubectl` wrapper and ships
  the real binary. `gcloud container clusters get-credentials` still runs on the broker, and the
  shim rewrites the kubeconfig it returns to point at the k8s-api-proxy. So the fleet-audit
  collectors, the watches and the reconcile, which all run in the sandbox, move their `kubectl`
  too. The default kubeconfig is set for the root and `platform` profiles; a cluster profile uses
  its own pinned kubeconfig.
- **Not the session pods.** The k8s-api-proxy's NetworkPolicy admits port 8443 from the sandbox
  pod only, and the session pods keep `CREDENTIAL_PROXY_URL`. How their `kubectl` works after the
  wrapper is gone is open question 7.

What stays on the exec tier: `gcloud` (cluster lists, `describe`, `get-credentials`), and the
session pods' commands. The Cloud API relay moves there too (§3.1). #4199 already moved the
reconcile's GKE cluster lists and describes from `gcloud` onto the relay, which turns `gcloud`
children into relay requests on the same tier. The budget doc measured a
`gcloud` child at 101 to 102 MiB, and a `kubectl` read at 45 to 48 MiB plus 71 MiB for the GKE auth
plugin under it [doc, `credential-proxy-child-memory-budget.md` §1]. So `kubectl` leaving frees
about 116 MiB of child memory per command, and the exec tier keeps the `gcloud` children.

How the load splits between them:

- At the 143-cluster install, the broker ran about 6,000 exec requests an hour, about 80%
  `kubectl` and 20% `gcloud` by count (#2324, #2637). By memory and time, `gcloud` is the heavier
  part: 101 to 102 MiB per child, a 2 s median and an 18 s p95, as those issues report.
- The broker plan's first record, at 2 clusters during the first-run audits: 175 `kubectl`
  commands ran for 180 s in total, and 43 `gcloud` commands for 204 s. So `gcloud` was 20% of the
  commands but 53% of the slot time. The broker's CPU was throttled in 11% of its periods
  [measured, #4242, image `f8fc7905`, one 30-minute run].

§8.3 has the split for every audit run at 2 and 8 clusters. At 8 clusters `gcloud` was 15 to 35%
of the commands and 37 to 47% of the slot time, less than the 53% at 2 clusters. The larger fleet
sizes are not measured yet.

How the k8s-api-proxy runs [#3760 head, `kube_proxy_manifests.go`]:

- One replica, `Recreate`, requests 100m CPU / 256 MiB, limits 1 CPU / 512 MiB.
- The same KSA as the broker, so the same Workload Identity GSA, plus `system:auth-delegator`
  for TokenReview and the cluster-reader role.
- State: a TokenReview cache (up to 60 s), a per-cluster endpoint and transport cache (5 minutes),
  and the operator's TLS Secret, which every pod mounts. Nothing crosses calls.
- No admission cap and no queue: it counts in-flight requests in a gauge only. It streams
  responses, but it buffers a `Secret` response up to 64 MiB to scrub it.

**Can it scale out by itself?** By its state, yes: it has no relays and no workspace, so N
replicas behind its Service with `RollingUpdate` look possible without the split this design
needs for the broker. Two things it does not have yet are in this design: priority between
interactive and background work (§4), and a bound on concurrent work. Without a bound, many
buffered `Secret` responses at once can reach the 512 MiB limit [predicted: 8 × 64 MiB = 512 MiB].

What this means for this design:

- The exec tier is still needed for `gcloud` and the session pods, and the stateful/exec split
  does not change. After #4128's default-on step, `gcloud` alone keeps the tier at 2 or more
  replicas. [predicted] The 2-CPU run at 8 clusters (§8.3) spent 1,840 `gcloud` slot-seconds,
  about 230 per cluster for one audit round. With one round per hour, 2,500 clusters need about
  2,500 × 230 / 3,600 ≈ 160 `gcloud` slots, which is about 27 replicas at `B = 6`. The
  143-cluster split gives about 12 slots at 2,500 clusters, but only as a lower bound: that steady
  state used the 2 s median as the mean, and the #4242 audits measured `gcloud` means of 4.7 to
  17 s. From then on, the tier's size depends mostly on
  whether the change feeds in §6 take over the reconcile's cluster lists and `describe` calls and
  the collectors' `gcloud` reads.
- A per-target-cluster request limit belongs at the k8s-api-proxy. The fleet-scale requirements
  draft asks for about 1 non-watch request per second per cluster, averaged over 5 minutes, and 5
  in flight, with watches excluded, summed over every kube-agents component and replica. Only a
  reverse proxy sees single requests; the broker sees whole commands. With N proxy replicas the
  limit is split, by hashing clusters to replicas or by giving each replica 1/N.
- The class (§4.1) and the reserve and cap rules (§4.2) should apply to both proxies, so a sweep
  cannot fill either one ahead of interactive work.
- The footprint (§8.4) has two terms: exec replicas for `gcloud` and the Cloud API relay, and
  k8s-api-proxy replicas for `kubectl`.
- #4128 states that each `kubectl` call through the broker starts a process of about 100 MiB,
  which caps concurrency at a handful of commands; the budget doc measured about 116 MiB with the
  auth plugin. The k8s-api-proxy starts no process per call, so it also raises `kubectl` capacity,
  while it still runs as one pod.
- #3760 also removes the shim's Python client from every `kubectl`. Before #4104 fixed #4027,
  that client cost 0.8 to 1 s of shell CPU per command (§2.3); after the fix, §8.3 measured
  about 0.24 s. For `gcloud` the broker's CPU binds first: 0.38 to 0.47 commands per second at
  1 CPU (§8.3). The caller ceiling after the fix is not measured, because the `kubectl` load ran
  only 1 at once.

### 3.7 Later: one scaled broker

This design keeps a stateful pod because two route groups need it (§3.1). A later step could
drop the split and scale one broker, once both are gone:

- **The content workspace is removed.** Its callers on `main` today: the fleet audit's
  remediation push and its reads (`audit_report.py`, `_land_group_via_broker` and
  `detect_content_mode`), the collectors' repository mirror (`collect.py`), the deprecation scan
  (`api_deprecation_scan.py`) and `inspect-repository`. The snapshot verb (`POST /v1/vcs/snapshot`)
  covers the reads that the other `/v1/vcs` verbs do not: repositories the install does not
  manage, context repositories, and a shallow read. The read-only callers, such as
  `inspect-repository`'s `clone` and `api_deprecation_scan.py`, can move to it.
- **The chat relays are made safe on N pods, or move out of the broker.**

#4243 tracks the first step; no issue tracks the second. `CREDENTIAL_PROXY_CONTENT_WORKSPACE`
switches the workspace off in the broker (`credential_proxy.py:5405`). But on an operator install the operator pins the
flag to `1` and has no field to change it (`platformagent_manifests.go:4499-4540`). So a test of a
broker without the workspace needs an operator change. While the flag is off, the fleet audit's
remediation push and the other workspace callers above fail.

## 4. Priority admission

### 4.1 The class

Each exec request has a class: `interactive` or `background`. The broker sets it from what the
caller presents, and nothing the caller can set alone changes it.

- Once the turn token exists (open question 9), a shell-role request is `background` by default,
  and the fleet audits, the watches and the reconcile need nothing from their callers. Until then
  the shell role counts as interactive (the next bullets).
- A request is `interactive` only with a claim minted outside the sandbox: a session pod's own
  token (the session role), or a short-lived turn token that the agent's gateway mints for a chat
  turn. The sandbox session receives the turn token at its start, the way it receives
  `HERMES_PROFILE_HOME`, so it needs an `AcceptEnv` entry and the session-command path. #3889's
  per-task credentials have the same shape, and the turn token can be one of them.
- A profile-role request (#4257) is `background` by default, like the shell role once the turn
  token exists. A task that a chat turn started could later carry an interactive claim in its
  `proxyToken`; open question 9 covers that.
- The turn token does not exist on `main`. The broker authenticates only projected
  ServiceAccount tokens, through TokenReview (`ServiceAccountAuthenticator` in
  `credential_proxy.py`), and the session role comes from the token audience and
  `CREDENTIAL_PROXY_SESSION_CALLERS`. So the shell-role half of this section depends on open
  question 9. Until it is answered, shell-role and MCP-role requests count as interactive (§12),
  and only the profile and `scope` roles are background (§9, step 2).
- No header and no sandbox variable sets the class. The sandbox's `sshd` accepts only `LANG`,
  `LC_*` and `HERMES_PROFILE_HOME` (`deploy/sandbox/sshd_config`), so a cron variable would not
  reach the shim, and a header that any command can set is not a claim.
- A turn token that a command copies can use at most the interactive cap (§4.2). It gains no
  access. A copy is possible: the fleet audits run as the same `agent` account in the same
  container as chat turns, so a command can read a concurrent session's `/proc/<pid>/environ`. So
  the class protects chat latency only against work that does not read another session's
  environment. Open question 12 asks how to bind the token to something a copy cannot supply.

### 4.2 The rule, per replica

- Background work may hold at most `B = admitted − R` of a replica's admissions, where `admitted`
  is the smaller of the slot cap and what the memory budget admits. The proposal is `R = 2`:
  6 of 8 at the 2 GiB default, and 2 of 4 on Autopilot at a 1 GiB request, without the 2 GiB
  minimum below. The reserve is budget room as well
  as slots, so a background child cannot take the memory an interactive request needs. This is
  like the session cap of 2 today, but it reserves capacity instead of only limiting it.
- Interactive work may hold at most `I = admitted − 1`, so one admission is always free for
  background work and the reserve is not one-way.
- Profile work has its own share per replica: `P = min(CREDENTIAL_PROXY_PROFILE_SLOTS, B)`. A
  profile command counts against both `P` and the background cap `B`. With the defaults
  (admitted 8, `B = 6`, `P = 4`), profile work holds at most 4 admissions, all background work at
  most 6, and the interactive reserve of 2 is untouched. #4257 clamps its share to "pool − session
  − 1" on today's single queue. Once the classes exist (§9 step 2), the clamp to `B` takes its
  place, because the reserve already protects interactive work.
- Inside a class the order stays arrival order. Interactive requests go ahead of queued background
  requests, within the cap.
- Without a minimum, Autopilot's clamp to a 1 GiB request would make the budget admit 4, and
  `B = 2`. When the tier is on, each exec replica requests at least 2 GiB (§3.3). On Autopilot
  the limit then equals that request, so the budget admits 9, the slot cap holds it to 8, and
  `B = 6` on every cluster mode.
- A background request held at `B` waits up to 60 s, as today's queue does, and is then refused
  with the bound `background` (§4.3).
- The memory budget applies to both classes as it does today.
- The reserve guarantees admission, not CPU time. CPU binds first (§8.3), so an interactive
  command in a reserved slot still shares the replica's CPU with up to `B` background children.
  So the broker runs the children of background commands at a lower CPU weight (a nice value),
  and interactive children win the CPU under contention. A per-class child cgroup instead of nice
  is open question 11.
- The wait histogram, the refusal counter and the command duration histogram get a `class` label.

### 4.3 Across replicas

There is no shared queue. Each replica runs §4.2 on its own share of the traffic.

- The shim opens one connection per call, and kube-proxy balances per connection [measured, spike:
  69/51 over two pods]. So each replica gets about 1/N of each class.
- New: the shim retries a `503 CREDENTIAL_PROXY_BUSY` once. The retry is a new connection, so it
  reaches another replica with probability (N−1)/N: half the time at 2 replicas. It does not
  retry a dropped connection (`BrokerDisconnected`), because the command may have run.
  `CREDENTIAL_PROXY_BUSY` is raised only at admission, before the command starts, so a retry
  never runs a command twice.
- The retry skips a refusal at the session cap. Today that refusal uses the same code, so that the
  model reports it and does not retry [code, `credential_proxy.py`, `SessionSlots`]. The busy reply
  therefore gets a `bound` field with the bound that the broker already counts (`slot`, `budget`,
  `session` and others) [code, `ADMISSION_BOUND_*`]. A new bound, `draining`, marks the refusal
  of a replica that is stopping (§9 step 0). The shim retries `slot`, `budget`, `background`, `draining` and
  `profile`, and no other bound. `profile` is a share of one replica's pool, not a limit on one caller, so another
  replica can admit the command.
- The retry has costs. After a full 60 s wait it makes the worst case about 120 s, and when every
  replica is saturated it only adds load. §8 measures whether it helps; if not, it is dropped.
- The session cap of 2 is per replica, so the tier admits `2 × N` session commands. They also
  count against the interactive cap.
- Rejected for now: a central queue in front of the tier (a new component, and a new single point
  of failure), and a shared slot count in a store (a network round trip on every admission).
  Reopen them if §8 shows uneven load across replicas.

## 5. Audit as one stream

Every replica writes the same JSON lines (`tool_execution_audit`) to stdout, and the GKE log
agent collects them [code, `configure_logging` in `credential_proxy.py`]. Each line already
carries the principal and the pod. The design adds:

- `broker_tier` (`exec` or `stateful`) and the class, both set by the broker;
- the replica's pod name in the line itself, not only in the log's resource labels;
- the shim's existing `requestId`, reused on a retry, so a retry on another replica is one request
  in the log. The caller supplies it, so it goes in a field of its own and stays out of
  `principal`.
- the shim's caller name, for the traffic mix (§8.1). The caller supplies it too, so it is a field
  of its own, outside `principal`, and nothing trusts it.

Today's lines carry no target cluster. The broker parses a target only for `kubectl` (its
`kubeconfigContext`), so the line can carry that one where it exists.

"One stream" means one log name, one schema and one query across all pods. No shared file is
needed. The audit docs and the log query in the site's security page list both tiers.

## 6. The cache contract (option 3)

The fleet cache does not exist yet. This design does not wait for it. It writes down what the
cache must give, so the broker design stays valid whether the cache lands soon or later.

| Read                                                        | Moves to the cache | Freshness the caller needs                                     | Notes                          |
| ----------------------------------------------------------- | ------------------ | -------------------------------------------------------------- | ------------------------------ |
| Clusters, node pools, versions, locations                   | Yes                | Minutes (change feed: Cloud Asset Inventory or Admin Activity) | Sweeps, inventory questions    |
| Workload metadata (namespaces, Deployments, images, labels) | Yes                | Minutes to an hour                                             | Sweeps; not Pod status         |
| Pod status, events, logs, `describe`                        | No                 | Live                                                           | Drill-downs and triage         |
| Anything a write or a remediation depends on                | No                 | Live                                                           | Always read live before acting |

What a reader does:

- The cache answers with the data and its age.
- If the age is past the read's freshness, or the cache has no entry, the reader does a
  **bounded live read through the exec tier**, as `background` class, for the clusters in
  question only.
- A sweep may not fall back to a live read of the whole fleet. If more than a set share of
  clusters is stale, the sweep reports "cache stale" instead.
- The cache fills itself through the exec tier or its own feeds. Its live reads count as
  `background`.
- Which one fills it decides the sizing (§8.4). Change feeds that do not use the exec tier (Cloud
  Asset Inventory or Admin Activity) can fill clusters, node pools and versions. Workload metadata
  filled through the exec tier at hourly freshness adds back close to one sweep round per hour.

## 7. Option 2: brokers per profile or per project

**Recommendation: do not build per-profile or per-project brokers for throughput.** The exec tier
gives throughput. A broker per profile multiplies the footprint and the caches by the profile
count, and changes the operator, the NetworkPolicy and the audit.

**For isolation, the boundary is the credential, not a check in the broker.**

- Every exec replica holds the same GSA, so a check inside the tier is a policy layer in front of
  a wide credential, not an IAM boundary. Also, the broker parses no target for `gcloud`,
  `/v1/gcp/` runs on the exec tier with the same GSA, and after #4128's default-on step `kubectl` goes
  through the k8s-api-proxy.
- The real boundaries are two. First, **one pool per credential tier** (Read, Unattended), each
  with its own KSA and GSA, when a tier stronger than Read gets its own credentials: a second exec
  Deployment, not one per profile, decided when #3659's tier CRs land. Second, the scoped service
  account pool (`scoped_sa_pool.py`, off by default: one reader account per project in scope, which
  holds no grants until bound), which narrows a request's credential to one project.
- Per-task scope (#3889's signed statement of a task's target) is then checked where each path
  can read its target: the exec tier for a `kubectl` context (until #4128's default-on step), the
  k8s-api-proxy for its `/clusters/<id>` path, and `/v1/gcp/` for the project in its URL. Each
  check is defence in depth on top of the credential, and `gcloud` has no such check.

## 8. Measurement plan

The #3935 runner is the tool. The tiger team owns it, so the plan is a new plan file,
`bench/scale/broker.yaml` on #4242, run as part of #3753 Phase 2. This document owns what it measures. The
first run, on 2026-10-28, is in #4242: 32 runs at 2 and 8 clusters on a `spec.mode: next` install,
with an agent image that has the #3933 fix (`f8fc7905`) and the broker at 1 CPU / 2 GiB, slot cap 8.

### 8.1 Metrics to add

| Metric                                                                                                 | Source                                                                                                                                                                   | Status                                                                                                                              |
| ------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------- |
| Slots in use (peak) and admitted                                                                       | `kubeagents_credential_proxy_slots_in_use` gauge [code], as Managed Prometheus stores it (30 s scrape); `/v1/exec/admission` (#4084)                                     | Read by the runner (#4242). The 2026-10-28 cluster stored no Managed Prometheus series for the gauge, so those records have no peak |
| Wait p50 and p95, refusals, **by class**                                                               | Wait histogram with a new `class` label; the refusal counter also gets the `draining` bound (§4.3), so a rollout does not count as load, and the `profile` bound (#4257) | Needs §4                                                                                                                            |
| Command duration p50 and p95 **by class**                                                              | The broker's command duration histogram with a new `class` label; shows whether the nice value (§4.2) protects interactive run time                                      | Needs §4                                                                                                                            |
| Requests and CPU by route: `/v1/gcp/`, the other non-exec routes, and exec                             | `kubeagents_credential_proxy_requests_total` by its `endpoint` label; exec CPU from the per-tool command time and §8.3's CPU per command                                 | New; gives the stateful pod its sizing term (§8.4, open question 4)                                                                 |
| Traffic mix: requests and slot-seconds by caller (sweep, reconcile, stall watch, interactive, session) | Audit lines with the class, and a caller name from the shim (caller-supplied, outside `principal`)                                                                       | New                                                                                                                                 |
| CPU throttling                                                                                         | The proxy container's cgroup `cpu.stat`, before and after each run                                                                                                       | Measured (#4242)                                                                                                                    |
| Per-replica balance                                                                                    | The same metrics per pod                                                                                                                                                 | New in the runner                                                                                                                   |
| Requests and slot-seconds by tool (`kubectl`, `gcloud`, other)                                         | The broker's command duration histogram, by its `tool` label                                                                                                             | Measured (#4242); splits the load §3.6 moves                                                                                        |
| k8s-api-proxy CPU, memory and in-flight requests                                                       | Cloud Monitoring; its `in_flight_requests` gauge (#3760, #4128)                                                                                                          | New in the runner, with the proxy switched on                                                                                       |

### 8.2 Points

| Run set           | Points                                                                                                             | Why                                                                                       | Status                                                                                                              |
| ----------------- | ------------------------------------------------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------- |
| Baseline, one pod | 2, 8, 16, 32 clusters, with a fleet audit that really runs                                                         | Where waits and refusals start, and the sweep's share                                     | 2 and 8 measured, 3 runs each (#4242); 16 and 32 not yet                                                            |
| Mixed concurrency | 8, 10, 12, 15 interactive questions during a sweep; interactive command time under a background `gcloud` sweep     | Interactive waits and run time behind background work, today and with §4 (the nice value) | Questions measured at 8 clusters, 3 runs each, without a parallel sweep (#4242); command time under a sweep not yet |
| Vertical control  | Broker CPU at 2 cores at 8 clusters (`bench/scale/broker-proxy-raised.yaml` on #4242); #4107 ran 4 CPU at 2 and 16 | CPU-bound or slot-bound                                                                   | Measured, 1 run (#4242)                                                                                             |
| Autopilot         | One point at the 1 GiB clamp (budget admits 4)                                                                     | The worst default                                                                         | Not yet                                                                                                             |
| Mode              | One baseline point in `spec.mode: today`                                                                           | Phase 1 covered `next` only                                                               | Not yet                                                                                                             |
| Exec tier         | 1, 2 and 3 replicas at 16 and 32 clusters                                                                          | #4082 "Done when": the chosen shape at two or more sizes                                  | Not yet; needs the tier                                                                                             |
| `kubectl` split   | The baseline points again with #4128's switch on                                                                   | The broker sized without the `kubectl` share, and the k8s-api-proxy's own load            | Not yet; needs #4128                                                                                                |

Each point runs 3 times, except where the status says otherwise: the first run cut the `gcloud`
load levels and the 2-CPU control to 1 run each, to fit its time limit.

### 8.3 Results of the first run (#4242)

The fleet audits ran: the image has the #3933 fix, so no audit cancelled. In two windows a job failed or was still running at the end. They read the clusters and
cannot write, because the experiment install has no GitOps repository. All numbers are [measured,
#4242], with the broker at 1 CPU unless a line says 2 CPU.

| Point                                 | Result                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 |
| ------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Audits, 2 clusters (3 × 30 min)       | 158 to 218 commands. `gcloud`: 20 to 26% of the commands, 53 to 62% of the slot time. 0.2 to 0.5 slots in use on average. CPU throttled in 11 to 13% of periods. Wait p50 0.1 s, max 2.5 s. No refusals.                                                                                                                                                                                                                                                                                               |
| Audits, 8 clusters (3 × 30 min)       | Runs 1 and 2: 773 to 790 commands, 4.4 to 4.6 slots in use on average, mean command time 10.3 to 10.5 s, CPU throttled in 72 to 74% of periods, wait p50 5 s, max in the 60 s bucket. `gcloud`: 199 and 200 commands (25 to 26%), 3,296 and 3,460 slot-seconds (40 to 43% of the slot time). Run 3 was lighter (cause not confirmed): 511 commands, 1.9 slots, 6.7 s, 37% throttled, wait p50 0.1 s, max 30 s; `gcloud` 77 commands (15%), 1,252 slot-seconds (37%). No refusals. Memory peak 716 MiB. |
| Audits, 8 clusters, **2 CPU** (1 run) | 893 commands. Mean command time 4.4 s. 2.2 slots in use. `gcloud`: 315 commands (35%), 1,840 slot-seconds (47% of the slot time). CPU peak 2.1 (at the new limit), mean 0.88 cores; throttled in 42% of periods. Wait max 30 s. Memory peak 680 MiB. One job was still running at the end of the window.                                                                                                                                                                                               |
| `gcloud` load, 1 / 4 / 8 / 16 at once | Levels 1 to 8: 0.38 / 0.47 / 0.43 commands per second; p50 2.5 / 8.5 / 18 s. From 4 at once, about 95% of CPU periods are throttled. The 4 and 8 levels ran once each. The 16 level (0.35 per second, p50 45 s, wait p50 30 s) comes from a run that the runner excluded, because 19 outside commands overlapped it, so it is not clean. It suggests that the 8 slots bind at 16.                                                                                                                      |
| `kubectl` load, 1 at once             | 1.5 commands per second, bound by latency (p50 0.6 s), with the sandbox shell at about 0.36 cores (1 clean run; 2 more overlapped other work). Before #4104 fixed #4027, #4107 got 0.7 to 0.8 per second at 1 at once.                                                                                                                                                                                                                                                                                 |
| Questions at once, 8 clusters         | 8 and 10: no refusals, answers in 3 to 10 minutes; most of these runs also met model-provider 429s. 12: 2 to 3 refused per run. 15: 5 refused per run (`429 Too many concurrent runs`). The broker stays almost idle: 0.53 slots or fewer in use on average.                                                                                                                                                                                                                                           |

What they mean for this design:

- **One CPU is the first limit, not the slots.** `gcloud` throughput stays flat at 0.38 to 0.47 per second from 1 to 8 at once, while the p50 grows from 2.5 to 18 s. This supports the CPU raise as step 0 (§9) and the CPU term in §8.4.
- **A 2-CPU limit more than halves the command time** at 8 clusters. The broker reached that limit at peak (mean 0.88 cores, 42% of periods throttled), so at peak it needs more than 2 cores at 8 clusters.
- **`gcloud` is the load that stays on the exec tier** (§3.6). Per command it costs about 5 times the broker CPU of `kubectl` (about 1.6 to 2.0 against about 0.34 core-seconds).
- **Interactive bursts meet the agent's 10 run slots first** (#3724), as §2.3 said.
- **Memory is not a limit at these sizes:** at most 775 MiB of 2 GiB.

Not measured yet: 16 and 32 clusters, Autopilot, `spec.mode: today`, the exec tier itself, and the
peak slots in use (the 2026-10-28 cluster stored no series for the gauge).

### 8.4 Footprint at 250 / 1,000 / 2,500 clusters

The doc must give this, so the formula is fixed now, and the numbers come from §8.2:

```
  exec replicas = max( ceil( background slots / B ),
                       ceil( all exec slots / admitted per replica ),
                       ceil( exec CPU cores / CPU limit per replica ) )
  slots         = sweep slots (clusters × slot-minutes per cluster / sweep window)
                + interactive slots + reconcile and stall-watch slots
```

CPU comes first. At 16 clusters, during the first-run audits, #4107 measured about 36 commands a
minute at about 3 s each with the broker at 4 CPU (about 1.8 slots in use), against 22 a minute
at 17 s with 1 CPU [measured, #4107]. Slot-minutes per cluster therefore depend on CPU. So the
CPU-limit raise (#4106) is step 0 (§9), and the slot terms are taken after it.

The worked example takes an exec replica with a 2-CPU request and limit (§3.3) and a 1,000-cluster
audit round in one hour. Its base is the measured 8-cluster run at 2 CPU (§8.3) [measured, #4242]:
about 8.2 slot-minutes and about 200 broker core-seconds per cluster for one audit round. #4107's
two 16-cluster points are kept for comparison [measured, #4107]. Every row's CPU term uses one
basis: the mean core-seconds per cluster, which is the broker container's CPU seconds in the
30-minute run divided by its clusters.

| Base (1,000 clusters)         | Slot-minutes per cluster | Background slots | Slot term (`B = 6`) | CPU term (mean core-seconds per cluster; 2 CPU per replica)                                                             | Replicas     |
| ----------------------------- | ------------------------ | ---------------- | ------------------- | ----------------------------------------------------------------------------------------------------------------------- | ------------ |
| **8 clusters, 2 CPU (#4242)** | about 8.2                | about 136        | 23                  | about 200 core-seconds per cluster: about 55 cores, 28. Peak basis: 2.1 cores per 2.2 slots in use, about 132 cores, 66 | **about 28** |
| 16 clusters, 4 CPU (#4107)    | about 3.4                | about 57         | 10                  | about 101 core-seconds per cluster: about 28 cores, 15                                                                  | about 15     |
| 16 clusters, 1 CPU (#4107)    | about 12                 | about 202        | 34                  | about 106 core-seconds per cluster (at its 1-CPU limit): about 29 cores, 15                                             | about 34     |

The tables fill two of the formula's terms: the background slot term and the CPU term. The
interactive, reconcile and stall-watch slots are taken as zero: at 8 clusters, 8 to 15 questions at
once kept the broker at 0.53 slots or fewer on average (§8.3). The all-slots term is then the
background slots over 8 admitted per replica, about 17 at 1,000 clusters, under the CPU term.

The headline uses the mean basis. The two raised-limit runs disagree by about 2× per cluster:
about 200 core-seconds at 8 clusters and 2 CPU against about 101 at 16 clusters and 4 CPU. So one
30-minute window is not a stable per-cluster cost.

[predicted] The same formula and the #4242 base at the other two sizes, linear in clusters, with
one round per hour:

| Clusters | Background slots | Slot term (`B = 6`) | CPU cores (mean basis) | CPU term | Replicas without a cache | Replicas with the cache, if feeds supply the fill |
| -------- | ---------------- | ------------------- | ---------------------- | -------- | ------------------------ | ------------------------------------------------- |
| 250      | about 34         | 6                   | about 14               | 7        | about 7                  | about 2                                           |
| 1,000    | about 136        | 23                  | about 55               | 28       | about 28                 | about 6                                           |
| 2,500    | about 340        | 57                  | about 139              | 70       | about 70                 | about 14                                          |

[predicted] So about 28 replicas at 1,000 clusters without a cache. With the cache it is about 6
if feeds supply the fill, up to about the uncached figure if the exec tier fills workload
metadata. The 6 assumes that the cache takes 80% of the sweep's reads (an assumption), and that
change feeds that do not use the exec tier (Cloud Asset Inventory or Admin Activity, for
clusters, node pools and versions) fill it. Workload metadata filled through the exec tier at
hourly freshness (§6) adds back close to one round per hour. These are floors, for three reasons:

- one audit job was still running at the end of the 30-minute window, so 8.2 slot-minutes and 200
  core-seconds are a lower bound for a full round;
- the CPU term plans each replica at its full 2 cores on average, while the measured broker was
  throttled in 42% of its periods at 44% average use, so the peak needs more (about 66 replicas
  on the peak basis);
- these are averages, and the peak is higher.

It is also a straight line from 8 clusters to 1,000 and 2,500, from one run whose broker reached
its 2-CPU limit at peak, so it is an order of magnitude, not a size. The CPU term sets the count,
as before. The core-seconds come from the whole broker, which also runs the other routes, so they
overstate the exec tier's share. The exec tier carries the Cloud API relay too (§3.1), so its CPU
term includes the relay's requests. #4199's move of the reconcile's cluster reads from `gcloud` to
the relay lowers the cost per cluster, which the next run measures. The stateful pod has no
sizing term yet; the per-route request and CPU measure in §8.1 gives it. The 16- and 32-cluster points and the exec
tier runs (§8.2) replace these numbers. This is why the design needs both the exec tier and the
cache contract.

## 9. Rollout

0. **Phase 0, useful at one replica:**
   - **Raise the broker's CPU limit (#4106)** to at least 2 cores, the exec replica's limit
     (§3.3). It is the first limit measured (§2.3), and it costs one value in the operator. On
     Autopilot without bursting the limit equals the request, so raising the limit alone does
     nothing there: the operator raises the broker's CPU request with its limit, and the footprint
     and the quota preflight count that.
     Because it is one operator value, it is the fastest single relief for current installs.
   - **Graceful shutdown.** Today `serve()` runs `serve_forever()` with no SIGTERM handler, and
     the Deployment sets no `terminationGracePeriodSeconds` and no `preStop`, so a pod stop drops
     the commands in flight. The broker gets a SIGTERM handler that stops admitting (new requests
     get a 503 with the new bound `draining`, which §4.3's retry sends to another replica) and
     drains the commands in flight. The drain depends
     on the Deployment:
     - On the single pod, which keeps `Recreate`, Kubernetes ends the old pod before it starts
       the new one, so every route is down while the pod drains. There the drain is short and
       bounded: it finishes commands for up to 30 s, then ends the rest. The pod drains its relays
       in the same window, and its `terminationGracePeriodSeconds` is 45 s, so the kubelet does
       not kill it before the drain and the exit end, and events are not lost.
     - On the exec tier, `RollingUpdate` keeps another replica serving, so the drain is long: a
       `terminationGracePeriodSeconds` longer than the longest command it admits. A
       PodDisruptionBudget with `maxUnavailable: 1` applies only when replicas are 2 or more; at 1
       replica it protects nothing.
1. **Off by default.** New CR field `spec.deployment.credentialProxy.exec.replicas`. The value 0
   (the default) means no exec tier and no `CREDENTIAL_PROXY_EXEC_URL`, so every install behaves
   as today. The operator sets N. It is not derived from the fleet size and not an autoscaling
   floor (open question 13). The signals for raising it are per replica (§8.1): the background
   admission wait (its p50 above the lowest bucket), refusals by bound (`slot` or `budget`), and
   CPU throttling, which the admission route (§3.5) also reports for the tier. Raise N while
   those persist. The implementation changes the slot-wait advice in the site's `observability.md`
   to name this field, beside narrowing the reconcile.
2. **Per-class metrics and the session-role class first,** on the single pod. They give the
   traffic mix (§8.1) before anything scales. Until the turn token exists (open question 9), the
   shell and MCP roles count as interactive, so the background cap holds only profile and `scope`
   work. Chat commands and the MCP server's interactive tools that run through `sandbox_exec` then
   get at most one admission fewer than today, which stays free for background work.
3. **Exec tier on the eval install and the experiment install,** at 2 replicas. Run §8.2.
4. **Default on** at a replica count from the measurements, with the chart footprint updated.
5. **Then** mark `credential-proxy-single-pod.md` superseded. It already points here.

The cache contract (§6) is not needed to ship the tier, and its work can run in parallel with steps
1 to 3. The footprint in §8.4 assumes it, though. For a 1,000-cluster round in one hour it is about
28 replicas without the cache. With the cache it is about 6 if feeds supply the fill, up to about
the uncached figure if the exec tier fills workload metadata [predicted, and floors: §8.4]. So for
large fleets the cache, and the feeds that fill it, must land before default on (step 4).

Do not add new work to the stateful pod beyond phase 0. §3.7 can remove most of its routes later:
the content workspace, and the chat relays if they become safe on N pods.

The stateful broker keeps `/v1/exec` and `/v1/gcp/` for as long as 0 replicas is a legal value.
An install at 0 (§10's small installs, or a revert) has no exec URL, and neither does a sandbox on
an older image; their callers reach the stateful pod, and a 404 there would break every command.
So the routes leave the stateful broker only in a release that makes replicas ≥ 1 required, and
only when no supported sandbox image lacks `CREDENTIAL_PROXY_EXEC_URL`.

To revert, set replicas to 0. The shim falls back to `CREDENTIAL_PROXY_URL`. This revert works as
long as the stateful pod serves those routes (above). The sandbox reads
`CREDENTIAL_PROXY_EXEC_URL` only when its pod starts (`sshd`'s `SetEnv`), so a change between 0 and
N replicas restarts the sandbox pod and ends its open sessions. Until that restart, the shim's
connect fallback (§3.2) sends commands to the stateful pod. The same holds when the tier is turned
on: the sandbox restarts with the variable before an exec replica is ready.

## 10. Risks

- **Uneven load.** Per-connection balancing is not per-load balancing. Long `kubectl` reads on one
  replica can fill it while another is idle. The retry in §4.3 and the per-replica metrics in §8.1
  are the checks.
- **Caches filled N times.** More cold fills, each a `describe` and a `get-credentials` (§3.4),
  and more TokenReviews. Open question 5 (a kubeconfig without `gcloud`) must be decided before
  default on.
- **Footprint.** Each replica adds 2 CPU and 2 GiB to the requests on every cluster mode; the
  request equals the limit (§3.3). On small installs, 0 replicas stays the default until a
  fleet needs it.
- **A wrong class.** Once the turn token exists, interactive work without one runs as background
  and waits longer.
  A copied turn token can use at most the interactive cap. Neither gains access. A sweep can copy a
  turn token from `/proc` (§4.1) and then go ahead of the turn it copied it from.
- **The k8s-api-proxy as a credential holder.** It runs as the broker's KSA, so it uses the same
  Workload Identity GSA and mints tokens for every cluster. Once it carries most Kubernetes
  traffic and scales out (§3.6), it needs a security review of its own, separate from the
  broker's.
- **Two addresses.** A caller that uses the wrong URL gets a 404 from the exec role, which is loud
  and easy to find in tests.
- **Background starvation.** Interactive work goes first, within its cap of `admitted − 1`. A long
  interactive burst can still slow sweeps to one admission per replica. §8.2's mixed-concurrency
  runs show whether the cap is enough.

## 11. Open questions

1. Closed: no `/v1/exec` call uses a content-workspace handle. `/v1/exec` accepts only `gcloud`
   and `kubectl` (`EXEC_ROUTE_EXECUTABLES`, `credential_proxy.py:5439`, enforced at
   `:9533-9547`), and no exec path takes a handle. The broker's own `git` in the workspace runs
   through `execute_workspace_git` (`:6704`), a separate door that `/v1/exec` cannot reach.
2. Are a reserve of `R = 2` and an interactive cap of `admitted − 1` right? The measured mix
   (§8.1) decides them.
3. Do the dispatcher's profile workers reach the broker through the sandbox shim as the shell role?
   This was not checked.
4. Closed: the Cloud API relay moves to the exec tier (§3.1). It keeps no state across calls, it
   uses the same KSA, and #4199 moved the reconcile's GKE cluster reads onto it, so it grows with
   the fleet. The per-route measure (§8.1) still sizes the stateful pod.
5. Kubeconfig without `gcloud` (§3.4): to decide before default on (§9 step 4). At about 28
   replicas and 1,000 clusters, the cold fill after each rollout runs up to 56,000 `gcloud`
   children. A cluster whose `describe` keeps failing adds two children per replica each minute.
6. Who owns `bench/scale/broker.yaml`: the tiger team, as part of Phase 2, with this doc as its
   requirements? The first run (#4242) used it; to agree with them before the 16- and 32-cluster runs.
7. For dshnayder, on #3760 and epic #4128 (§3.6):
   - How do the session pods run `kubectl` once the sandbox image has no wrapper? Do they move to
     the k8s-api-proxy, or keep `/v1/exec`?
   - Is there a reason for one replica and `Recreate`, or can the k8s-api-proxy run N replicas
     with `RollingUpdate`?
   - Should it take the same broker-side class, reserve and cap rules as the exec tier, and a bound on
     concurrent requests for the buffered `Secret` responses?
   - Did the concurrency and memory benchmark in its design ("16 parallel multi-cluster listing
     passes") run, and with what numbers?
8. Closed: #4027 capped the command path at about 1.5 commands per second in the sandbox. #4104
   (merge `37a9829b`) fixed it on `main`, and the #4242 image `f8fc7905` has the fix. The caller
   ceiling after the fix is not measured (§3.6).
9. How does the gateway mint the turn token (§4.1), and can #3889's per-task credential be it for
   chat turns as well as for profile workers? The same claim could mark a profile task that a chat
   turn started as interactive (§4.1).
10. After #4128's default-on step, should `/v1/watcher/*` and `/v1/k8s/*` move to the
    k8s-api-proxy? They do the same job (Kubernetes API reads under kube-agents' identity) and keep
    no state that breaks on more than one pod (§3.1), so there would be one Kubernetes path instead
    of two.
11. Should background children run in a per-class child cgroup with a lower CPU weight, instead
    of at a nice value (§4.2)?
12. How does the broker bind a turn token to its turn, so that a command in the same sandbox that
    copies it from `/proc` cannot present it (§4.1)?
13. Should the exec tier get a HorizontalPodAutoscaler, with `exec.replicas` as its floor (§9 step
    1)? The CPU and admission signals above are what it would scale on.

## 12. Implementation contract

The names and shapes that the implementation shares. The operator, the broker, the shim and the
chart will use these exact values; on `main` today, none of them does yet. A Python block in `credential_proxy_client.py` holds the broker's
and the shim's copies, and `test_exec_tier_contract.py` checks that they match this section.

| Item                  | Value                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           |
| --------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| CR field              | `spec.deployment.credentialProxy.exec.replicas` (`Exec *CredentialProxyExecSpec`, `Replicas *int32`): an integer from 0 to 32; unset and 0 both mean off. At 0 the operator renders no exec workload and sets no exec URL. Each replica requests and is limited to 2 CPU and 2 GiB (§3.3); there is no override field. The bound 32 covers §8.4's mean-basis estimate for 1,000 clusters without the cache (about 28); larger fleets need the cache before default on (§9), and a later release can raise the bound.                                                            |
| Workload names        | Deployment, Service and PodDisruptionBudget `<agent>-credential-proxy-exec-tier`; ConfigMap `<agent>-credential-proxy-exec-tier-replicas`. Port 8765 for requests, 8766 for metrics. Not `credential-proxy-exec`, which is the shim's client binary in the images. The Service name and the `app` label must fit in 63 characters, so with the tier on, an agent's name is at most 36 characters (63 minus the 27-character suffix); the operator refuses a longer name when `exec.replicas` is above 0.                                                                        |
| Labels                | `app: <agent>-credential-proxy-exec-tier` and `kubeagents.x-k8s.io/component: credential-proxy-exec-tier`. Not the stateful broker's labels, and no `kubeagents.x-k8s.io/has-credential-proxy`.                                                                                                                                                                                                                                                                                                                                                                                 |
| Broker role           | `CREDENTIAL_PROXY_ROLE=exec`. An exec pod runs the broker and Envoy only: no watchers and no relays.                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| Exec routes           | `POST /v1/exec`, `GET /v1/gcp/…`, `GET /v1/exec/admission`, `/healthz` and metrics. Every other route answers 404 on an exec pod. The `scope` role's `/v1/exec` call is refused there with 403 `CALLER_ROLE_FORBIDDEN`, because its member window lives on the stateful pod.                                                                                                                                                                                                                                                                                                    |
| Caller URL            | `CREDENTIAL_PROXY_EXEC_URL`, the exec Service's URL, for the sandbox, session pods and profile workers; the A2A gateway reads it from `A2A_CREDENTIAL_PROXY_EXEC_URL`. Unset at 0 replicas, so callers use `CREDENTIAL_PROXY_URL`. The scope publisher Job keeps `CREDENTIAL_PROXY_URL`. The fleet audit's admission client (`broker_admission.py`) reads `CREDENTIAL_PROXY_URL`; the capacity change (§3.5) points it at the exec URL.                                                                                                                                         |
| Replica count in pods | The ConfigMap's key `replicas`, mounted at `/etc/credential-proxy-exec-tier/replicas`; `CREDENTIAL_PROXY_EXEC_REPLICAS_FILE` names that path. A scale change rewrites the ConfigMap and does not restart the pods.                                                                                                                                                                                                                                                                                                                                                              |
| Busy reply            | HTTP 503 `{"status": "busy", "code": "CREDENTIAL_PROXY_BUSY", "bound": "<bound>", "error": "…"}`. The bounds are `slot`, `budget`, `refresh-lock`, `session`, `profile`, `scope`, `background`, `draining` and `other`; `background` is the background cap `B` (§4.2) after the 60 s wait; `draining` is a replica that is stopping (§9 step 0). `scope` is the scope publisher role's refusal, not a capacity bound, so the shim does not retry it.                                                                                                                            |
| Shim retry            | Once, on a new connection, for the bounds `slot`, `budget`, `background`, `draining` and `profile`; never for the others, and never after a dropped connection.                                                                                                                                                                                                                                                                                                                                                                                                                 |
| Connect fallback      | When a connection to `CREDENTIAL_PROXY_EXEC_URL` cannot be made, the shim sends the request to `CREDENTIAL_PROXY_URL` and writes one stderr line.                                                                                                                                                                                                                                                                                                                                                                                                                               |
| Classes               | `interactive` and `background`; the metric label is `class`, on the admission wait, the refusals and the command duration. The profile and `scope` roles are background. Every other slot-taking role is interactive: the session role, the MCP role, and the shell role (its vcs and forge routes too) until the turn token exists (§4.1). Interactive work is held at `admitted − 1`, so one admission stays free for background work. Background children run at nice 10, set on their process group right after they start. The reserve `R = 2` and the caps are constants. |
| Audit fields          | `broker_tier` (`exec` or `stateful`), `class` and `broker_pod` on each tool-execution record. §5's `requestId` and caller name are fields of their own, outside `principal`.                                                                                                                                                                                                                                                                                                                                                                                                    |
