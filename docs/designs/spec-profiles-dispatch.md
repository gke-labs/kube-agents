# AgentProfiles and dispatch, off kanban

- **Author:** [@bnaylor]
- **Date:** 2026-10-22
- **Status:** decided; build tracked in #3654 (#3655 dispatcher, #3656 persona format, #3657
  gateway delegation, #3658 cluster-agent profile, #3659 tiers).
- **Companions:** [`spec-subagent-profiles.md`](spec-subagent-profiles.md) - the `AgentProfile`
  CRD and what the operator renders from it; the A2A payload spec
  ([`spec-a2a-payloads.md`](spec-a2a-payloads.md)) - envelope, subjects, authority; the NATS
  deployment spec ([`spec-nats-deployment.md`](spec-nats-deployment.md)) - streams, accounts,
  consumer narrowing; the chatops gateway spec
  ([`spec-chatops-gateway.md`](spec-chatops-gateway.md)) - doors and delegation. This spec
  changes parts of each; [Changes to companion specs](#changes-to-companion-specs) lists them.

## Purpose

This spec moves delegation off Hermes kanban and onto AgentProfiles, with real skills and
prompts per profile. Kanban is going away entirely. Its execution bandwidth is limited, and
the integration has grown a pile of patches (the kanban-only image patches, #2734's use of
`chat.notify` for card reports, and guards such as #2932, #3255 and #3359).

What exists today: the `AgentProfile` CRD and what the operator renders per profile (#2469:
a ServiceAccount, a narrowed callout identity, an operator-published agent card). There is no
dispatcher and no running profile. Delegation is one hop to `platform`, and from there it
runs on kanban.

## Where dispatch lives

Clients address a profile, and the operator runs it. A client may be the gateway's chat
front door, a desktop console client (the Antigravity MCP client), cron or the event watcher.
Server-side execution has to exist anyway, for events, cron and chat users without a desktop
client.

Desktop clients keep their own client-side agent definitions, fully supported, and gain the
**option** of routing to AgentProfile agents. That needs one gateway console verb or an
addressee field on the console frame. The gateway stays the single entry point, which is
where tiers and grants apply.

## What a profile is

A profile is a **persona** (instructions, its own skills, its own configuration) mounted into
a **harness**, with the model chosen through LiteLLM (`harness.model`). Models go through
LiteLLM only (#2922).

**Harness-agnostic.** Nothing may require one harness's specific functionality. Claude Code
is the design center. Antigravity is the alternative and has to keep working.

### Persona format

The persona is built into the profile's `persona.image` and mounted read-only at `/persona`:

```
persona.yaml            apiVersion, name, description, model hint,
                        declared task parameters, tool allowlist, harness hints
instructions.md         the profile's instructions; plain markdown, AGENTS.md-compatible
skills/<name>/SKILL.md  one directory per skill, Agent Skills format
                        (frontmatter name + description, body, optional scripts/)
mcp.yaml                MCP servers the persona needs, by reference to brokered tools;
                        no credentials
```

A thin **adapter** per harness runs at task start, in the worker, and writes that harness's
own configuration into a **per-task configuration directory outside the task's workspace**, so
the agent, which can write its working directory, can't change the configuration it runs
under (the Claude Code adapter in `a2a/lib/persona/claudecode` does this). Claude Code and Antigravity get the first
two adapters. An adapter only maps: anything a harness can't express is dropped with a
warning, never a hard failure, so no persona can depend on one harness's feature. Task
parameters (for example the target cluster) reach the agent as a preamble the dispatcher
renders into the first message, so the persona stays static and harness-neutral.

Each profile's own skills are baked into its persona image. Shared skill packs mount as
plugins (#1726), so fixing a shared skill doesn't mean rebuilding every persona. A small
`persona build` step produces persona images, in CI and for customers' own profiles.

Because the persona is read-only, and every writable path a task can reach is reset per task
(see [Execution](#execution)), an agent that edits its own skills or configuration (#3444)
changes nothing that outlives the task.

### Command-line tools

`harness.image` is the worker image, set per profile, so a profile can point at a base image
built on the standard harness. But tools baked into a harness image have to be rebuilt for
every harness. So `AgentProfile` has a **`tools`** list of OCI images (shipped in #3694; the
field's doc comment is authoritative): each is mounted read-only at `/opt/tools/<name>` with
its `bin/` on `PATH` in list order, so one tools image serves every harness, and
`harness.image` stays the escape hatch. Where image volumes aren't available (Autopilot, or
Kubernetes below 1.35), tools are staged by an init container. The requirement this spec adds
for warm pools: the staged copy is read-only to the worker and re-staged whenever the
per-task reset runs (or the pod restarts), so a task can't replace a tool for the next one.

```yaml
spec:
  persona:
    image: registry/kube-agents/personas/gitops-helper:1.2
  harness:
    image: registry/kube-agents/harness-claude-code:0.17
    model: claude-sonnet
  tools:
    - name: terraform
      image: registry/kube-agents/tools/terraform@sha256:...
    - name: helm-kustomize
      image: registry/kube-agents/tools/helm-kustomize@sha256:...
```

Tools that need credentials (`kubectl` or `gcloud` against the fleet, `gh` against a forge)
reach them through the broker and credential proxy, scoped per task and per tier, as the
sandbox does today: the worker holds no raw tokens, only what the credential-isolation rules
allow (audience-bound projected tokens). Credentials are never in a tools image. Tools that
need none (`helm template`, `jq`, `kustomize`) run locally.

## One profile per role, not per cluster

There is **one generic cluster-agent profile**, and the target cluster is a parameter on each
task. There are no per-cluster CRs, identities or cards. The same design runs 10 clusters and
10,000, with no mode flip on the way. A flip would mean two code paths, and a migration that
lands while a customer is growing fast.

What a one-per-cluster design would have bought, and where it lives instead:

- **Least privilege per cluster:** the target cluster is part of the task's **grant scope**,
  minted by the gateway, and the credential proxy issues credentials for that cluster only,
  checking the target through the verifier, never from the envelope or the worker.
- **Per-cluster context and memory:** a store keyed by cluster, which each task reads at
  start. It is data, not objects, so it scales. It is the one channel that outlives a task, so
  it goes through the shared memory service (its own design, outside this spec), with that
  service's rules on who may write which scope and its injection scan on writes. A task treats
  recalled memory as untrusted context, and an Unattended task never proposes a change on the
  strength of memory alone.
- **A different persona or tier for one cluster:** a **pinned profile** with `clusterRef` set
  (already in the CRD), for example a production cluster that allows only some tiers. That is
  a handful of CRs, not one per cluster.

## Scale rules

A profile is a template, not one agent. Concurrent instances are bounded by the dispatcher's
cap, so the profile itself never becomes the bottleneck. Two design rules keep the real
limits in check:

- **Breadth by collectors, depth by agents.** A fleet-wide question runs a deterministic
  collector sweep (as #3135 moved the audit streams onto one collector run). Agents start only
  for the few clusters the sweep flags. Fan-out to agents is narrow by design, and the cap is
  a backstop.
- **Correlate before you dispatch.** Unattended triggers dedupe and group across clusters
  first, so one incident gets one investigation, not one per affected cluster. Each trigger
  has its own rate cap.

Very wide work fans out in layers: a coordinator profile (per project or region) above the
cluster agents, each layer a different profile. Precisely (decided 2026-10-22, worded here
to close the gaps review found): only a **`top`** task may start children of its own profile,
and **no profile appears twice on a task's ancestor chain** otherwise. So a `child` task never
starts a child of its own profile, and A → B → A cycles are refused. The gateway checks the
chain when it mints a child. A waiting parent's time cap stays the backstop, and resource
cycles across separate chains (two chains each waiting on the other's profile) are left to
the dispatcher's plan (#3655).

The model-quota ceiling, and replicas or sharding for the gateway, NATS, LiteLLM and the
credential proxy, belong to the sizing workstream. Profiles take their concurrency caps from
its budget function; until it exists, a fixed conservative default behind a knob.

## Replacing kanban's plumbing

1. **Results back to chat.** The gateway relays results from any profile and any number of
   children. A top-level task's result goes to the asking conversation. A child's terminal goes
   to its **parent task**, as a child-result input on the parent's own per-task reader, so a
   parent that waits by staying open receives what its children found. It never goes to the
   conversation directly. This retires #2734's card-report use of `chat.notify`.
   `chat.notify` itself stays: it is the chatops spec's route for proactive posts (cron
   reports, alerts to the home channel).
2. **Progress.** Profile workers publish progress on the task's event stream as a new
   `kube-agents.step/v1` artifact (introduced with the Antigravity client, #3653), with a
   `subtask` kind. It extends today's `progress` artifact. Desktop clients and the chat
   adapters both render it.
3. **Fan-out with a wait.** A profile can start several child tasks and wait on their
   results. **Each child gets a fresh root grant, minted by the gateway** for the child's
   profile. It is not an attenuated copy of the parent's grant. The gateway enforces the
   narrowing when it mints: the child's tier equals the parent's (and the child's profile must
   allow it), and the child's scope must lie inside the parent's. The gateway stays the only
   publisher of submissions. Depth is capped (as `A2A_DELEGATION_DEPTH_MAX` caps delegation today), and the
   ancestor-chain rule in [Scale rules](#scale-rules) applies (only a `top` task nests its own
   profile; no profile repeats otherwise). Entries a broker adds to a grant chain never change
   the profile. Width is bounded by the dispatcher's caps.
4. **Stop.** One stop cancels the conversation's whole task tree, enforced by the dispatcher,
   keeping the user-facing behaviour of #3263 and #3379.
5. **No "run after" dependencies.** Kanban's `kanban_link` is dropped on purpose. Fan-out plus
   a wait covers every use we know of. Dependencies get added only when a profile actually
   needs them.

**The parent waits by staying open** (with a time cap) while its children run, rather than
finishing and being woken when they are done. This is the simple path, chosen deliberately.
It gets revisited only if workers held during long fan-outs become a real cost. To keep it
from deadlocking, **child tasks are admitted through their own queue with their own cap** (the
`child` kind below), so parents holding the profile's top-level slots can never starve their
own children (or a coordinator's children of the same profile).

## Tiers and identity

- **Two tiers to start:**
  - **Read:** cluster state plus contents and logs. No Secret contents. Writes are proposed
    through GitOps.
  - **Unattended:** the system principal of a trigger (the event watcher, a named cron). It
    reads cluster state plus logs, may only propose (a ledger entry, a pull request, a
    notice), and has a cap per trigger.
- **Tiers are data, not code.** The tier set is defined in a CR, so tiers can be added by us or
  by customers. A tier names its Kubernetes RBAC set, its GCP role set, its write mode (propose
  or none), its caps, and whether it is interactive or unattended (so the budget knows which
  side of the interactive share a tier falls on). A tier's caps never lift the install
  ceiling. Only cluster administrators may write tier CRs; no agent ServiceAccount can. A tier
  that needs GCP roles beyond the role sets the install already provides needs those set up
  in Terraform first, so "without a release" holds for tiers that reuse existing role sets.
- **No tier reads Secret contents.** That keeps cluster state apart from secrets with only two
  tiers. A secrets-reading tier would be added explicitly.
- **No direct writes** in this round.
- **The carrier is the #1884 capability grant,** issued at ingress and resolved through the
  verifier. `authority.requester` stays advisory, as the payload spec requires. The verifier
  answers yes or no by design, so the **dispatcher** asks it, with a new verb, whether the
  grant permits running the resolved profile at tier T for the target in scope. It never reads
  a tier out of the grant.
- **Which tier a task runs at.** A task runs at the requester's tier. Each AgentProfile lists
  the tiers it allows, and the dispatcher refuses a task whose tier the resolved profile
  doesn't allow (a pinned production profile might allow only Read). Tier is authority, so it
  lives on the CR, never in the persona.
- **Membership:** which tier a human is in comes from a mapping on the PlatformAgent (groups or
  user ids to tier), defaulting to Read. Admission refuses a mapping that assigns Unattended to
  a human. In a multi-author thread, the task runs at the minimum tier across the authors,
  keyed on the door's attested identity, as the delegation allowlist already does.

Gaps on main that the build closes (#3659):

- The gateway mints every grant with the install-wide `AuthorityTier`, so the membership
  mapping has to feed the mint.
- The capability library ranks a hard-coded tier list and refuses unknown tiers. That becomes
  explicit per-tier permission sets, since Read and Unattended are not a rank.
- Per-tier Kubernetes ServiceAccounts don't change cloud authority. GCP service accounts come
  from Terraform and fleet reads run as the broker's account, so each tier needs a GCP role set
  provided by Terraform, with the broker choosing by tier.

## How tasks are routed

### Addressing

1. Every profile has a card on `a2a.agents.<profile>`, published by the operator. That is how
   clients discover what they can address.
2. A client names a profile and, for cluster work, a target cluster.
3. The gateway resolves the address:
   1. for cluster work, a pinned profile whose `clusterRef` matches the target gets the task,
      otherwise the generic cluster agent does; a pinned profile addressed for any other
      cluster is refused;
   2. it checks that the target is in scope (`spec.scope`, including `exclude.clusters`);
   3. it mints the task's grant against the **resolved** profile, with the requester's tier
      and the target cluster in the grant's scope;
   4. it publishes the submission on `a2a.submit.<profile>.<tier>.<kind>.<task>`, on the
      `PROFILE_SUBMITS` stream, where `<kind>` is `top` for a task a client asked for and
      `child` for a task another task started. The target cluster is a task parameter rather than a subject token, so queues are
      per profile, tier and kind, never per cluster.

**Subjects (decided 2026-10-22).** Only new submissions use the submission subject, and only
it carries the tier and kind tokens, because they exist to route queues. Follow-ups and cancels
for a running task stay on the task's own `a2a.tasks.<profile>.<task>.in`, read by the
task's per-task reader, and `.events` and `.supervisor` keep their per-task shape. Addressees
that aren't profiles (`platform`, sessions) keep today's subjects.

**Amended 10/28 (#4121): submissions have their own stream.** They were first planned as
`a2a.tasks.<profile>.<tier>.<kind>.<task>.submit` on TASKS. On TASKS, any principal that can
create consumers there (the bridge, the gateway, and the `web` and `console` credentials a
browser is handed) could create a consumer that pulls a profile's submissions, or rewrite
the operator's durable in place, because JetStream carries a consumer's settings in the
request body where a subject grant can't see them. `PROFILE_SUBMITS` on `a2a.submit.>`
closes that: the operator is the only principal with a consumer verb on it, the dispatcher
holds only fetch and ack on the durables it is configured for, the gateway holds only
publish on `a2a.submit.*.*.*.*`, and the console holds direct get by subject, so it can show
a profile task's prompt and nothing more. Session queues on TASKS keep the exposure; that
is #4173.

### Admission and holding

**The dispatcher is the only fetcher and holder** (decided 2026-10-22), on both execution
paths:

- There is **one shared durable consumer per (profile, tier, kind)** on
  `a2a.submit.<profile>.<tier>.<kind>.*`, on `PROFILE_SUBMITS`, created and owned by the
  operator. The
  dispatcher is its only reader. `max_ack_pending` is per consumer, so each durable caps its own
  in-flight count, and a `child` durable has its own cap separate from its profile's `top`
  durable.
- The dispatcher fetches a submission, asks the verifier (with a new verb) whether the grant
  permits running the resolved profile at tier T for the target in scope, checks the tier
  against the profile's allowed tiers, and checks the message's age against
  `spec.queueTimeoutSeconds` (default 3600; the stream's own age limit is stream-wide). It
  fails a task that fails any check with a visible notice.
- It then **holds the message unacknowledged for the task's whole life**, with periodic
  in-progress acks, and hands the task to an executor: a warm worker for interactive tiers, a
  Job for the unattended tier. Workers get tasks only from the dispatcher; they have no fetch
  grant on the durables and no verifier grant.
- **The executor renews a liveness lease** for the task in a KV bucket, keyed by task, and the
  dispatcher enforces a deadline (`lifecycle.activeDeadlineSeconds` on Jobs, the same limit
  for warm workers). When the lease expires, the dispatcher publishes a terminal failure with
  the reason. A task is never re-run automatically, which keeps the subagent spec's rule for
  LLM tasks.
- **After a dispatcher restart**, its held messages are redelivered to the new instance. For
  each one it checks the lease: a live lease means the executor is still running, so it
  re-adopts the task and keeps holding; an expired lease on a started task becomes a terminal
  failure; a task that never started is dispatched normally.
- **Caps:** `spec.concurrency` is the profile's own ceiling. The sizing budget, once it exists,
  sets the effective value, which never exceeds `spec.concurrency`. An install-wide ceiling on
  the PlatformAgent caps the total across profiles, and reserves `max(1, ceil(ceiling / 3))`
  slots for interactive tiers, so unattended work cannot starve users. Caps must be dynamic,
  from 10 clusters to 10,000. Amended 10/28 (#3655): a child reserve keeps waiting parents
  from holding every slot; the reserves are disjoint, filled child first, and leave at least
  one slot for top-level unattended work, so at a ceiling that can't fit both (1, 2 and 4
  with the default reserves) not every reserve is honoured in full, and the PlatformAgent
  carries a `ProfileReservesShort` warning saying which and the smallest ceiling that does.
- ~~Each durable counts against the TASKS consumer budget (#2077), so that formula includes
  them.~~ Amended 10/28: the durables live on `PROFILE_SUBMITS`, whose `max_consumers` is
  their allowance (64, so 16 profiles with both tiers). They take no TASKS slot.

### Execution

- **Interactive profiles (the cluster agent, chat) run as warm pools:** a small always-on set
  of worker pods per (profile, tier), taking tasks from the dispatcher. A pod's
  ServiceAccount is fixed, so **each (profile, tier) pair gets its own ServiceAccount, RBAC
  binding and callout identity entry** (decided 2026-10-22), and a pool serves one tier. The
  replica count comes from the budget. Between tasks every writable path the task can reach
  (workspace, `HOME`, `/tmp`, caches, staged tools) is reset, or the container restarts, and
  credentials are reissued for the next task's scope, so one task's state can't reach the
  next. A warm pool is a fixed, predictable footprint, which also suits Autopilot.
- **The unattended tier runs a Job per task:** it is bursty and latency-insensitive, and each
  run gets a clean pod.
- **Placement:** AgentProfile CRs stay in the PlatformAgent's namespace, so a profile still
  binds to the PlatformAgent in its namespace as today. The profile ServiceAccounts and worker
  pods live in a dedicated profile namespace with its own quota, rendered there by the
  operator, so profile work can never take `/session` slots (the session quota counts every
  pod in its namespace). The operator also renders the bus credentials, tier RBAC and the
  NetworkPolicy that lets that namespace reach NATS, LiteLLM and the credential proxy.

### Results and stop

- A top-level task's results and progress go back through the gateway relay to the asking
  conversation, whichever door it came in on. A child's terminal goes to its parent task.
- Stop on a conversation cancels its task tree through the supervisor path: the dispatcher
  tells warm workers to abandon the task (and resets them) or deletes the Job. It learns of the
  stop from a stop record the gateway writes beside each profile task's cancel
  (`a2a.stop.<profile>.<task>` on `DISPATCH_STOPS`), so a task it still holds queued is dropped
  rather than admitted later.

### What this spec leaves to the build

This document stops at the decisions. Further queue and dispatcher mechanics (ack timing
details, lease intervals, restart and reconnect edge cases, grant shapes) are settled in the
dispatcher's plan and PRs (#3655), with tests, rather than added here. Where a build PR finds
a decision above doesn't hold, it comes back to this spec as a change, not a workaround.

**Cross-chain admission cycles (decided 2026-10-22, "a for now").** Parents keep waiting by
staying open. If the dispatcher's tests or live runs show cross-chain cycles happening in
practice, switching to finish-and-wake (a waiting parent gives up its slot and is woken when
its children finish), which closes the whole class, is the agreed next step, and comes back
here as a spec change.

## Changes to companion specs

The build PRs amend these; this list is what they must cover.

- **`spec-subagent-profiles.md`:** the execution model (warm pools beside Jobs, with the
  dispatcher as the only fetcher and holder), held delivery with in-progress acks instead of
  "ack after Job create", the liveness lease and re-adoption after a restart, the persona
  format replacing per-profile SOUL files, the allowed-tiers list on the CR, the worker
  namespace separate from the CR's (the binding rule itself is unchanged), and the CRD doc
  comments (persona contents, `clusterRef` usage, lifecycle on warm pools).
- **`spec-nats-deployment.md`:** the `PROFILE_SUBMITS` stream, and the shared durables per
  (profile, tier, kind) on it, operator-created and read only by the dispatcher, replacing per-pod consumer
  narrowing for new submissions, and their `max_ack_pending`; and a KV bucket for per-task
  liveness leases.
- **`spec-a2a-payloads.md`:** the new submission subject
  `a2a.submit.<profile>.<tier>.<kind>.<task>`, with follow-ups and cancels staying on
  the per-task `.in`; children of a profile task are gateway-minted (the gateway stays the only
  author of submissions); a child's terminal is delivered to its parent task.
- **`spec-chatops-gateway.md`:** delegation to any profile (not only `platform`), multiple
  children, the depth cap and the same-profile nesting limit; `chat.notify` keeps its
  proactive-post role.
- **`docs/architecture/02-agent-personas.md`:** §2.3 ("no agent blocks waiting on another";
  coordination only through durable state) is superseded for profile tasks: a parent may wait
  on its children, and agents fan out to other agents through the gateway. §§4-5 and §10's
  persona-per-scope model (a Cluster Admin agent per cluster, a Developer Team agent per
  namespace) is superseded by one generic profile per role with the target as a parameter.
- **`docs/architecture/06-api-and-data-contracts.md`:** §1's tier-discriminated `Agent` CR
  (`platform | cluster-admin | developer-team`, one per cluster or namespace) and §2's
  per-tier canonical KSA are superseded by AgentProfiles plus CR-defined tiers (Read and
  Unattended to start), with an identity per (profile, tier).
- **`docs/designs/agent-communication.md`:** guiding principle 1 ("no agent-to-agent
  prompting… when delegating, through the kanban board") is superseded by gateway-minted
  delegation to profiles. Its known gap about card bodies as untrusted input carries over:
  a child's result is untrusted input to its parent.
- **What leaves with kanban:** the daily **Kanban Board Health** chat report
  (`agents/platform/cron/jobs.json`, `kanban_board_health.py`) goes when nothing files cards,
  replaced by dispatcher health (queue depth, held tasks, expired leases). The CRD field
  **`spec.harness.tuning.maxInProgress`** (kanban's in-progress limit, default 6) is replaced
  by the install-wide profile ceiling: while both exist, the ceiling defaults to
  `maxInProgress`'s value so an install's footprint doesn't jump, and `maxInProgress` is
  deprecated once `platform` is off kanban.
- **Callout narrowing** (`profile_narrowing.go`) and the CRD's identity rendering: one
  ServiceAccount and identity-map entry per (profile, tier); workers get no fetch grant on the
  shared durables and no verifier grant; the dispatcher gets both.

## Order of work

1. **The GKE Support Agent (`tse`, #3492)**, the first AgentProfile adopted from outside our
   team. It lands as a temporary in-pod profile and moves onto dispatch first.
2. **Cluster agent** with tools through the broker (#1699). It replaces the per-cluster Hermes
   profiles.
3. **Chat:** the gateway reads its own `chat` AgentProfile.
4. **One unattended investigation profile**, to exercise the unattended tier early.
5. Rewrite `platform`'s instructions and skills off `kanban_*`.

Each step moves its share of delegation off kanban as it lands. The kanban patches and guards
are deleted once nothing files cards. There is no big-bang switch.
