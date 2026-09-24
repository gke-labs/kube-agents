---
title: PlatformAgent CRD
description: The single custom resource the operator reconciles.
sidebar:
  order: 1
---

The `PlatformAgent` resource declares everything the operator needs to run one Platform Agent instance: which Hermes image, which service account, which chat integrations, and which framework-level toggles.

- **API group / version**: `kubeagents.x-k8s.io/v1alpha1`
- **Kind**: `PlatformAgent`
- **Source**: [`k8s-operator/api/v1alpha1/platformagent_types.go`](https://github.com/gke-labs/kube-agents/blob/main/k8s-operator/api/v1alpha1/platformagent_types.go)
- **Sample**: [`k8s-operator/examples/platformagent.yaml`](https://github.com/gke-labs/kube-agents/blob/main/k8s-operator/examples/platformagent.yaml)

## Top-level shape

```yaml
apiVersion: kubeagents.x-k8s.io/v1alpha1
kind: PlatformAgent
metadata:
  name: platformagent
  namespace: kubeagents-system
spec:
  harness: { ... } # execution environment + framework
  deployment: { ... } # container image, pull policy, containers, volumes
  security: { ... } # service account + Workload Identity
  telemetry: { ... } # OTLP collector endpoint (optional)
  networkPolicy: { ... } # generated egress NetworkPolicy (optional)
  integration: { ... } # Google Chat, Slack, GitHub
  scope: { ... } # projects beyond the management project, and exclusions (optional)
  mode: today # unsupported dev toggle for the A2A stack (optional)
```

`spec.deployment`, `spec.security`, `spec.telemetry`, and `spec.networkPolicy` are inlined from the shared `AgentSpec`, so they are common to every agent type. `spec.harness` is required; `spec.integration`, `spec.telemetry`, `spec.networkPolicy`, and `spec.scope` are optional. `spec.mode` is an optional enum (`today`/`next`, absent means `today`) and, like `experimental.platformFrontDoor`, **unsupported**: it is the dev toggle from `docs/designs/spec-mode-switch.md` that keeps the A2A `next` stack dark, and it is deliberately not surfaced in the Helm chart. Under `next` the operator additionally renders the stage-1 A2A playground stack — the NATS/JetStream component and its provisioning Job, an ingress NetworkPolicy fencing the bus, an egress NetworkPolicy fencing the session pods, a session-pod ResourceQuota, the A2A gateway Deployment with session-pod spawning armed, and the auth callout that authenticates bus clients (its Deployment, Service, ServiceAccount, Role and RoleBinding, a cluster-scoped ClusterRoleBinding to `system:auth-delegator`, the identity-map ConfigMap `<agent>-a2a-authmap`, and the keys Secret `<agent>-a2a-callout-keys`), plus ServiceAccounts for the provisioning Job and for the spawned session pods, so the callout has an identity to resolve each by. Flipping back to `today` tears that stack down, keeping the generated credentials Secret and the JetStream PVC. The callout's keys Secret is **not** kept: a stale issuer key would make every answer the callout gives be refused, so it is deleted with the rest.

## `spec.harness`

Framework-level settings passed to Hermes. `clusterName`, `location`, and `projectId` are all
required — the API server rejects a `PlatformAgent` that omits any of them. The credential proxy
only renders its kubeconfig bootstrap (the `gcloud container clusters get-credentials` that gives
the agent a usable kubectl context) when it has the complete triple; with one missing, every
`kubectl` the agent runs resolves to `localhost:8080` instead of a cluster.

| Field                                          | Type   | Purpose                                                                                                                                                                                                                           |
| ---------------------------------------------- | ------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `clusterName`                                  | string | Logical cluster name (e.g. `cluster-a`). Surfaces in observability and chat replies.                                                                                                                                              |
| `location`                                     | string | Cloud region (e.g. `us-central1-a`).                                                                                                                                                                                              |
| `projectId`                                    | string | GCP Project ID of the cluster. Required.                                                                                                                                                                                          |
| `hermes.dashboardEnabled`                      | bool   | Toggle the Hermes dashboard endpoint. Default `true`.                                                                                                                                                                             |
| `hermes.pluginsDebug`                          | bool   | Enable plugin-level debug logging. Default `false`.                                                                                                                                                                               |
| `hermes.agentHome`                             | string | Path to the `AGENT_HOME` directory. Default `/opt/data`.                                                                                                                                                                          |
| `hermes.apiServerSecretRef.name` + `key`       | string | `Secret` holding `API_SERVER_EXTERNAL_KEY`, the credential outside callers present to the gateway pod's `agent-api-auth` sidecar. Not `API_SERVER_KEY` — see [How config reaches each profile](#how-config-reaches-each-profile). |
| `hermes.sessionKVApiKeySecretRef.name` + `key` | string | `Secret` holding the bearer token for the pod-local Session KV server (`SESSION_KV_API_KEY`). Optional; absent, the server rejects every request with `503`.                                                                      |
| `hermes.sessionKVSaltSecretRef.name` + `key`   | string | `Secret` holding the HMAC salt used to pseudonymise chat identities (`SESSION_KV_SALT`). Optional; absent, the agent generates a per-pod salt and warns.                                                                          |
| `memory.memoryEnabled`                         | bool   | Toggle framework memory persistence. Default `false`.                                                                                                                                                                             |
| `memory.provider`                              | string | Memory provider implementation. Default `multiuser_memory`; `none` for none. See below.                                                                                                                                           |
| `memory.userProfileEnabled`                    | bool   | Toggle per-user memory profiling. Default `false`.                                                                                                                                                                                |
| `eventWatcher.enabled`                         | bool   | Start the `k8s-event-watcher`. Default `true`; `false` is the emergency stop for an event storm (see below).                                                                                                                      |
| `tuning.<persona>.apiMaxRetries`               | int    | Model-call retries before a run gives up. Unset = Hermes default `3`.                                                                                                                                                             |
| `tuning.<persona>.maxTurns`                    | int    | Iterations allowed in a single turn. Unset = Hermes default `90`, except `platform` (see below).                                                                                                                                  |
| `tuning.maxInProgress`                         | int    | Board-wide cap on concurrent kanban workers. Unset = operator default `2`.                                                                                                                                                        |
| `tuning.maxSessions`                           | int    | Install-wide cap on concurrent A2A session pods (1–10000). Unset = operator default `10`. Inert under `mode: today`; a separate lane from `maxInProgress`. A stream too small for it makes the provision Job refuse.              |
| `experimental.platformFrontDoor`               | bool   | **Unsupported.** Run the gateway as the Platform Agent, so chat reaches it directly. Default `false`. See below.                                                                                                                  |

`dashboardEnabled` publishes port `9119` on the agent Service, but nothing answers there: `hermes
dashboard` binds `127.0.0.1`, so a request arriving over the pod network gets connection refused.
Reaching it means getting inside the pod's network namespace — `kubectl port-forward svc/<agent>
9119:9119` on an ordinary node pool, and
[`scripts/hermes-dashboard-tunnel.py`](https://github.com/gke-labs/kube-agents/blob/main/scripts/hermes-dashboard-tunnel.py)
on a GKE Sandbox (gVisor) one, where port-forward is set up in the host-side netns and cannot see
the sandbox's listener. That script is canonical on the dashboard's access path and on why the
loopback bind is deliberate; the exec relay it uses to get inside the sandbox lives in
[`scripts/exec_tunnel.py`](https://github.com/gke-labs/kube-agents/blob/main/scripts/exec_tunnel.py),
shared with the E2E suite, which reaches the agent API the same way and for the same reason. The container's readiness probe runs `curl` against loopback for the same reason a
`tcpSocket` probe cannot work here: kubelet dials the pod IP, and nothing is listening on it.

`sessionKVApiKeySecretRef` is optional in the API but not in practice, and the `503` above is the
milder half of what its absence costs. The `k8s-event-watcher` in the `agent-api-auth` sidecar
authenticates to that same server, treats an empty `SESSION_KV_API_KEY` as fatal, and exits on every
start — so no cluster events are watched at all, while the container stays Ready and the CR
`.status` says nothing. An installation upgraded from before the key existed is the case that lands
here; add the key to the agent Secret and restart the pod.

### `spec.harness.memory`

`provider` picks which long-term memory implementation the agents load. Two ship in this repository,
and the difference between them is the whole choice:

| Value                          | Fits                       | What it costs to run                                      | What it gives                                            |
| ------------------------------ | -------------------------- | --------------------------------------------------------- | -------------------------------------------------------- |
| `multiuser_memory` _(default)_ | small or personal installs | nothing — a per-user Markdown file inside the pod         | verbatim recall of everything, no ranking or search      |
| `kube_agents_memory`           | enterprise deployments     | a Hindsight API server and a Postgres database in-cluster | ranked recall, per-user and shared scopes, consolidation |
| `none`                         | —                          | nothing                                                   | no provider; Hermes' built-in store only                 |

The split is about how the store is read, not about how good it is. The file provider concatenates
everything into the system prompt on every turn, so it is bounded by the context window; Hindsight
retrieves only what a question needs, so its cost per turn barely moves as the store grows. A fleet
of a few clusters and a handful of people will not reach the bound, and paying for a database there
buys nothing.

Anything else is passed through to Hermes untouched, so its own external providers (`hindsight`,
`mem0`, `openviking`, …) work if you bring their configuration. `none` is this API's spelling of
Hermes' empty string, which cannot be expressed here: an absent field takes the CRD default.

Only a Hindsight-backed provider reaches the specialist profiles, because only that one can be made
read-only and scoped by tag. Under any other value the specialists get no provider at all and the
Planning Agent keeps the store to itself.

`memoryEnabled` and `userProfileEnabled` are a **different** mechanism — Hermes' built-in
`MEMORY.md` / `USER.md` files, which have no per-user scoping. Both providers above replace that
store rather than supplement it, so both run with `memoryEnabled: false`.

The installer's `MEMORY_ENABLED` variable is that same built-in store and nothing more; the install
copies it into this field unchanged. It is not a master switch — whether the agent remembers
anything is `provider`'s question, and `none` is how that answers no.

The install reads only the provider: the chart's `hindsight.*` values deploy the Hindsight store
when the provider is Hindsight-backed (`--memory=hindsight`), and nothing when it is
`multiuser_memory` or `none`. See
[`docs/designs/memory.md`](https://github.com/gke-labs/kube-agents/blob/main/docs/designs/memory.md).

### `spec.harness.eventWatcher`

The `k8s-event-watcher` runs in the gateway pod's `agent-api-auth` sidecar, streams warning events
from every managed cluster, and posts each surviving incident to the pod-local Session KV server,
which opens an autonomous triage session for it. `enabled: false` stops it from starting at all.

```bash
kubectl patch platformagent platform-agent -n kubeagents-system --type merge \
  -p '{"spec":{"harness":{"eventWatcher":{"enabled":false}}}}'
```

The field has to exist in the installed CRD for that patch to mean anything, and
[Helm installs CRDs on first install but never upgrades them](https://github.com/gke-labs/kube-agents/blob/main/charts/kube-agents/README.md).
A chart-upgraded install therefore needs the CRDs applied first — worth doing ahead of the incident
rather than during it, since a client that does not send strict field validation gets the unknown
field pruned and an emergency stop that reports success and does nothing.

```bash
kubectl apply --server-side -f k8s-operator/config/crd/bases/
```

`--server-side` is not optional here. Client-side apply stores the whole object in the
`kubectl.kubernetes.io/last-applied-configuration` annotation, and this CRD is far past the 262144-byte
annotation cap, so a plain `kubectl apply` fails with `metadata.annotations: Too long` and leaves the
CRD unchanged. `make install` in `k8s-operator/` applies them the same way.

**This is an emergency stop, not a tuning knob.** It exists for the case where events arrive faster
than the agent can triage them — a fleet-wide rollout gone wrong, a node pool flapping — and the
cheapest way to get the agent back is to cut the inflow rather than chase the cards it has already
been handed. It is all-or-nothing across every watched cluster: the watcher's reason and namespace
filters live in the sidecar's entrypoint and are not exposed on the CRD, so there is no way to
silence one noisy namespace through this field. If the board is merely busy rather than swamped,
[`tuning.maxInProgress`](#specharnesstuning) is the knob for that — it throttles how many cards run at
once without losing the events.

Three consequences before you press it:

- **It rolls the pod.** The value reaches the sidecar as an environment variable, so changing it
  rewrites the pod template. During a storm that restart is usually wanted anyway — it is also what
  ends the sessions already running.
- **It stops the inflow only.** Kanban cards and sessions created from events already delivered keep
  running and still have to be dealt with on the board. It reclaims nothing either: the watcher's
  kubeconfig, token projection, and mounts stay in place, and the sidecar keeps the memory request
  sized for the informer and dedup caches it is no longer running.
- **Nothing turns it back on.** An install left with the watcher off has no incident detection at
  all, and the container stays Ready throughout — the readiness probe covers the credential proxy,
  not the watcher. Two things say otherwise: a line in the sidecar log naming the consequence, and
  the `EventWatcher` condition on the CR ([`status`](#status) below). Set `enabled: true`, or remove
  the field, to start watching again.

Unset means enabled. The watcher is how a fleet notices its own incidents, so an install that never
mentions the field — which is every install today — keeps watching, and only an explicit `false`
turns it off.

### `spec.harness.tuning`

Execution limits per agent persona, where `<persona>` is one of `default` (the Planning Agent front
door), `platform` (the Platform Agent), or `cluster` (**every** Cluster Agent), plus the board-wide
`maxInProgress` and the mode-next session-pod cap `maxSessions`.

**The per-run limits are opt-in.** The operator pins nothing of its own there: what a fleet needs
depends on its model quota and on what its agents actually do, so a deployment doing short
interactive work should not inherit limits raised for long-running batch work. Unset therefore means
whatever the profile's own `config.yaml` carries, and the `default` and `cluster` configs set no
execution limit of their own — Hermes' defaults apply there, 3 retries and 90 iterations. The
`platform` profile is the exception: the image ships `agent.max_turns: 250` in
`agents/platform/config.yaml` because the fleet audits outgrow 90, and
[Config reference](/kube-agents/reference/config/#agent) is canonical for why. Setting
`tuning.platform.maxTurns` here still wins — the overlay is merged after the image force-sync — and
removing it restores the image's value rather than Hermes'.

**`maxInProgress` is not.** Unset renders `2`, because the untuned case is the one that cannot
absorb the alternative — see [Why dispatch is capped by default](#why-dispatch-is-capped-by-default)
below. Set it on the CR to raise or lower that.

```yaml
spec:
  harness:
    tuning:
      maxInProgress: 4 # board-wide; raises the operator's default of 2
      platform:
        apiMaxRetries: 8
        maxTurns: 200
      cluster:
        apiMaxRetries: 8
        maxTurns: 150
```

Raised limits belong with the workload that needs them, not with the platform. A long-running,
quota-hungry agent plugin should ship its own tuning — as a patch its installer applies — so that a
deployment without it stays on Hermes defaults, and installing the plugin brings the limits it
requires along with it.

The GKE Stockout Investigator is the worked example:
[`agentplugins/gke-stockout-investigator/tuning.yaml`](https://github.com/gke-labs/kube-agents/blob/main/agentplugins/gke-stockout-investigator/tuning.yaml)
records the reasoning behind each number, and its `install.sh` applies it.

The keys are personas rather than profile names because the profiles are not all known when the CR
is written: Cluster Agent profiles are scaffolded at runtime, one per managed cluster, with
generated names like `cluster-<project>-<cluster>-<region>`. `cluster` therefore applies to all of
them at once — including ones onboarded after the pod last started, which pick the limits up as they
are scaffolded.

Both limits matter because they fail the same way, and it is not an obvious way. A run that
exhausts either stops mid-task without ever calling a terminal kanban tool. The card is charged a
`timed_out` failure whose error text names how the turn ended — `Iteration budget exhausted (N/M)`
for `maxTurns`, `turn_exit_reason=all_retries_exhausted_no_response` for `apiMaxRetries` — and
retrying re-runs into the same wall, so read that text and the upstream error rate before suspecting
the worker. One `apiMaxRetries` exhaustion is handled differently: when the retries were spent on
provider rate limits (429s), the worker blocks its own card instead, kind `transient`, with the
provider's error text as the reason; `kanban_unblock` it once the quota window has passed. A
second rate-limit block after an unblock sends the card to `triage`. An exit like this that reaches
the dispatcher unexplained surfaces instead as a **protocol violation**, which describes the
symptom and hides the cause; the image narrows that window in
[`deploy/docker/patches/kanban_guardrail_exit.py`](https://github.com/gke-labs/kube-agents/blob/main/deploy/docker/patches/kanban_guardrail_exit.py).

Sizing notes: `maxTurns` is consumed mostly by repository exploration, so scale it against how much
the agent has to read rather than how complex the request is. `apiMaxRetries` exists because
Hermes' default of `3` assumes an interactive session where a human retries; a background worker
has nobody to retry it, so a transient burst of upstream 503s simply ends the run. A 429 retry
waits out the delay Google states in the error body (up to 600 s) rather than a few seconds, so a
burst shorter than that window is survived; see
[`deploy/docker/patches/rate_limit_retry_delay.py`](https://github.com/gke-labs/kube-agents/blob/main/deploy/docker/patches/rate_limit_retry_delay.py). Raising
`maxTurns` interacts with `maxInProgress`: a worker doing the work holds its slot for the whole
task and there are only `maxInProgress` of them, so raising one is a reason to reconsider the other.
A coordinator waiting on work it fanned out is the exception — see
[why dispatch is capped](#why-dispatch-is-capped-by-default).

#### Why dispatch is capped by default

A kanban worker is not a coroutine. It is a full `hermes … kanban task` process — a few hundred MiB
resident once its MCP proxies are up, and alive for as long as the task takes, which for an incident
triage is minutes rather than seconds. Uncapped, the dispatcher starts one per queued card, and a
burst of cluster events queues them faster than they retire.

What follows is invisible in the places you would look. The cgroup OOM killer takes a worker, not
PID 1, so there is no container restart and no Kubernetes event; the pod stays `Running` and the
only trace is `pid not alive` in the kanban ledger. The dispatcher's retry budget is 1, so the card
is stranded rather than re-dispatched, and the work it stood for is never done — a triage report
that simply never arrives, with nothing anywhere reporting a failure.

`2` is a floor for a deployment that has not measured itself, not a recommendation. It is chosen to
hold on the smallest pod anyone runs, and because the cost of being wrong is asymmetric: too low
delays a delegated task, too high loses it silently. Raise it once you know your worker footprint
and your model quota — that quota is the other shared resource, and for most deployments it binds
before memory does.

The cap counts running cards, not resident processes, and one case makes those differ: a coordinator
waiting on work it fanned out is discounted, or it would hold the slot its own children need
([`kanban_scheduling.py`](https://github.com/gke-labs/kube-agents/blob/main/deploy/docker/patches/kanban_scheduling.py)).
Peak memory is therefore the cap plus however many coordinators are waiting, held down by the
dispatcher's memory-pressure guard rather than by this number.

### `spec.harness.experimental`

Opt-in switches with no compatibility promise. A field here may change meaning, change its default,
or be removed outright in any release; an install that depends on one is expected to re-check it at
every upgrade. Fields live here while the question they answer is still open — once it is settled
the switch either graduates into a supported block or goes away.

#### `platformFrontDoor`

Makes the **Platform Agent** the profile the Hermes gateway runs as, so a chat message is handled by
the agent that has the tools instead of arriving at the Planning Agent, which delegates through the
router MCP server and the kanban board.

```bash
kubectl patch platformagent platform-agent -n kubeagents-system --type merge \
  -p '{"spec":{"harness":{"experimental":{"platformFrontDoor":true}}}}'
```

Three things change while it is on:

- The gateway container runs `hermes --profile platform gateway run`. Above one replica the container
  still runs `leader_elect.py`, which reads the same choice from `HERMES_GATEWAY_PROFILE` and builds
  that command line for the process it supervises.
- `profile-platform.overlay.yaml` gains the three profile-shaped things only the `default` profile
  carried before: the toolsets each chat platform key resolves, the ingress plugins, and the
  `kanban` block. The adapters themselves are not copied — the managed scope at `/etc/hermes` is
  machine-global, so `platforms.*` and `display.platforms` already land on this profile. `kanban`
  does have to follow the gateway, because the dispatcher and the notifier run in the gateway
  process and read their settings from its own home; that is what keeps
  [`tuning.maxInProgress`](#specharnesstuning) applying. The board itself does not move — Hermes
  anchors `kanban.db` at the shared root rather than the active profile, deliberately, so the
  dispatcher/worker handoff survives — so cards in flight are unaffected by the flip.
- The entrypoint stops force-syncing `profiles/platform/config.yaml` from the image and back-fills
  it instead, on the same terms as the `default` profile's own file (with one exception, the
  remote MCP `User-Agent`), so `/sethome` and `monitoring.install_id` survive a restart — see
  [How config reaches each profile](#how-config-reaches-each-profile).

Setting the field back to `false` reverses all three. The overlay records what it applied, so the
keys are unapplied rather than left behind, and the force-sync resumes.

**What it costs.** The Planning Agent's lockdown is its whole reason for existing: a front door
with three toolsets, so an inbound message cannot reach the full Platform Agent tool surface before a
card and a worker turn have framed it. With this on, an inbound message reaches that surface
directly. The lockdown is deliberately **not** copied onto the platform profile — copying it would
leave the Platform Agent unable to do the work the flag exists to let it do.

**Known limits.**

- One gateway means one profile, so this is not additive. While it is on, the Planning Agent persona sees
  no chat at all and the router MCP path is simply unused. Kanban delegation is not: the front door
  keeps `dispatch_in_gateway`, so it can still hand a card to a spawned worker — it just does so as
  the agent that could also have done the work itself.
- `gateway.multiplex_profiles` is still off, so a `/p/<profile>/` prefix on an API request is
  ignored. Those requests now land on the Platform Agent rather than the Planning Agent.
- The `hermes dashboard` sidecar is deliberately left on the `default` profile, so the dashboard
  shows that profile's sessions while the front door is the platform one.
- **An [`AgentPlugin`](./agentplugin-crd.md) without a `spec.targetProfile` does not follow the
  gateway.** The two halves that decide whether it loads at all stay on the `default` profile: the
  image volume is mounted at `$HERMES_HOME/plugins/<name>`, and the name is added to that profile's
  `plugins.enabled`. With the flag on, the gateway is homed at `profiles/platform` and reads
  neither, so the plugin never registers — silently, since nothing errors, and its `platforms:`
  block does arrive here through the machine-global managed scope, configuring an adapter that has
  nothing to configure. The `pubsub-platform` adapter and the `gke-stockout-investigator` alert
  route that depends on it are both affected. Setting `spec.targetProfile: platform` moves the
  plugin and its `platform_toolsets` across.
- **The `default` profile's cron roster stops ticking.** Hermes binds its cron ticker to one
  `HERMES_HOME` — the job store, the execution ledger and `.tick.lock` all resolve from the gateway
  process's own home — so the one roster that ticks becomes `profiles/platform/cron/jobs.json`. The
  Platform Agent's own watchdogs therefore tick natively, which is the upside; the cost is that the
  four jobs on the `default` roster never come due. Those are `cluster-agent-reconcile`, which
  scaffolds a profile for a newly onboarded cluster and prunes one for a deleted cluster;
  `bootstrap-inventory-scan` and `bootstrap-inventory-delivery`, which are first-run onboarding; and
  `profile-cron-tick`, the only thing that ticks a **named** profile's own store — so every
  `cluster-*` roster goes quiet with it. Nothing errors and nothing is logged: a job that is never
  ticked simply stays `scheduled` with a `next_run_at` in the past.
- **Per-user memory does not follow the gateway either.** The front door's provider is
  `multiuser_memory`, committed in `agents/chat/config.yaml` and reaching the `default` profile
  alone. The platform profile is configured as a specialist instead: `read_only: true` from the
  image, and a `memory.provider` its overlay blanks unless
  [`spec.harness.memory`](#specharnessmemory) names a Hindsight-backed store. With the flag on the
  front door is that profile, so chat has no recall, no per-user profile and no retention — while
  `memory` stays in the profile's toolsets advertising all three. The keys cannot simply be copied
  across, because `profiles/platform/config.yaml` is shared: it is also the home of every
  kanban-spawned specialist and every job on the platform cron roster. Lifting `read_only` there
  would let a specialist write the shared corpus, and un-blanking the provider would collapse those
  writes into one anonymous bucket, since a specialist carries no gateway identity to scope a
  per-user store by. Carrying memory to the front door needs those settings to be per-process
  rather than per-profile.
- **First-run onboarding does not follow the gateway.** Its two jobs are on the `default` roster the
  bullet above stops ticking, and its greeting hook — the `bootstrap_onboarding` plugin — resolves
  its once-per-deployment markers from the gateway's home, so on the platform profile it would greet
  an already-onboarded install and promise a report nothing can deliver. The operator therefore
  leaves that plugin off the front door deliberately. Bring an install up with the flag off, let
  onboarding finish, then turn it on.
- **A home channel set with `/sethome` stays on the profile it was set on.** The operator renders no
  `home_channel` of its own, so on an install that did not populate the CR's Google Chat or Slack
  `homeChannel` the value lives only in the config file the gateway last wrote. Flipping the flag
  changes which file that is, and nothing carries it across. The Platform Agent's own `deliver: all`
  watchdogs then tick in-process, where the delivery target is read from the environment alone —
  so every scheduled report posts nowhere while chat replies stay healthy and the install looks
  fine. Either populate `homeChannel` on the CR before flipping, or re-run `/sethome` after.
- **`chat_message_audit` stops recording.** It is a hook rather than a plugin, and hooks are only
  ever copied into the root home — nothing puts them on a named profile, and Hermes reads
  `$HERMES_HOME/hooks` and returns silently when the directory is absent. `tool_call_audit` is a
  plugin, is enabled on this profile and still records the inbound message with its session and
  pseudonymised user id, so what is actually lost is the record carrying the agent's **response**
  text. Response content remains in the session store.
- **The platform profile's `config.yaml` stops being restored from the image on every restart.**
  Handing the file to the agent is the point — that is what lets `/sethome` and
  `monitoring.install_id` survive — but the same change means a key the running agent writes there
  is not reverted at boot. Keys the image adds still arrive through the back-fill; keys already in
  the file stay as they were last written, except the remote MCP `User-Agent`, which follows the
  image ([below](#how-config-reaches-each-profile)). Operator-owned settings are unaffected: they
  come from the overlay and the `/etc/hermes` pins, both re-applied every boot.
- Cluster profiles that already exist are otherwise unaffected — their config, skills and
  scaffolding on disk are unchanged and they keep working. What stops is the scheduled work above,
  which includes `cluster-agent-reconcile`, so a cluster onboarded while the flag is on gets no
  profile until the flag goes back off.

#### `shellSandbox`

- `enabled` — the agent's shell runs in a StatefulSet of its own, reached over SSH with the keypair in the agent's credential Secret (`SANDBOX_SSH_PRIVATE_KEY` and its public half in `<name>-shell-authorized-keys`). **This is not a toggle: `false` is refused** with `Degraded`/`ShellSandboxCannotBeDisabled`, which changes nothing about the running workload. Absent or `true` are the same thing. With no keypair the sandbox Pod cannot start at all — the Secret it mounts is not optional — so the operator reports `Degraded`/`ShellSandboxKeysMissing` rather than leaving the Pod in `ContainerCreating` with the reason only in a Pod event. `install.sh`, `upgrade.sh` and the Terraform composition generate the pair; a bare `helm install` that passes none, or a kustomize install (INSTALL.md Method 2) that skipped its Step 2, reaches that state.
- `image` — overrides the sandbox image. Empty takes the operator's default.
- `runtimeClassName` — runs the sandbox Pod under a sandboxed container runtime, `gvisor` being the one GKE offers. Unset by default. Separate from [`spec.deployment.availability.runtimeClassName`](#specdeployment), which governs the agent Pod: that Pod holds SQLite databases whose WAL mode gVisor corrupts on the gofer-backed mount, and setting the agent Pod's field pins Hermes' own databases to the DELETE journal mode (the Session KV store is not covered; see `availability.runtimeClassName` under [`spec.deployment`](#specdeployment)), while the sandbox Pod holds none — so an install can sandbox the untrusted Pod without sandboxing, or slowing, the trusted one. On GKE Standard the cluster needs a node pool created with `--sandbox type=gvisor`; Autopilot ships the RuntimeClass natively. A RuntimeClass the cluster does not have leaves the CR `Degraded` naming it, rather than a Pod sitting `Pending`.

The GitHub-writing skills hand the credential broker file content and a commit message rather than a directory both sides mount, so the agent never holds a `.git`. There is no field for it: the broker keeps the checkout on its own state volume, which closes git's config-driven exec surface — a hook, a pager, a `filter.*.clean`, an `ext::` transport — and an install that could turn that off would be choosing to keep it open.

The credential broker is a Deployment of its own, never a container of this Pod, so a proxied command reaches it over a ClusterIP Service and nothing crosses as a path: a document travels on stdin, a kubeconfig as a GKE context name, a commit as file content. [`spec.security.workloadIdentityFederation`](#specsecurity) is optional hardening on top of that, not what places it.

## `spec.deployment`

Abstracts the pod/deployment configuration. The controller synthesises a `Deployment` from these plus the workspace ConfigMaps. Available fields:

- `image` — container image repository.
- `tag` — image tag. Applies only when `image` is set without a tag or digest, falling back to `latest` there; when `image` is omitted, the operator's default platform-agent version applies instead.
- `imagePullPolicy` — one of `Always`, `Never`, `IfNotPresent`. Default `IfNotPresent`.
- `imagePullSecrets` — Secrets in the agent's namespace holding registry credentials, as
  `- name: <secret>` entries. Referenced, not created: each must exist before the pod is
  scheduled. Pod-scoped, so it covers the agent, both injected sidecars, anything in
  `initContainers`/`sidecars`, and the OCI image volumes `AgentPlugin`s mount.
- `browserArgs` — extra command-line args for the agent's browser (e.g. `--no-sandbox`).
- `availability.runtimeClassName` — pod runtime class (e.g. `gvisor`) for the agent Pod. Nested under `availability` alongside `replicas`, `nodeSelector`, `tolerations` and `affinity`. When set, the managed config also carries `database.journal_mode: delete`, and the entrypoint converts Hermes' existing databases out of WAL once at start-up — `state.db`, `kanban.db` and the cron, project, evidence, response, memory and Discord stores Hermes opens through the same journal-mode helper: a sandboxed runtime serves the data volume over a gofer mount that accepts SQLite's WAL mode and then corrupts it ([#610](https://github.com/gke-labs/kube-agents/issues/610)). The Session KV store (`session_kv.db` on the `system-metadata` volume) sets WAL itself and is not covered by either. Clearing the field drops the pin, and the databases return to WAL on their next open.
- `env` — additional container environment variables.
- `initContainers` / `sidecars` — standard init and sidecar containers. A `volumeMounts` entry naming a reserved volume is refused at admission, the same as `extraVolumeMounts`. That is the route that needs no user-authored volume at all, and so the one the name reservation exists for; see [Reconcile behavior](#reconcile-behavior).
- `extraVolumes` — custom volumes for the main container. Both halves of the reservation apply: a reserved name, and a source that would carry the A2A bus credential, are each refused at admission; see [Reconcile behavior](#reconcile-behavior).
- `extraVolumeMounts` — custom mounts for the main container. A mount has no source, so only the name half applies: an entry naming a reserved volume is refused at admission.
- `sidecarVolumes` — custom volumes for the sidecar containers. Same reservations as `extraVolumes`.
- A `hostPath` entry on either volume list is refused at admission, and on an install where the
  webhook did not run (the Helm chart ships it off, and one enabled through the chart fails open at
  its default `failurePolicy: Ignore`) the controller leaves it out of the Pod template at render,
  together with every `volumeMount` naming it on the agent container, the dashboard container, and
  the CR's own `sidecars`/`initContainers`. The rest of the template renders as written, and the
  `VolumesDropped` condition ([`status`](#status) below) names what was left out — as many entries
  as fit in 4096 bytes, then a count of the rest, and an entry longer than that on its own cut with
  `...`.
  The budget is the controller's, not the schema's: a condition message may hold 32768 characters,
  and the list stops well short of it because `kubectl describe` is where it is read and the first
  few entries are what anyone acts on. Volume names and host paths are the author's, and nothing
  bounds their length or their number, so an unbounded list would be one spec away from failing
  the whole status write — `Ready`, the phase and every other condition with it.
- `podAnnotations` — annotations applied to the generated pod template.
- `scaleToZero` — when `true`, scales the deployment to 0 replicas (idle cost saving).

Default image: derived dynamically from the operator's container image at runtime via the `OPERATOR_IMAGE` env var on the controller manager (e.g. `ghcr.io/gke-labs/kube-agents/platform-agent:<version>`), overridable operator-wide via the `PLATFORM_AGENT_IMAGE` env var on the controller manager (see [Docker images § Private / custom registry](/kube-agents/deploy/docker-images/#private--custom-registry)), and falling back to `ghcr.io/gke-labs/kube-agents/platform-agent:latest` if neither is set. Rebuild with `make dev-rebuild-agent ARGS="platform"` for local iteration.

`imagePullSecrets` has the same operator-wide form, `IMAGE_PULL_SECRETS` on the controller manager, taking comma-separated Secret names. It differs from the image overrides in one way: a CR that sets `imagePullSecrets` **replaces** the operator's list rather than merging with it, so an agent that names its own registry identity is stating it completely. See [Docker images § Registry authentication](/kube-agents/deploy/docker-images/#registry-authentication).

## `spec.security`

- `serviceAccountName` — the KSA the pod runs as. `kubeagents-platform-agent` by convention.
- `serviceAccountAnnotations` — passed through to the KSA. Typically holds `iam.gke.io/gcp-service-account` for Workload Identity binding.
- `workloadIdentityFederation` — `audience` (the pool provider's full resource name) and `serviceAccountEmail` (the GSA to impersonate). Set both, or neither: a half-filled block is read as absent. Optional hardening: with it set, the credential broker takes its GCP identity from a projected token file in its own Pod rather than from the metadata server answering a ServiceAccount the gateway also runs as. Without it the broker works, and the gateway keeps an ambient cloud identity. The chart sets both from `platformAgent.security.workloadIdentityFederation`; nothing creates the pool, and those commands are in [`designs/agent-shell-sandboxing.md`](https://github.com/gke-labs/kube-agents/blob/main/docs/designs/agent-shell-sandboxing.md#setting-up-the-pool).

- `egressPolicy` — `None` (default) or `Allowlist`. `Allowlist` renders a default-deny egress NetworkPolicy on the agent Pod, with the link-local metadata server's credential API left off the allowlist (the address is permitted on port 53 alone, where it is the Cloud DNS for GKE resolver). **It blocks nothing today, and cannot — unless `networkPolicy.enabled: false` has withheld the gateway policy on a Helm install, in which case this becomes the Pod's only policy and default-denies for real on an enforcing CNI (a Kustomize install still carries the static `platform-agent-core-egress` set over the same Pod).** Everywhere else: adding a policy is monotone — policies selecting one Pod are unioned and the API has no deny rule — and the same Pod is already selected by `<name>-gateway-netpol`, which permits `169.254.169.254/32` on TCP 80 and on port 53, the discovered metadata-daemon port (`988` by default) to both link-local metadata addresses, and TCP 443 to `0.0.0.0/0` minus the private ranges. So enabling this widens the Pod's permitted egress (by the broker on 8765, and by the managed collector namespace on 4317/4318 when the agent is not exporting telemetry) and narrows nothing; what you get is an auditable object, not enforcement. The broker has left the Pod, but the gateway still shares its annotated ServiceAccount, so the gateway policy cannot stop permitting the metadata path yet; narrowing it — after `workloadIdentityFederation`, or after the broker gets a ServiceAccount of its own — is what makes this field a control, and the capability cost lands then. It will also do nothing on a cluster whose CNI does not enforce NetworkPolicy, which the operator cannot detect. See [Credential isolation § Denying the sandbox the metadata server](/kube-agents/reference/credential-isolation/#denying-the-sandbox-the-metadata-server).
- `egressAllowlist` — tunes `egressPolicy: Allowlist`. `controlPlaneCIDRs` permits the Kubernetes API server on 443 (absent by default, which costs the agent container its API-server connection; a bare address — what the documented `gcloud` command emits for a public endpoint — is widened to a single-host prefix); `extraRules` are NetworkPolicy egress rules appended verbatim — except that a rule the operator will not render (an `ipBlock` reaching a metadata address, including in IPv4-mapped form; a rule with no `to` peers; a peer naming no `ipBlock`, `podSelector` or `namespaceSelector`; an invalid CIDR, in `cidr` or `except`; an `except` outside its `cidr`, which the API server would reject in a way that wedges the whole reconcile; or `controlPlaneCIDRs` wider than /16 IPv4 or /32 IPv6) is refused loudly rather than silently dropped: the agent goes `Degraded`/`EgressAllowlistRefused`, the workload stops being reconciled, and the policy keeps being rendered minus the refused destinations.
- `scopedServiceAccounts` — the cluster→service-account mapping for the [scoped service account pool](/kube-agents/reference/security-and-iam/#the-scoped-service-account-pool). Absent (the default) the pool is disarmed and the broker runs on the agent's own identity. A non-empty list arms it: the operator renders the mapping into the credential-proxy ConfigMap, mounts it read-only, and sets `CREDENTIAL_PROXY_SCOPED_SA_POOL=1` on the broker, which then mints a short-lived token for the account a request's target cluster maps to and refuses a cluster with no entry (`403`, rule `gcp.scoped-sa.unmapped-scope`) rather than falling back to the wider credential. The list is keyed on `(projectId, location, clusterName)` so a duplicate cluster is rejected at `kubectl apply` rather than crashlooping the broker at startup; entries are bounded at 100 and each component is pattern-validated. **Leave it empty for now**: pool members currently hold no IAM grant, so an armed pool turns every mapped-cluster read into a `Forbidden` — see the caution on the pool section.

The Workload Identity target GSA (`kubeagents-platform-gsa@<project>.iam.gserviceaccount.com`) is created and bound by the [`kube-agents-iam` Terraform module](https://github.com/gke-labs/kube-agents/tree/main/terraform/modules/kube-agents-iam) with one of these permission sets:

- `read-only` (default)
- `custom` (roles supplied via the installer's `--custom-roles`, the composition's `project_roles`)

## `spec.scope`

Optional. Which GCP projects, beyond the one the agent runs in, the hourly Cluster Agent reconcile
enumerates for GKE clusters, and which projects and clusters it leaves unmanaged. Not yet surfaced
in the Helm chart, which owns the CR on a chart install: until the chart gains the values, the field
is set by editing the `PlatformAgent` directly. Absent, the reconcile lists the management project alone, every cluster there getting a Cluster Agent profile, keeps the last declaration's exclusions and marks nothing newly `retiring` (a project an earlier block already marked `retiring` is still pruned on a clean run); an empty `projects` list in a present block drops the projects an earlier block declared, over two clean runs. The management project is always in scope and cannot be excluded.

```yaml
spec:
  scope:
    projects: # explicit project IDs, in addition to the management project
      - payments-prod
      - payments-staging
    folders: # numeric folder IDs, every project beneath them at any depth
      - "123456789012"
    exclude:
      projects: # project IDs or shell-style globs, dropped after resolution
        - "*-sandbox"
      clusters: # one cluster each, by the full triple
        - projectId: payments-staging
          location: us-central1
          clusterName: scratch-cluster
```

- `projects` — project IDs whose clusters get profiles. The agent's service account needs the
  read roles in each one (`roles/container.clusterViewer`, `roles/container.viewer`,
  `roles/compute.viewer`, `roles/monitoring.viewer`, `roles/logging.viewer`,
  `roles/iam.securityReviewer`, the read subset of the `read_only_roles` the Terraform composition binds in the management project). No installer path grants them yet: until the Terraform
  composition gains a scope input, grant them by hand in each project. A project it cannot list is reported as `denied` (no role left that grants `container.clusters.list`; `roles/iam.securityReviewer` alone keeps a project listable, so a project reads `denied` only once every such role is gone), `api-disabled` (GKE API off in that project) or `unreachable` (anything else, including a listing that did not finish within the run's listing budget) and its existing profiles are kept. Each list is capped at 100
  entries, and the run lists at most 100 projects in total, the management project included; an
  explicit project past that reads `over-cap` and is likewise kept but not listed.
- `folders` and `organizations` — numeric IDs of GCP folders and organisations. The design recommends folders until the scoped service account pool grants authority: a folder binding reaches every project beneath it, an organisation binding every project in the organisation, including ones nobody meant to manage, and one service account holds the read roles across all of them ([Security & IAM](/kube-agents/reference/security-and-iam/#roles-per-set)). Each is resolved on every run with one Cloud Asset Inventory call (`gcloud asset search-all-resources --scope=folders/<id> --asset-types=container.googleapis.com/Cluster`), which names every GKE cluster in every project beneath it, at any depth, including projects created since the last run; no per-project listing follows for them. The asset index is what answers, not Resource Manager: a project moved into or out of a folder appears under its new parent once the index has caught up, which after a move can lag by more than an hour, and until then it reads as absent from the container. Because the index and not the declaration dropped it, a project that was reached through a still-declared container (whether or not it was also named in `projects`, so dropping the explicit entry while the project moves into the folder costs nothing, and the same holds when the folder is declared in the edit that drops the entry: a container the previous run did not know is what marks that edit, and the same day's grace applies) and is absent from the index is kept, listed under `unmanaged` with that reason and the time it was first found absent (`absentSince` on its row), and is back in scope the run the index places it again. It retires sooner when the declaration speaks, by the container leaving the CR or an `exclude.projects` entry naming it, and otherwise once it has been absent for a day, over the ordinary two clean runs: that is what retires a project that was deleted or moved under a parent the CR does not declare, since a deleted project answers 403 to `describe`, never NotFound. A project dropped from `projects`, or whose folder leaves the CR, in the same edit that declares the folder it moved into is held that day too, since the index may not place it yet. A rollback to an operator that predates these fields renders a declaration without them, which the reconcile likewise reads as saying nothing about containers, so their members are kept. The agent's service account needs `roles/cloudasset.viewer` and the read roles above on the container, and `cloudasset.googleapis.com` enabled in the management project; no installer path binds them yet, so grant them by hand. A container the run cannot read (`denied`, `api-disabled`, `unreachable`) is frozen: the members the previous snapshot reached through it are carried forward under that outcome, nothing is created under them, and the scope prune stays off for the run. A container whose members would cross the 100-project cap reads `over-cap`: the members the run just resolved are carried reading `over-cap`, with nothing created under them, and because the run holds the full member list the prune is not held back. A project under two declared containers takes the live listing when either resolves, whichever sorted first, unless the cap is already reached. A member whose per-cluster call answers 403 reads `denied` for the run, unless it is also explicit or the management project, whose own listing decides. The snapshot's `containers` array lists each container with its outcome and a project count: the projects the lookup returned for `ok` and `over-cap`, the members carried forward from the previous snapshot for a failed lookup, and a member project's `via` names it (`folders/<id>`), or several sources when more than one produced it. A container whose members would take the run past the resolved-set cap of 100 reads `over-cap` with nothing created under it; its members are carried but not listed, and do not count against the containers after it, so a small folder declared beside a large one still lists. What an operator above the cap does today: `exclude.projects` globs subtract members before the count, so a folder that carries sandboxes or archives is trimmed by pattern; or declare the sub-folders that hold the clusters, each counted on its own. A cap declared on the CR, for an estate of a few hundred projects, is planned and not yet shipped.
- `exclude.projects` — IDs or globs matched against every resolved project ID. An entry that matches
  the management project is ignored and recorded in the snapshot, never applied.
- `exclude.clusters` — single clusters by `projectId`, `location` and `clusterName`, because a
  cluster name is unique only within a project and location. This replaces the
  `RECONCILE_EXCLUDE` environment variable, which matched bare names across every project and keeps
  working for one release alongside it.

Rolling the release back past these fields: remove the whole `spec.scope` block from the CR first and re-add it after, because a `cluster_agent_reconcile.py` from before `folders` and `organizations` reads the same `scope.json`, ignores those keys, and finds every container member in its previous snapshot but not in its resolved set, which is the ordinary two-run retire; a CR without the block marks nothing newly `retiring`. On a default install the agent image follows the operator's, so an operator rollback is an agent rollback too. The operator renders the block as `scope.json` in the agent's config ConfigMap, mounted read-only at `/etc/kube-agents/scope.json`, so editing it moves the config hash and rolls the pod. The file records whether the CR carries a `scope` block at all: a CR without one declares nothing, so the reconcile lists the management project alone and marks nothing newly `retiring` (a project an earlier block already marked `retiring` is still pruned on a clean run), keeping the last declaration's exclusions and carrying its projects, because a block goes missing on its own when a CR write passes an older operator's webhook. To drop projects, empty `projects` and keep the block. Each reconcile run (other than `--dry-run`) writes what it resolved to `fleet_scope.json` beside the profiles on the data volume: the declaration the run applied, or on a run that could not read it the last one read (`declared`), every project with its outcome (`ok`, `denied`, `api-disabled`, `unreachable`, `over-cap`), the profiles whose project the scope never produced or whose prune waits for a clean run (`unmanaged`; a profile whose identity could not be read is kept but appears under the report's `skipped_no_identity`, and under `profiles` once an earlier run has read its identity), any exclusion it declined to honour (`ignoredExcludes`), every folder and organisation with its outcome and member count (`containers`, with `resolver` reading `asset-inventory` once one is declared and `explicit` otherwise), and each profile's project as read this run or, for a profile whose identity could not be read, as last read (`profiles`), which is what keeps such a profile attributed while it stays on the volume. A cluster named in `exclude.clusters` loses its profile on the next run, unconditionally, as `RECONCILE_EXCLUDE` always did. A project removed from the scope, or newly matched by an `exclude.projects` entry, has its profiles pruned over two clean runs: a run is clean when no project came back `unreachable`, every folder and organisation resolved `ok` or `over-cap`, the management project resolved, listed its own clusters and is the one the previous snapshot named, and the scope file was readable. The first clean run that finds a previously in-scope project absent records it as `retiring` in `fleet_scope.json` and keeps its profiles (listed under `unmanaged`); the next clean run deletes them, so a scope edit reverted before that run costs nothing. A management project that changes identity (`RECONCILE_PROJECT` re-pointed, or the metadata server naming another project) marks the old project `retiring` on the run the change is seen, once the new project has listed its own clusters, and the next clean run prunes, unless `projects` names the old project and no `exclude.projects` entry matches it; an answer from the gcloud config fallback that disagrees with the previous run is treated as unresolved instead: nothing is created or retired under the management project until an authoritative source, or a fallback answer that matches the previous run, names it. A run that cannot read the declaration is not a clean run and prunes nothing; one that reads a CR without a scope block keeps the exclusions of the last declaration it read and marks nothing newly `retiring`, so a rollback to an operator without the field neither re-onboards an excluded cluster nor, on the roll forward, prunes a project the rollback's own CR write dropped. A profile the scope never produced is kept and listed, which is also what a project dropped while `fleet_scope.json` was lost between the two runs becomes: declare and drop it again to retire it. The onboarding sweep names any folder or organisation the reconcile could not resolve and, on an install whose scope resolved more than one project, any project it could not list, so a partial roster reads as partial; an install with one project and no container renders the sweep prompt it rendered before scopes existed.

## `spec.telemetry`

- `otlpEndpoint` — the OTLP/HTTP collector **base** URL (no `/v1/traces` suffix; the exporters append their own per-signal path). Up to 2048 characters, `http://` or `https://`.

Optional, and omitting it is the point: with the field absent the operator discovers an in-cluster collector, falls back to GKE Managed OpenTelemetry when it cannot establish what the cluster has, and disables export altogether when discovery finds no collector (`otlpEndpointSource: None`). Setting it pins the endpoint and suppresses discovery. The full precedence ladder, the discovery order, and the Helm value that drives LiteLLM and the NetworkPolicy alongside this field are on [Deploy → Telemetry](/kube-agents/deploy/telemetry/#pointing-at-your-own-collector).

## `spec.networkPolicy`

Configures the operator-generated egress `NetworkPolicy`.

- `enabled` (bool, optional) — toggle operator-managed NetworkPolicy generation. Default `true` (unset
  means on). Setting `false` stops generation and deletes the policies the operator manages for this
  agent: `<name>-gateway-netpol`, the `<name>-fqdn-netpol` `FQDNNetworkPolicy`, and the shared
  `litellm-policy` (if LiteLLM is present). Deletions check ownership / managed labels first, so a
  policy that the operator did not create survives. To opt only LiteLLM out of operator NetworkPolicy
  management while keeping the agent pod's policies managed, set the annotation
  `kubeagents.x-k8s.io/enable-litellm-network-policy: "false"` on the `PlatformAgent`. When opted out, the operator deletes any managed copy of `litellm-policy`, leaving LiteLLM unselected (fail-open) unless a replacement policy is provided and managed out-of-band.

  **Upgrade note:** when upgrading from a chart version that shipped the static `litellm-policy`, Helm
  prunes the static policy on the first upgrade (unless the live object already carries
  `helm.sh/resource-policy: keep`, in which case Helm retains it and the operator adopts it). The operator recreates it once the new operator pod
  rolls out, acquires leader election, and reconciles. LiteLLM is unselected (fail-open) during this
  operator rollout window. To eliminate this window on an existing cluster, annotate the live policy before upgrading (`kubectl annotate netpol litellm-policy helm.sh/resource-policy=keep -n <namespace>`); Helm will retain the policy across the upgrade and the operator will adopt it via Server-Side Apply. Alternatively, pre-roll the new operator image before running `helm upgrade` to narrow the window to controller watch latency (~1s), or manage the
  policy out-of-band during the transition via `litellm.networkPolicy=false`. The same window
  opens on a fresh default install, with no live object to annotate; the chart README's
  [upgrade notes](https://github.com/gke-labs/kube-agents/blob/main/charts/kube-agents/README.md#upgrade-notes-static-to-dynamic-networkpolicy)
  say how to cover it. Going the other way,
  from operator-managed back to the static copy, needs a handoff first — see
  [Handing `litellm-policy` back to Helm](https://github.com/gke-labs/kube-agents/blob/main/charts/kube-agents/README.md#handing-litellm-policy-back-to-helm)
  in the chart README.

- `dnsClusterIPs` ([]string, optional, max 8 items) — pins the cluster DNS Service ClusterIPs in
  rule 1 of both the agent gateway policy (`<name>-gateway-netpol`) and the LiteLLM gateway policy
  (`litellm-policy`), suppressing dynamic discovery from `kube-system/kube-dns`. Each entry is a bare IPv4 or
  IPv6 address with no prefix. Admission bounds the IPv4 octets and rejects the leading-zero form
  (`010.96.0.10`) that Go's `net.ParseIP` refuses, so the usual typos are apply-time errors; a malformed
  IPv6 literal can still get past it, in which case the operator drops the entry and falls back to
  discovery, and says so in its log.
- `metadataDaemon` (object, optional) — pins the node-local cloud metadata daemon IP in rule 3 of
  `<name>-gateway-netpol` and rule 4 of `litellm-policy`. Its
  one field, `endpoint`, is required within it, so `metadataDaemon: {}` is rejected; an explicit
  `endpoint: ""` suppresses the post-NAT metadata rule entirely in both policies, for datapaths without a post-NAT daemon. Leave it
  unspecified to let the operator discover the container port from the `kube-system/gke-metadata-server`
  DaemonSet on port `metadata-server` (promoting `metadataDaemonIPSource` to `Discovered`). If undiscoverable,
  it falls back to `169.254.169.252` on port `988`. Overriding the endpoint explicitly opts out of port
  discovery and uses port `988`.
- `additionalEgress` ([]EgressRule, optional, max 32 items) — appends custom CIDR and port egress
  rules to the generated agent gateway policy (`<name>-gateway-netpol`). (Does not apply to `litellm-policy`.) A peer CIDR broader than `/12` (IPv4) or `/48` (IPv6) is rejected at
  admission, so that a caller-supplied range cannot be widened into an unrestricted egress bypass.
  One shape gets past that check and is dropped by the operator instead: an IPv4-mapped IPv6 block
  such as `::ffff:0:0/96` is a 128-bit prefix by every textual measure, so it clears the IPv6 floor,
  and the operator re-measures it against the IPv4 floor once it has collapsed it to the IPv4 block
  it means. An `except` block that is not a strict subset of its peer's CIDR is dropped too — the API
  server rejects the whole policy for one that is not — and a rule left with no usable peer is
  dropped whole — a rule carrying ports and no peer would otherwise permit egress to every
  destination. All three are logged, so the operator's log is where a rule that did not take effect
  explains itself.

  A rule's `ports` list is optional, and omitting it is not one of those drops: a rule with peers
  and no ports permits **every** port to those peers, which is what a NetworkPolicy egress rule with
  an empty port list means. Nothing is logged, because nothing was dropped. List the ports unless
  that is what you want.

  A peer's `except` entries may be written bare (`10.0.1.5`, meaning a `/32`) as well as with a
  prefix, the same as `cidr`. Unlike `cidr` there is no prefix floor on them, because an `except`
  has to be a strict subset of its peer to be kept at all.

Annotations (`kubeagents.x-k8s.io/dns-cluster-ip` and `kubeagents.x-k8s.io/metadata-daemon-ip`) remain
available as escape hatches and take precedence over `spec.networkPolicy`.

## `spec.integration`

Enables external integrations. Only the enabled ones need to be present.

- **`googleChat`** — `enabled` (default `false`), `projectId`, `topicName`, `subscriptionName`, `allowedUsers`, `homeChannel`, and `mode` (`default` or `debug`, default `default`). When `enabled`, `projectId`, `topicName`, and `subscriptionName` are required (enforced by a CEL validation rule). Populated by the installer when Google Chat is enabled.
- **`slack`** — `enabled` (default `false`), `botTokenSecretRef` and `appTokenSecretRef` (Secret refs, required when enabled), `allowedUsers`, `homeChannel`, and `homeChannelName`. Populated by the installer when Slack is enabled.
- **`github`** — `org` (optional target GitHub organization/user, up to 39 characters) and `gitRepo` (optional target GitOps repository URL or `owner/repo` shorthand, up to 2048 characters). Supports HTTPS/HTTP (`https://`, `http://`), SCP-style SSH (`git@github.com:owner/repo`), SSH/Git protocols (`ssh://`, `git://`), and bare `owner/repo` shorthand. Rejects non-GitHub hosts and URLs containing whitespace or invalid syntax at admission (`failurePolicy: Fail`). If an invalid URL or organization is encountered during reconciliation, GitOps is disabled in config and a `Degraded` condition (`Reason: InvalidGitRepoURL`) is surfaced on the resource status. On reconcile, `gitRepo` is appended to the `gitops-state` ConfigMap (`managed_repos`) if absent; removing a repository configured via `gitRepo` requires clearing or updating `spec.integration.github.gitRepo` on the CR in addition to editing the ConfigMap. Populated by the installer when a GitOps repository is connected. The same ConfigMap accepts a hand-added `context_repos` key in the same JSON shape (`[{"type": "github", "url": "https://github.com/<owner>/<name>"}]`), naming repositories the agent reads for declared intent — a Terraform repository an audit consults before it reports a posture as a finding — and never writes to. An entry may add `"ref": "<branch>"` to read a branch other than the remote's default; a `ref` is kept when it is made of letters, digits, `.`, `_`, `/`, `-` and, after the first character, `@`, and passes git's branch-name rules (no `..`, `@{`, `//` or `.lock` component, no leading `-`); any other, an empty string, a JSON `null` or a value that is not a string (a number, a boolean) included, is refused with a warning naming it, and the declared-intent search skips that repository rather than reading its default branch in the pin's place, so the ledger names it as not searched until the entry is corrected (omit the key to read the default branch); a `ref` on an entry naming the GitOps repository itself is ignored, because the audit reads that repository at the branch it publishes against. Only `github` entries with a GitHub URL are read; any other entry is skipped with a warning naming the key. The operator does not seed that key: it is not populated from the CR, there is no CR field for it, and reconciles leave it in place. The operator does read it: each same-organization entry gets a read-only (`contents: read`) token minter policy, so a private context repository is readable through the credential broker's content-mode clone, provided the GitHub App is installed on it — see [Read-only tokens for context repositories](/kube-agents/deploy/token-minter/#read-only-tokens-for-context-repositories). Nothing that writes consults the key: a context repository stays refused by the broker's `commit` and `push`.

:::caution[Upgrade note: non-GitHub repository rejection]
Earlier operator versions passed arbitrary `https://` and `http://` URLs through to `CleanRepoURLWithOrg` without host validation. The operator now strictly restricts repository URLs to GitHub (`github.com` or `www.github.com`). Existing `PlatformAgent` resources configured with non-GitHub repositories reconcile into `phase: Degraded` with condition `Reason: InvalidGitRepoURL`, and subsequent updates to those resources are rejected at admission by the validating webhook (`failurePolicy: Fail`) until `spec.integration.github.gitRepo` is corrected or removed.
:::

See [`k8s-operator/api/v1alpha1/platformagent_types.go`](https://github.com/gke-labs/kube-agents/blob/main/k8s-operator/api/v1alpha1/platformagent_types.go) for the exact struct definitions.

## `status`

The operator writes observed state to the `status` subresource:

| Field                                  | Type     | Purpose                                                                                                                                         |
| -------------------------------------- | -------- | ----------------------------------------------------------------------------------------------------------------------------------------------- |
| `phase`                                | string   | Overall state (`Pending`, `Provisioning`, `Ready`, `Degraded`, `Failed`).                                                                       |
| `observedGeneration`                   | int64    | The `metadata.generation` the status was last computed from. Behind `metadata.generation` from a spec edit until the reconcile that follows it. |
| `address`                              | string   | Fully qualified domain name (FQDN) of the agent service.                                                                                        |
| `lastReconcileTime`                    | time     | Timestamp of the last status write. A reconcile that changes nothing in the status leaves it where it was.                                      |
| `conditions`                           | list     | Standard `metav1.Condition` observations, keyed by `type`.                                                                                      |
| `deploymentStatus.name`                | string   | Name of the underlying Deployment.                                                                                                              |
| `deploymentStatus.readyReplicas`       | int32    | Number of fully ready replicas.                                                                                                                 |
| `serviceStatus.endpoint`               | string   | Primary URL/IP (with protocol and port) to reach the agent.                                                                                     |
| `storageStatus.bound`                  | bool     | Whether the primary PVC has been provisioned.                                                                                                   |
| `telemetry.otlpEndpoint`               | string   | The OTLP collector the agent was wired to.                                                                                                      |
| `telemetry.otlpEndpointSource`         | string   | Which rung answered: `DeploymentEnv`, `Spec`, `OperatorEnv`, `Discovered`, `None`, or `Default`.                                                |
| `networkPolicy.generated`              | bool     | Whether the operator-managed NetworkPolicy is active. `false` when disabled, or not yet reconciled.                                             |
| `networkPolicy.dnsClusterIPs`          | []string | The DNS ClusterIPs written into rule 1.                                                                                                         |
| `networkPolicy.dnsClusterIPsSource`    | string   | Which rung answered: `Annotation`, `Spec`, `OperatorEnv`, `Discovered`, or `Default`.                                                           |
| `networkPolicy.metadataDaemonIP`       | string   | The post-NAT daemon IP in rule 3, empty when suppressed.                                                                                        |
| `networkPolicy.metadataDaemonPort`     | int32    | The post-NAT daemon port in rule 3, resolved from live DaemonSet or default (`988`).                                                            |
| `networkPolicy.metadataDaemonIPSource` | string   | Which rung answered: `Annotation`, `Spec`, `OperatorEnv`, `Discovered`, `Default`, or `Suppressed`.                                             |

These condition types appear in `conditions`; only `Ready` is always present:

| Type                  | Written                                                                                                                     | Meaning                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| --------------------- | --------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `Ready`               | Always                                                                                                                      | Tracks `phase`; its `reason` and `message` carry whatever the reconcile is waiting on — including the reasons that park `phase: Degraded` (`RuntimeClassNotFound`, `ShellSandboxCannotBeDisabled`, `ForbiddenVolumeMount`, `EgressAllowlistRefused`, `ModeNotRecognized`, `ShellSandboxKeysMissing`). The last two render today's stack in full and report at the end rather than returning early — `ModeNotRecognized` because it is a version skew rather than a bad spec, `ShellSandboxKeysMissing` because the objects are wanted in place so the Pod starts by itself once the Secret appears. Under `mode: next` a failed A2A provisioning Job parks the phase the same way with `A2AProvisionFailed`, naming the Job to inspect. That signal is prompt where the refusal is deterministic: the script exits 2 for a refusal a re-run cannot clear, the Job's `podFailurePolicy` matches exit 2 with `FailJob`, and the Job fails from its first pod, so the condition is written in seconds. Any other failure — NATS unreachable, an unexpected command failure — matches no rule and still spends the `backoffLimit: 20` retry budget, which is the slow path it used to take. On a fresh install the earlier symptom is an install that looks healthy — NATS `1/1`, clients authenticating — while every stream read returns `stream not found` (`err_code=10059`); a fresh `next` install with no streams and no `Degraded` condition is this, not a render fault. An install that already has a bus reaches the same reason from the other end, with every stream and bucket present: the script's closing check refuses when `TASKS` holds fewer consumers than `tuning.maxSessions` needs, so there the symptom is a working bus. The remedy is not a stream edit: `max_consumers` is the one limit nats-server will not change on a stream that exists, answering an update that carries a different one with `stream configuration update can not change MaxConsumers`, and the rendered bus is pinned to `nats:2.10`. The message names the two that do work — lower `tuning.maxSessions` until the budget fits the stream, or delete the `TASKS` stream and let provisioning recreate it at the width the CR asks for, which discards the 72h of task history that stream was holding. Those two do not finish the same way, and the message splits them. Lowering `tuning.maxSessions` finishes by itself: the CR edit re-renders the provision Job, whose name digests that render, so a new Job appears and runs with nothing else to do. Deleting the stream does not — nothing re-reads the bus until the Job runs again, so delete the Job to re-run it, or leave it and the 24h TTL will. Deleting the Job before one of the two remedies is done helps nothing: the same script refuses again against the same stream. Recreating the stream then costs one more step, and the order matters: once `TASKS` is back, restart the A2A gateway (`kubectl rollout restart deployment/<agent>-a2a-gateway`), and the agent workload `<agent>-gateway` with it where a Hermes bridge sidecar runs. Deleting a stream deletes every consumer on it, and neither of those two durable readers re-creates one, so a gateway left running keeps accepting delegations and spawning session pods while relaying no events, the bridge dispatches nothing, and the CR reads `Ready` over both — session pods recover by themselves, which hides it. Restarting before the stream is back only fails the subscribe.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| `Degraded`            | Only while degraded                                                                                                         | Something in the spec, cluster state, or operator install cannot be honoured. `Reason: InvalidGitRepoURL` (invalid org or non-GitHub repository in `spec.integration.github`; subsequent updates are rejected at admission until corrected), `Reason: CorruptManagedRepos` (unparseable `managed_repos` JSON in the `<agent>-gitops-state` ConfigMap; GitOps disabled), or `Reason: RBACIncomplete` (not a spec fault but install skew — the ClusterRole bound to the operator denies a permission its RBAC markers declare, which is what an image deployed ahead of its manifests looks like; the message names the denied verbs and the fix. While `InvalidGitRepoURL` or `CorruptManagedRepos` holds the condition the RBAC reason is not written, and the operator's startup log is the only trace). The refusals above ride the `Ready` condition instead of this one.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        |
| `BusCredentialsReady` | Under `mode: next`, and while a callout Deployment exists                                                                   | Whether every replica the callout Deployment asks for is ready, and therefore both serving an identity map and attached to the bus — its readiness probe answers 503 unless both hold. It is the claim about the callout as a whole, not the rule a dispatcher waits on: a workload dispatched before the callout can authenticate it is refused with an error indistinguishable from a bad credential, and one callout replica serving on the current spec is enough to prevent that. The operator's own gate on the A2A gateway does not read it: the gateway Deployment is not **created** until the callout Deployment's status shows at least one replica that is both ready and on the current pod template (`ReadyReplicas + UpdatedReplicas - Replicas >= 1`, once the Deployment controller has observed the current generation, read from a copy of the Deployment whose generation has reached the one the operator's own apply of the callout returned in that pass), and is reconciled normally once it exists. That gate is narrower than this condition on purpose. The callout's replicas form a NATS queue group, so one serving replica answers every authorization request, and a callout whose second pod cannot schedule would otherwise withhold a first gateway indefinitely; the lower bound cannot read true while no current-template replica is serving, so a gateway created while this condition is `False` still has a serving replica to authenticate its sessions. Expect the two to differ: this condition reads `False` with `1 of 2 replicas ready` while the gateway exists and dispatches. The hold is not silent: under `mode: next` the A2A gateway is one of the workloads `Ready` is a claim about, so while it is withheld the phase reads `Provisioning` and the `Ready` message names the Deployment. What waits on the callout is not the gateway's own connection — the gateway is a static principal, `NATS_USER=gateway` with a rendered password, listed in `auth_users` and so exempt from the callout exactly as the Hermes bridge sidecar is. It is what the gateway _spawns_: a session pod authenticates with a projected token that only the callout can turn into a bus identity, so a gateway created while the callout is unready accepts delegations and spawns sessions that cannot connect. That is the ordering this condition exists to enforce, and the gateway's creation is the last moment the operator can enforce it. Creation only, so a callout that goes unready later does not freeze a running gateway's spec, and does not orphan the session pods that carry an `ownerReference` to it. The gate's error directions are both false negatives, and each holds the gateway one more pass before the held reconcile's requeue clears it: while a terminated callout pod is still counted, one ready replica on the current template reads as none; and on the pass that changed the callout's pod template, the operator's cached copy of the Deployment may still be the one from before that change, which the gate refuses rather than trusting counts that describe the previous template. `Reason: CalloutServing` (`True`), `CalloutUnavailable`, or `CalloutAbsent`. The messages differ in what they can name: `CalloutServing` gives the identity map version being served, the two `CalloutUnavailable` messages give the ready count against the count asked for, and `CalloutAbsent` gives neither, because there is no Deployment to read either from. Ready is counted against `spec.replicas`, not against the pods that exist: the callout rolls at `maxSurge: 1` over `maxUnavailable: 0`, so a healthy roll spends its whole duration with more pods than it wants, and a callout coming up spends its window with fewer — a condition comparing the two numbers to each other would call the first an outage and the second serving. `CalloutUnavailable` also covers a spec change the Deployment controller has not observed yet, where the reported counts describe the spec before the roll; the message says so rather than reporting those counts as current. It does **not** assert that a named replica has observed a named map version, so a sub-second window after a re-render can report ready while a replica still serves the previous map. Removed, not set false, once the mode is not `next` **and** the callout Deployment is gone — both, so the condition never describes a callout that is still running. Re-derived from the callout Deployment on every exit from the reconcile, not at a point in its sequence, so a pass that parks `Ready=False` for an unrelated reason — a missing shell sandbox keypair, a refused `spec.deployment`, say — still reports it rather than leaving it absent or, worse, leaving the last value standing while the callout it named goes away. The one case it is neither written nor removed is version skew, a `spec.mode` this operator does not recognise: the bus a newer CRD rendered is deliberately left standing there, so the condition describing it is too. A CR flipped to `today` by an edit that is itself refused also keeps the condition, because the refusal withholds the teardown too and the callout is still running. |
| `EventWatcher`        | Only while `eventWatcher.enabled` is `false`                                                                                | `status: False`, `Reason: DisabledBySpec`. The emergency stop is still pressed and no cluster events are reaching the agent.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        |
| `VolumesDropped`      | Only after a pass that rendered the Pod template around a `hostPath` on `spec.deployment.extraVolumes` or `.sidecarVolumes` | `status: True`, `Reason: HostPathVolumeDropped`. The render leaves the named `hostPath` entries and every mount naming them out of the Pod template the operator applies; the message lists them by field and index, and on a spec carrying more of them than the controller's 4096-byte list budget holds it lists as many as fit and counts the rest. Not `Degraded`: the Pod runs without them. It is a claim about that template and not about the Pod that is running, because nothing here waits on the rollout — see the note under the table. Written on whichever status the pass ends on, `Ready` or `Degraded`, because the drop happens at render and three of the reasons that park `phase: Degraded` render first — `ShellSandboxKeysMissing` among them, which is where a chart-default install sits, and a chart-default install is one with no webhook in front of it. The four refusals that return **before** the render — `ForbiddenVolumeMount`, `ShellSandboxCannotBeDisabled`, `RuntimeClassNotFound`, `EgressAllowlistRefused` — neither write it nor clear it. No Pod is rendered on those passes, so the workload still running is whatever the previous pass left, and a condition written there would report the entries out of a Pod this operator never wrote — on an install rolled forward over a CR a pre-fix operator gave real `hostPath` mounts, that reads as the security property being satisfied while the live Pod still mounts them. One already present is left exactly as it stands, because the pass that wrote it did render and that Pod is the one running; so while such a refusal holds, the message and the condition's `observedGeneration` can both lag the spec until a pass reaches the render again. Remove the entries to clear it.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        |

`EventWatcher` is absent on a healthy install rather than `True`, deliberately: the operator can say
it asked for a watcher, but nothing here checks that one is alive, and a permanently-`True`
condition would read as a health signal it is not. Disabling the watcher is also not a `Degraded`
state — it is a decision somebody made, and `phase` stays `Ready`. `VolumesDropped` follows the
same shape: it exists while the spec carries a `hostPath` and a pass has rendered around it, and
it is the render-side half of the `hostPath` refusal, which the admission webhook makes at
admission on installs where it runs.

It is a claim about the Pod **template** the operator rendered, not about the Pod that is running.
The controller server-side-applies the template and returns as soon as the API server accepts it;
nothing waits on the rollout. So on the first pass of an operator carrying this behaviour over a CR
that a previous one rendered with real `hostPath` mounts, the condition reads `True` while Pods from
the previous revision still mount them — until the roll replaces them, or indefinitely if it stalls
at `progressDeadlineSeconds`. `Ready` does not close that window either: `readyReplicas` counts
ready Pods across every ReplicaSet the Deployment owns, so one still-ready old Pod is enough for
`phase: Ready`. With the single-replica default the strategy is `Recreate` and there is no window;
it opens at `availability.replicas` above 1, and the StatefulSet path rolls one Pod at a time for
the same effect. While the operator can see the roll is unfinished — the workload has not observed
the applied template, or it counts Pods that are not on it — the message says so in a sentence of
its own. A CR parked `Degraded` by `ModeNotRecognized`, `A2AProvisionFailed` or
`ShellSandboxKeysMissing` carries it on the same terms: those three refusals render before they
park, so that status write reads the gateway workload back to see whether the roll has finished.

```console
$ kubectl describe platformagent platform-agent -n kubeagents-system
...
  Conditions:
    Type:                 EventWatcher
    Status:               False
    Observed Generation:  3
    Reason:               DisabledBySpec
    Message:              Cluster event ingestion is disabled by spec.harness.eventWatcher.enabled=false. …
```

### Telling a current status from a stale one

`status.observedGeneration` is the `metadata.generation` the status was last computed from, and the
`Ready` condition, with any `Degraded`, `EventWatcher` or `VolumesDropped` condition written in the
same pass, carries it in its own `observedGeneration`. `Degraded`/`RBACIncomplete` is the exception:
it is written when the set of denied permissions changes and left in place otherwise, so its
`observedGeneration` is the generation at which that set last changed. `VolumesDropped` is the
other: a refusal taken before the render leaves it untouched, so while one holds its
`observedGeneration` is that of the last pass which did render. A spec edit bumps `metadata.generation` at admission
and leaves both behind until the next reconcile, so a `Ready` condition whose `observedGeneration`
is below `metadata.generation` describes the previous spec, and `kubectl wait --for=condition=Ready`
on its own can return on it. The field says the operator has processed that generation; it does not
say the rollout it triggered has finished. `Ready` is still derived from replica counts, and during
a rollout the replica satisfying it can be the previous one. `Ready` is a claim about three
workloads (the gateway, the shell sandbox and the credential broker), and under `mode: next` about
the A2A gateway as well — a next install without one serves no A2A request, and that is also how
the callout ordering gate becomes visible: while the operator holds a first gateway creation
back for a callout replica to serve, the phase reads `Provisioning` and names the Deployment it is
waiting on. So to gate
on a change, wait for the generation to be observed, then for the rollout of each workload, then
for the condition:

```bash
gen=$(kubectl get platformagent platform-agent -n kubeagents-system -o jsonpath='{.metadata.generation}')
kubectl wait platformagent/platform-agent -n kubeagents-system --for=jsonpath='{.status.observedGeneration}'="$gen"
kubectl rollout status deployment/platform-agent-gateway -n kubeagents-system
kubectl rollout status statefulset/platform-agent-shell -n kubeagents-system
kubectl rollout status deployment/platform-agent-credential-proxy -n kubeagents-system
# under `mode: next` only
kubectl rollout status deployment/platform-agent-a2a-gateway -n kubeagents-system
kubectl wait platformagent/platform-agent -n kubeagents-system --for=condition=Ready
```

## How config reaches each profile

A deployment runs several Hermes **profiles** from one pod: `default` (the Planning Agent front door),
`platform`, and one `cluster-*` profile per managed cluster. The named profiles are each configured
by an overlay merged into an image-built base at startup. The `default` profile is the exception: it
takes the operator's settings by _two_ routes at once — an overlay merged into its config, and a
read-only **managed scope** pinned over it.

| Profile                                                       | Delivery                                                                                                                                                   | Who owns the file                                      |
| ------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------ |
| `default`                                                     | Image-built base, writable on the PVC + `profile-default.overlay.yaml` merged at startup + a narrow set of keys pinned read-only at `/etc/hermes`          | Agent owns the file, operator the pins                 |
| `platform`                                                    | Image-built base + `profile-platform.overlay.yaml` merged at startup                                                                                       | Image owns the base, operator the overlay              |
| `platform`, with [`platformFrontDoor`](#platformfrontdoor) on | The same two inputs, but the base is back-filled rather than force-synced — and the `/etc/hermes` pins land here too, because that mount is machine-global | Agent owns the file, operator the overlay and the pins |
| `cluster-*`                                                   | Image-built base + `profileclass-cluster.overlay.yaml`, plus `profile-<name>.overlay.yaml` if one exists                                                   | Image owns the base, operator the overlay              |

A cluster profile is the only one that can take two overlays: the class overlay carries
`tuning.cluster`, which applies to all of them, and a plugin targeting one specific cluster produces
a `profile-<name>` overlay for it as well. The class overlay merges first, so the per-profile file
wins any conflict.

**Why `default` is also pinned.** The pins are the one change-control boundary the front door has:
the agent's own config file is writable, so without them a bad runtime edit survives a restart. (It
is _not_ a security sandbox — see the
[AgentPlugin trust boundary](/kube-agents/reference/security-and-iam/#change-control--safety).)

**What is pinned is narrow, on purpose.** `/etc/hermes` is machine-global — one file for every
profile in the pod, not just `default` — so it carries only what is identical for every profile
_and_ beyond the agent's own repair: `model.*`, `platforms.*`, `approvals.cron_mode`,
`display.platforms`, `terminal.*` (where the shell runs: one sandbox per Pod, reached the same way by every
profile) and, when the agent Pod has a runtime class, `database.journal_mode` (one data volume per Pod, and a
corrupted database is found only after the sessions in it are unreadable). The reasoning is that as long as a
human can reach the agent (`platforms`) and the agent can reason (`model`), anything else it breaks it can be
talked into fixing.

Everything else the operator owns for the front door goes in `profile-default.overlay.yaml`
instead: `plugins.enabled` for AgentPlugins with no `targetProfile`, those plugins' non-gateway
config subtrees, and `spec.harness.tuning`'s `default` limits and `maxInProgress`. Those are
profile-shaped — pinning them machine-globally would hand the front door's settings to every
specialist — and they are all recoverable by an agent that can still talk and still reason.
Nothing the operator renders appears on both routes. What appears on neither, and so stays the
image's alone, is each profile's toolsets, `mcp_servers` and `memory`.

It is also the only profile whose config the _running agent_ writes to: `/sethome` records the home
channel there, the monitoring policy mints `monitoring.install_id`, and slash commands save
preferences. Those two facts pulled in opposite directions, and the managed scope is what resolves
them.

The rendering is published as the `managed-config.yaml` key of the `<agent>-config` ConfigMap and
mounted read-only at `/etc/hermes/config.yaml`. Hermes treats that directory as an administrator
layer and overlays it, **per leaf key**, on top of `$HERMES_HOME/config.yaml` at every load. Three
things enforce it (`hermes_cli/managed_scope.py`):

- `load_config` deep-merges the managed dict on top of the agent's own;
- `save_config` strips every managed leaf before writing, so a save cannot persist one;
- `hermes config set` rejects a managed key by name.

So `$HERMES_HOME/config.yaml` stays an ordinary writable file — `/sethome` and the install id work —
while every leaf the operator renders is authoritative and immutable at runtime. Whatever ends up in
the PVC file, the operator's value is what loads, so a restart always heals. Earlier shapes did not
manage both: mounting the render over `$HERMES_HOME/config.yaml` made the path read-only and failed
every runtime write (`/sethome` with a permission error, the rest silently), and
merging it into the file at startup left every merged key mutable, so an agent that repointed
`model.base_url` at nothing kept that across restarts.

`platforms.<platform>.home_channel` is deliberately **not** pinned, so `/sethome` can still set it
from chat. The platform credentials and endpoints that have no `config.yaml` equivalent are pinned
through a companion `/etc/hermes/.env`, which Hermes applies last with `override=True` and refuses to
let the agent overwrite — without that, a container env var would beat the pinned `platforms.*` leaf.

That file also pins five values that are not credentials at all. The first is `API_SERVER_KEY=cluster-internal-trusted`,
the non-secret loopback sentinel the Hermes API server on `127.0.0.1:8642` validates. It is pinned here
because Hermes' stage2 hook generates a random `API_SERVER_KEY` into `$HERMES_HOME/.env` whenever that
file carries none, and the PVC `.env` is applied with `override=True` too — ahead of the container env,
behind `/etc/hermes`. Unpinned, the gateway ends up validating against a value no caller has, and the
credential proxy, the startup probe and every in-pod loopback call get `401 Invalid gateway API key`.
The container entrypoint warns at boot when this pin and the container env disagree.
The credential that guards the API from _outside_ is `API_SERVER_EXTERNAL_KEY`, set from
`hermes.apiServerSecretRef`; the sidecar authenticates the caller against it and swaps in the sentinel.
The second is `HERMES_HOME_MODE=2770`, the mode Hermes re-applies to `$HERMES_HOME` at every process
start. The container env carries it too, but that is the lowest-precedence of the three layers, so a
`HERMES_HOME_MODE=0777` line the agent writes into the PVC `.env` outranks it and widens every
directory Hermes secures on the shared volume.

The third is `KUBEAGENTS_MODE` (`today` or `next`, from `spec.mode`) — the mode switch's delivery
contract (`docs/designs/spec-mode-switch.md`). It is pinned always, with the real value, because an
absent key is a key the agent may write, and it is read back by exactly one module, `agents/platform/scripts/runtime_mode.py`.

The fourth is `KUBEAGENTS_SCOPE_FILE=/etc/kube-agents/scope.json`, where the reconcile finds the
[`spec.scope`](#specscope) declaration. The container env carries it too, for the same reason as
`HERMES_HOME_MODE`: a line the agent writes into the PVC `.env` would otherwise outrank it and hand
the reconcile a declaration the agent authored. Its one reader is `agents/platform/scripts/cluster_agent_reconcile.py`.

The fifth is `RECONCILE_PROJECT=`, pinned empty. The reconcile reads that variable as the management
project ahead of the metadata server, and a management project that changes identity retires the
old one's profiles, so a line the agent wrote into the PVC `.env` could otherwise re-point it and
have two clean runs delete every profile of the real management project. Nothing in the operator
or the chart sets the variable; the script treats the empty value as unset.

One consequence of the render is worth knowing: the managed overlay is a
leaf-level merge, and a list is a leaf, so a list rendered here **replaces** the image's rather than
unioning with it — for every profile at once. That is why the render emits no lists at all today,
and why adding one is the change to think hardest about.

**Why the others get overlays.** Their `config.yaml` is assembled at image build time by merging the
shared defaults with that profile's own overlay, content the operator does not have; a `cluster-*`
config additionally carries a runtime `cluster_identity` stamp that the reconciler matches profiles
to clusters by. Rendering either file in full would fork the source of truth and, for cluster
profiles, strip that identity record.

Every overlay is a key in the one `<agent>-config` ConfigMap, so a change to any of them moves the
config hash and rolls the pod. That restart is required, not incidental: the merge happens once at
startup, so a live ConfigMap update without a restart would be a no-op. The managed key shares the
ConfigMap and so rolls the pod too, though for it the restart is belt-and-braces rather than
required — it is mounted as a directory, not a `subPath`, so the kubelet propagates updates and
Hermes re-reads the file when its mtime or size changes.

Startup is not the only moment a merge happens. Onboarding a cluster scaffolds a new profile without
changing the ConfigMap, so nothing rolls the pod; that profile applies the overlays itself as it is
created. Without it a Cluster Agent created between two pod starts would run on Hermes' own defaults
however the CR is tuned.

**Ordering.** The entrypoint force-syncs each profile's image-owned files first, then merges the
overlays. The reverse order would silently erase every overlay on each restart. The `default`
profile's `config.yaml` is the exception to the force-sync: it is the agent's own file, and a
force-sync is exactly what would throw the runtime's edits away. It is instead seeded from the image
on a fresh volume, and thereafter only back-filled — keys the image declares and the live file has
lost are restored, keys it already holds are left alone. Its overlay is merged after that
back-fill, so the operator's settings are not undone by it.

The platform profile's `config.yaml` becomes a second exception under
[`platformFrontDoor`](#platformfrontdoor), and for the same reason: the gateway is homed there, so
that file is now the one `/sethome` and the monitoring policy write to. It leaves the force-sync
list and is back-filled from the image template instead, on exactly the terms `default` gets — keys
the template declares and the live file has lost are restored, keys it already holds are left
alone. Its overlay merges after that back-fill as it always did. Everything else the image owns in
that profile — the persona files, `cron/`, `skills/`, `governance/`, `hindsight/` — still
force-syncs either way.

One value inside both of these files does follow the image: the `User-Agent` header that the
remote MCP servers' `args` carry (see [the config reference](/kube-agents/reference/config/)). The
back-fill recurses only through mappings and that value lives in a list, so it would otherwise stay
as the image that scaffolded the profile spelled it for the life of the volume. At every start the
entrypoint sets it to the image template's in each cluster profile's `config.yaml`, and in the
platform profile's when it is the front door, and changes nothing else in the file.

**Merge semantics.** These differ between the two mechanisms, which is the easiest thing to get
wrong here. In a startup **overlay** — every profile including `default` — maps merge recursively,
lists union, and scalars are replaced by the overlay; precedence, lowest to highest, is Hermes
built-in default → the value committed in `agents/<persona>/config.yaml` → the operator overlay from
the CR. In the **managed scope** the merge is per leaf key, so a list replaces rather than unions,
and it wins over everything else because it is applied at every load rather than once at startup.

**Two writers, two authorities.** Both `spec.harness.tuning` (operator policy) and an
`AgentPlugin`'s `spec.config` (plugin-supplied) land in the same overlay file, but not with equal
rights. A plugin's config is restricted to `approvals`, `platforms`, and `platform_toolsets`, and
for an untargeted plugin only `platforms` reaches the machine-global managed scope — the rest goes
to the front door's overlay. The `agent` subtree holding the execution limits is dropped from plugin
config and writable only by the operator. That is a coordination boundary rather than a security one — plugin code executes
in-process and could change these at runtime — but it keeps limits with board-wide consequences in
one reviewable place.

### Rotating a Secret rolls the pod

Credentials reach the agent pod as environment, through `SecretKeyRef`, and a container's
environment is fixed for the life of the pod: editing the Secret changes nothing a running container
can see. So the operator does for Secrets what the config hash does for ConfigMaps. It reads the
Secret keys the rendered pod spec consumes as environment, digests them with an HMAC-SHA256 keyed by
the UID of each Secret they come from, and stamps the result on the pod template as
`kubeagents.x-k8s.io/secret-env-hash`. Rotating one of those keys moves the digest, which changes the
template, which rolls the pod onto the new value. Both pods that read credentials this way are
stamped: the gateway and the credential proxy. The digest is keyed because the annotation is
readable by anyone who can read pods: an unkeyed hash would let that reader verify guesses at a
low-entropy value offline, whereas the UID is on the Secret object, and reading it takes the same
`get` on the Secret that reads the values. (A UID also travels on Events and owner references that
point at the Secret; the operator creates neither.)

Five details decide whether you will see it happen.

- **Within fifteen minutes, not immediately.** The operator does not watch Secrets — it holds no
  `list` or `watch` on them, deliberately — so nothing wakes a reconcile when one changes. A healthy
  pass instead asks to be requeued after `secretEnvReprobeInterval`, and the re-read happens then.
  `kubectl rollout restart deployment/<agent>-gateway` still works and is immediate.
- **Only what the pod reads as environment.** A key no container references is not in the digest, and
  editing it rolls nothing. Neither does a key the pod _mounts_: the kubelet refreshes a mounted
  Secret file in place, so hashing one would roll a pod over a change it was going to see anyway. The
  gateway mounts exactly one item of `platform-agent-secrets` that way, `SANDBOX_SSH_PRIVATE_KEY`,
  and because an init container copies it into an `emptyDir` at pod start, rotating that one key
  still needs a restart — as it did before this change. The shell sandbox mounts its own
  `<agent>-shell-authorized-keys`, and deliberately never names `platform-agent-secrets` at all.
- **Whichever Secret the pod actually names.** The refs are read off the rendered pod spec, so a CR
  that supplies its own `SecretKeyRef` pointing at a different Secret is covered without naming it
  anywhere.
- **A missing Secret is not an error.** It digests to a marker, so creating the Secret later moves the
  digest and rolls the pod, and an install whose credentials arrive after the agent behaves the way
  you would expect. A Secret the operator cannot read for any other reason — an API error rather than
  a `NotFound` — keeps the digest the last good pass computed, so a blip neither rolls the pod nor
  stops the rest of the reconcile.
- **Recreating a Secret rolls the pod once, even with the same values.** The digest's key is built
  from the UID of every Secret it reads, which an in-place edit, `kubectl apply`, or a patch keeps
  and a delete-and-create (including `kubectl replace --force`) replaces. A metadata-only write — a new label or annotation —
  changes neither the UID nor the values and rolls nothing.

**The roll is a stop-start.** At the default single replica the gateway's update strategy is
`Recreate`, so the old pod is terminated before the new one starts and the agent is unreachable
across the gap — up to the startup budget of roughly ten minutes on a cold image pull. Expect one
such restart per agent the first time an operator carrying this change reconciles: the annotation is
new, so the first pass adds it and the template changes once, whether or not anything was rotated.
An operator upgrade that changes how the digest is computed restarts each stamped pod once in the
same way.

## Reconcile behavior

- On create/update, the controller ensures the Deployment, Service, ServiceAccount, and ConfigMaps match the spec.
- On delete, it garbage-collects owned resources (note that `litellm-policy` is owned by `Deployment/litellm` rather than `PlatformAgent`, so deleting `PlatformAgent` preserves `litellm-policy` while LiteLLM is still running to prevent fail-open egress).
- The admission webhook (behind cert-manager) validates the spec before it's persisted; it enforces at most one `PlatformAgent` per cluster, forbids sensitive environment variable overrides (`API_SERVER_KEY`, `HERMES_HOME`) and privileged containers/volumes (`hostPath`), requires each `imagePullSecrets` entry to name a Secret, and acts as a name-based tripwire against obvious privileged service account names (`cluster-admin`, `system:admin`). The `hostPath` refusal has a second layer in the controller: a `hostPath` volume that reaches the reconcile anyway — the Helm chart ships the webhook off, and one enabled through the chart fails open at its default `failurePolicy: Ignore` — is left out of the Pod template at render with every mount naming it, and the `VolumesDropped` condition reports it. Note that full RBAC least-privilege enforcement is handled by controller- and pipeline-level policies rather than the admission webhook.
- The webhook also refuses a user volume or mount on `spec.deployment` that would carry the A2A bus credential by a route other than the operator's own projection, in every mode: the reserved volume name `a2a-bus-token`, a `serviceAccountToken` projection for the bus audience (`a2a-bus`) under any name, or a `secret` volume or projected `secret` source naming one of the Secrets the operator renders with bus credentials in them (`<name>-a2a-nats-creds`, `<name>-a2a-nats-config`, `<name>-a2a-callout-keys`). On an install running the A2A surface — `spec.mode: next`, and also a CR whose `spec.mode` this build does not recognise — the render leaves the same entries, and every mount naming them, out of the Pod. That happens whether or not admission ran; it is not a fallback for a skipped webhook, though a skipped webhook is when it is the only thing left. `env` references to those Secrets are not checked. This is a guard against misconfiguration rather than a boundary between the containers of one Pod, since a ServiceAccount token is pod-scoped.
- The `kubeagents.x-k8s.io/prevent-deletion: "true"` annotation on a `PlatformAgent` blocks deletion of the resource via the validating webhook (`ValidateDelete`). This serves as an accidental-deletion guardrail rather than an authorization control — `ValidateUpdate` does not block removing the annotation, so any principal with update permissions can patch the annotation off before deleting.
- The `kubeagents.x-k8s.io/enable-litellm-network-policy: "false"` annotation on a `PlatformAgent` opts the shared `litellm-policy` out of operator reconciliation and deletes any managed copy without affecting the agent pod's own NetworkPolicy. Note that deleting the managed policy leaves LiteLLM unselected (fail-open) unless a replacement NetworkPolicy is managed out-of-band.
- The `kubeagents.x-k8s.io/otlp-collector-namespace` annotation sets the collector namespace for `litellm-policy` when LiteLLM exports to an in-cluster collector whose namespace cannot be derived from `spec.telemetry.otlpEndpoint`.
- The `kubeagents.x-k8s.io/network-policy-enforcement: absent-accepted` annotation is a record, not a control: the Terraform composition stamps it when an install onto an existing cluster chose to proceed without NetworkPolicy enforcement, and the operator does not read it. [Installing without NetworkPolicy enforcement](/kube-agents/install/prerequisites/#installing-without-networkpolicy-enforcement) says what it means.
- Under the Helm chart, `platformAgent.annotations` is the route to these annotations. The chart stamps the last two itself from `litellm.networkPolicy=false` and a non-empty `telemetry.collectorNamespace`, and when it does, an entry that disagrees with the value fails the render — see [PlatformAgent annotations](https://github.com/gke-labs/kube-agents/blob/main/charts/kube-agents/README.md#platformagent-annotations) in the chart README.
- The Helm chart renders and applies the CR (the install engine drives it through `terraform apply`); you can also edit it directly with `kubectl edit`.

## Where to go next

- [`k8s-operator/README.md`](https://github.com/gke-labs/kube-agents/blob/main/k8s-operator/README.md) — build and test the controller locally.
- [Quick start (GKE)](/kube-agents/install/quickstart-gke/) — how the CR gets applied in a fresh install.
