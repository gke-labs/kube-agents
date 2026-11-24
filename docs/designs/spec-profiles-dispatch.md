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
  ([`spec-chatops-gateway.md`](spec-chatops-gateway.md)) - doors and delegation; the scheduling
  on profiles spec ([`scheduling-on-profiles.md`](scheduling-on-profiles.md)) - `AgentSchedule`,
  unattended triggers, and migrating cron jobs off Hermes. This spec
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
  a handful of CRs, not one per cluster. A pinned profile stands in for the generic cluster
  agent, which opts in with `yieldToPinned`, not for a specialist such as `tse`: a task
  addressed to a specialist by name keeps that specialist's persona on a pinned cluster.

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
to close the gaps review found): only the **head of a chain** may start a child of its own profile, once,
and **no profile appears twice on a task's ancestor chain** otherwise. The head is a `top` task, or
the first profile task under a session turn or a wake: neither a session nor a wake's ended child
is a profile task waiting open, so the chain starts afresh there. So a task below the head never
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
   narrowing when it mints: the child's tier equals the parent's, or is `read` with no trigger below an `unattended`
   parent, whose child's profile must also list `unattended` (#6744) (and the child's profile must
   allow its tier), and the child's scope must lie inside the parent's. The gateway stays the only
   publisher of submissions. Depth is capped (as `A2A_DELEGATION_DEPTH_MAX` caps delegation today), and the
   ancestor-chain rule in [Scale rules](#scale-rules) applies (only a chain's head nests its own
   profile, once; no profile repeats otherwise). Entries a broker adds to a grant chain never change
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
  lives on the CR, never in the persona. A pin's tiers cap every task for its cluster: a
  specialist that keeps a pinned cluster's task (it doesn't set `yieldToPinned`) keeps its
  persona there, and the gateway refuses the task at a tier the pin doesn't allow.
- **Membership:** which tier a human is in comes from a mapping on the PlatformAgent (groups or
  user ids to tier), defaulting to Read. Admission refuses a mapping that assigns Unattended to
  a human. In a multi-author thread, the task runs at the minimum tier across the authors,
  keyed on the door's attested identity, as the delegation allowlist already does.

Gaps on main that the build closes (#3659):

- The `Tier` CRD exists, but nothing reads it: the verifier and the gateway keep their built-in
  tiers, and `allowedTiers` is an enum of `read` and `unattended`.
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
   1. for cluster work, a task addressed to a profile that sets `yieldToPinned` (the generic
      cluster agent) goes to a pinned profile whose `clusterRef` matches the target, otherwise
      to the addressed profile; a task addressed to a profile that takes a target and doesn't
      set it (a specialist) stays with that profile; a pinned profile addressed for any other
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

**Re-creating a durable.** The operator re-creates a profile durable only when it is really
gone. A re-created durable starts from the stream's first message with a fresh ack floor, and a
new stream leader can answer "not found" before a durable's assignment reaches it, as after a
replica change. So a durable whose tier is in the profile's `status.durableTiers` (written only
by a pass whose durable sync succeeded, and kept through passes the bus refuses) is re-created
only if it is still missing a pass later (`durableRecreateDelay`, 30s), with the card reading
`DurableRecheck` meanwhile. A new profile, or a tier new to it, gets its durables at once.
`lib.EnsureHeldConsumer` offers the refusal as a primitive (`HeldConsumerConfig.Existed`) and
leaves the timing to its caller (#6283).

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
  fails a task that fails any check with a visible notice. A verifier nobody answers gives no
  verdict: the submission stays queued and is asked about again, until a verdict or its queue
  timeout ends it.
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
  At a ceiling of 2 there is a child reserve and no interactive one. At a ceiling of 1 there
  is neither: every task runs alone, so a parent can never start a child.
- ~~Each durable counts against the TASKS consumer budget (#2077), so that formula includes
  them.~~ Amended 10/28: the durables live on `PROFILE_SUBMITS`, whose `max_consumers` is
  their allowance (64, so 16 profiles with both tiers). They take no TASKS slot.

**As built (amended 10/29, #3655).** The dispatcher is its own Deployment,
`<agent>-a2a-dispatcher`: one replica, replaced with `Recreate`, since two at once would each
enforce the ceiling against half the in-flight count. The mechanics the decisions above
leave open:

- **Settings.** The operator renders a dispatch ConfigMap, `<agent>-a2a-dispatch`, from
  `spec.harness.tuning.profiles` and the bound profiles, every value resolved, and the
  dispatcher watches it. It carries the ceiling and both reserves, each profile's tiers,
  concurrency, queue timeout and deadline (the dispatcher derives each profile's durables from
  its tiers and draining tiers, and the config is written compact; #5479), and four intervals: `leaseSeconds`
  (default 90), `renewSeconds` (20; a lease is at least three renewals),
  `restartGraceSeconds` (60) and `acceptTimeoutSeconds` (10). A change applies to new
  offers. A task already handed out keeps the intervals its offer carried.
- **Hand-off.** The dispatcher offers the task to a free warm pod on
  `a2a.dispatch.<profile>.<tier>.<pod>` and waits `acceptTimeoutSeconds` for its `.accept`.
  On an accept it writes the task's lease record, which starts the lease, and then sends
  `confirm` on that pod's subject; the worker starts only on the confirm (contract addendum
  5). An accept is a core publish, so it can be lost or late: an offer that times out is
  abandoned on its pod before the task goes to the next one, and any accept no live offer
  waits on, a late one included, is answered with `abandon`. A lost confirm costs the task,
  which fails `lease-expired`, never a second run. Nothing is written while an offer is out:
  the pod counts as busy in memory, and a dispatcher restarted mid-offer finds no record and
  dispatches the task afresh. With no free pod the submission waits over its cap and is
  redelivered. A record past its expiry doesn't keep its pod busy, and a sweep deletes records
  long expired that no held submission has (its submission aged off the stream), reading the
  bucket in one pass and logging a pass cut short. A record nobody can read (or of another
  shape) has one meaning for every reader: a started task, last seen alive at the entry's
  write, so it expires a lease after that like any record. A renewal from the pod its key
  names has the dispatcher, the bucket's one writer, write it back readable. Answers a pod
  sends for tasks it doesn't hold are held to a small budget per pod.
- **Task credentials.** With a task-signing key (a cluster seed, read from the file
  `A2A_TASK_SIGNING_SEED_FILE` names), the confirm carries two credentials the dispatcher
  mints for the pod that accepted, before the lease record is written (contract addendum 8, #3889).
  `taskCredential` (audience `nats`) is the password of the worker's task connection.
  `proxyToken` (audience `credential-proxy`) carries the cluster the verifier checked at
  admission. When an unattended trigger submitted the
  task, `proxyToken` also carries that `trigger` (`{name, origin, stream, script}`), which the verifier
  found equal to the grant's at admission (added 11/11, #4685). A `script` must also be one
  the profile allowlists in `spec.scripts`, which the operator renders into the dispatch config with
  each script's path and timeout. A script-run task's path comes from that render, never from the
  task. Every path lies under `/opt/kube-agents/scripts/`, the one place helper scripts ship in the
  worker image. The CRD and the dispatcher's config check both hold paths to it, and
  `deploy/docker/test_script_root_ownership.py` keeps what a Dockerfile puts there root-owned and not
  group- or world-writable (#5622). A task that names no cluster gets
  no `proxyToken`, except an `unattended` task a trigger started: it gets a report credential, a
  `proxyToken` with no cluster that carries the trigger. The broker admits a report credential to
  the audit-report routes only and refuses it on every other route, `/v1/exec` included (#5619). The worker sends a report credential
  only on the audit-report routes, so its other calls go as a task with no `proxyToken`'s do.
  Any other task with a trigger and no cluster gets no `proxyToken`, and the dispatcher logs the
  trigger as dropped. The tier must be `unattended` exactly, because that's the only tier a grant
  binds a trigger at. When the submission's
  `taskParameters.changeRequested` is true, the `proxyToken` also carries
  `permits: ["vcs:proposal-create"]`, which admits the worker's version-control writes at the
  broker (#3993). A permit-carrying task runs in its own one-pod Job, never on a pool pod, at
  whatever tier it was admitted at and of either kind (a confirmed chain's children and wakes
  inherit the flag, so each of them runs on a Job too): one whose tier has no Job runner is
  refused at admission (`change-needs-job`), and the permit is minted only for a task on a Job
  (#5646). Under Job mode every profile has a Job template at each of its live tiers for
  this, so the refusal is what an install without Job mode, or a profile without a
  template, gets. Otherwise it carries no `permits` key at all, never an empty list, so its
  claims are those of a token minted before the claim existed. The `taskCredential` never
  carries one, and the callout refuses one that does. Both name the task, its profile and
  tier, the pod, and the pod's UID as the pool saw it at the offer. Both expire at the
  offer's deadline plus the callout's 30s clock skew, and never later than
  `A2A_TASK_CREDENTIAL_MAX_LIFETIME` (6h by default) from the mint. An offer or an abandon
  never carries either, and renewals mint nothing. A pod with no UID in the pool isn't
  offered the task. The credentials are minted on the accept, before the lease is recorded.
  If the pod's UID changed since the offer, or the mint fails, the offer is withdrawn as a
  declined one is, and the task is offered again. With the variable unset, confirms carry neither, workers keep their
  pod-level grants, and the dispatcher warns at startup. A file that is set but can't be read
  or holds another key type stops the dispatcher at startup. The dispatcher logs a
  credential's `jti`, never the token.
- **Script-run tasks.** A script must run in Job mode: admission refuses `script-not-allowed` where the
  profile has no Job template at an unattended tier. The dispatcher resolves the script from the config
  snapshot it admitted the task on, and the confirm carries `script` (`{name, path, timeoutSeconds}`).
  Only the dispatcher may publish on a dispatch subject (`TestOnlyTheDispatcherPublishesAConfirm`). The
  one-shot worker runs the script in place of the harness when all of these hold:
  - its task credential verifies against `A2A_TASK_SIGNING_PUBLIC_KEYS`, which the operator renders into
    each Job template from the key the callout and the proxy use;
  - the credential names this task and pod, and a trigger whose `script` is the confirm's;
  - the path resolves to a regular file under the script root, owned by root, writable by no group
    or other, and neither setuid nor setgid, in directories that are the same on both the named path
    and the resolved one, and the worker isn't its owner. The file itself may be one symlink within
    the root whose target, as written, is where it resolves: a link to another link, or through a
    symlinked directory that changes where it leads, is refused. No directory on the named path may be a symlink.

  The script runs with no shell and a cleared environment. It gets the worker's `PATH`, `HOME` and three
  broker hand-off variables (`CREDENTIAL_PROXY_URL`, `CREDENTIAL_PROXY_TOKEN_FILE`,
  `CREDENTIAL_PROXY_TASK_TOKEN_FILE`), plus the task's parameters as `KA_PARAM_<NAME>`. (A model
  turn's harness, unlike a script, also gets a triggered task's stream and origin as
  `PROFILE_TRIGGER_STREAM` and `PROFILE_TRIGGER_ORIGIN`, read from the submission's authority block,
  which the dispatcher held to the grant; the broker still decides any report write from the signed
  credential's trigger.) Its bound is
  its `timeoutSeconds` within the task's deadline. It starts in a session of its own, and its kill (at
  the bound, on a cancel, abandon or SIGTERM, and once more after it exits) SIGKILLs its process group
  and every process `/proc` lists in its session; where the worker is the container's pid 1, as the
  worker image's entrypoint is, it also kills every other process in the pod's pid namespace, which
  reaches a process that started a session of its own, and reaps them. Its
  stdout, capped at 64 KiB, is the text result. Its stderr is read, capped at 16 KiB, and only its size is logged: a script can print a credential, and the log is not where a redactor's misses should land. Exit 0
  completes the task, and anything else fails it as `script-failed`, or `script-timeout` at the bound.
  A check that fails rejects the task as `script-refused`, and nothing runs. So does a parameter whose
  name can't be a `KA_PARAM_<NAME>` (upper case, at most 32 characters, where a task parameter may have
  63), and a confirm with no `script` whose credential or submission's trigger names one, or whose
  submission's authority block can't be read: a script task never runs as a model turn. A cancel on the task's `.in` kills the script and ends it `canceled`, and other messages there are
  refused. An abandon kills it and publishes nothing, since the dispatcher writes that terminal. A
  SIGTERM ends it as `worker-evicted`.

- **Write refusals.** A model-turn task's worker stops at the first write refusal the broker
  marked definitive, and reads no tool output to find it. The broker marks its 403 body
  `write_refusal: true` when a policy rule refuses a mutating kubectl verb that is not a local
  render: the `kubernetes.read-only` rule, or the profile caller's flag rule
  (`caller.kubectl-flag`), which reads a profile write's flags first. A busy, rate or timeout
  answer is never marked, and neither is a version-control refusal: the version-control skill's
  answer to a missing permit is the patch, which the task still gives. On such a refusal the in-pod broker client
  appends one JSON record (task, rule, verb, route, time) to the file the worker names in
  `CREDENTIAL_PROXY_REFUSAL_FILE`: `write-refusals` on the task token's tmpfs, made empty before
  the harness starts and removed after. The client opens it append-only, without following a
  symlink and without creating it, and a missing or unwritable file is a line on stderr. After
  each tool-result line the worker reads the file. At the first record it stops the harness
  (closed stdin, SIGTERM to its process group, SIGKILL after the kill grace) and ends the task
  `failed` (work ran first, so not `rejected`) with `reason: write-refused`, whose detail names
  the record's rule and verb and carries no other output. Only a record naming one of those two
  rules, with a verb of one or two lowercase words, counts. A `gcp.read-only` refusal, a refused read spelling, a local render, an
  MCP relay refusal and an API 403 don't stop the task. The broker refuses the write whether or
  not the task stops, so a lost record only lets the task run on. Every route a definitive write
  refusal can take to the model runs through the client: `kubectl` and `gcloud` on a profile
  worker are the broker shim. The
  routes that don't (the MCP relay, the stock kubectl binary run directly, a direct HTTP call to
  the broker or the cluster API, local git, the harness's file and web tools) carry no definitive
  write refusal; `a2a/worker-adapter/refusal.go` lists them.

- **Holding.** The submission stays unacknowledged for the task's whole life. This replaces
  the subagent spec's ack after Job create. The worker renews on its `.lease` subject every
  `renewSeconds`, and the dispatcher extends the record by `leaseSeconds`. When the worker
  has published the task's terminal it sends a renewal with `final` set (`LeaseRenewal.Final`).
  The dispatcher has no read on `.events`, so that renewal is how it learns the task ended:
  it turns the record into a tombstone and frees the slot, then acks the submission and waits
  for the server to confirm the ack (an ack that never lands, as on a durable the operator
  deleted, leaves the tombstone to answer the redelivery). An expired lease or a passed
  deadline also tells the worker to abandon the task.
- **Tombstones.** `PROFILE_SUBMITS` keeps acked submissions for its 72h max age (limits
  retention), so a durable re-created with DeliverAll replays every task it ever held. Every
  outcome the dispatcher sees or writes is final: a `final` renewal, a `lease-expired` or
  `deadline-exceeded` failure, a refusal at admission. On each it writes a tombstone before
  it acks: the lease record under the same key with `finishedAt` set
  (`LeaseRecord.FinishedAt`); a refusal, which no pod held, under the pod token `_refused`,
  and before its `rejected` terminal, so a restart never re-admits a refusal already reported.
  A submission that finds a tombstone is acked and nothing else: it is never offered,
  adopted or given a second terminal, so a grant given since never runs a task once refused.
  A tombstone keeps no pod busy and is never renewed or re-adopted; it is told from a
  running lease by `finishedAt` alone, never by expiry, so an expired running lease is still
  re-adopted under the restart grace and failed. The sweep deletes a tombstone, by revision,
  once it is older than the stream's retention plus an hour. The retention is the dispatch
  config's `submitRetentionSeconds`, never less than `lib.ProfileSubmitRetention` (72h). The
  operator renders the longer of the constant it creates the stream with and the live stream's
  max age, which the provision Job reads back and reports when it is longer
  (`status.profileSubmitMaxAgeSeconds`). The operator never edits an existing stream, so a
  stream an older release created longer, or one edited by hand, is covered this way, once the
  Job's next run (at most a day) has read it. The retention is capped at a year
  (`lib.MaxSubmitRetention`): a stream with no age limit, or a longer one, keeps tombstones that
  long (#4948).
- **Restart.** A re-adopted task's record gets `restartGraceSeconds` before an expired lease
  fails it, which covers the gap `Recreate` leaves. The record carries the deadline and
  intervals its offer did, so a re-adopted task keeps them whatever the config says now.
- **Grants.** The callout fixes a connection's grants when it connects, and the dispatcher's
  name each durable and each profile's `.supervisor` (with bucketed grants, on its bucket
  connections). When the config adds a durable or a profile
  the dispatcher reconnects, retrying with backoff while the callout's map catches up. Tasks
  it holds keep running across the reconnect, which drains the old connection so answers it
  had already received still arrive. The NATS spec's `PROFILE_SUBMITS` and callout
  amendments of 10/28 ([`spec-nats-deployment.md`](spec-nats-deployment.md)) say the same
  from the bus's side.
- **A profile that leaves.** A profile removed from the install, or one whose identity stops
  rendering, drains (#5074). The operator keeps it in the dispatch config with no durables and
  `drainingUntil` set to the longest deadline any task can carry (six hours, the ceiling on
  `maxTaskDeadlineSeconds`) plus fifteen minutes past the moment it left, so nothing new is
  dispatched for it. A task keeps the deadline it was confirmed with, so a lower limit set since
  doesn't shorten the window. The dispatcher's identity keeps its `.supervisor` and
  stop-record grants and drops its durable grants, so the terminals of the tasks it still holds
  reach their requesters. The applied dispatch config is the state: a profile it lists that the
  render no longer has starts draining, and one already draining keeps its time until that
  passes, when it leaves the config and the identity together. The set is read once a pass for
  both, and only an entry the render could itself produce (subject-token name and tiers) is
  carried forward, since its name becomes a grant. A held task whose submission goes back to the
  stream when the dispatcher restarts mid-drain has no durable to come back on.
- **Off by default.** The operator renders the dispatcher only when its `A2A_DISPATCHER_IMAGE`
  is set, until profiles dispatch end to end; with it unset there is no dispatcher and no
  identity for one. Amended 11/10: on by default, at the release set's image (with
  `A2A_DISPATCHER_IMAGE` the override), and off only at `A2A_PROFILE_DISPATCHER=false`, which
  the chart sets from `operator.profileDispatcher.enabled`. Its identity-map entry grows with
  the profile count, and one entry can't be split across shards, so it fills one shard near 650
  profiles at two tiers. The dispatch config, one ConfigMap, has its own limit. Since #5485 an unattended profile's Job template is its own `DispatchJobTemplate`, and the config carries only a reference to it, so that limit is reached first only by profiles that declare many parameters (#5479). The operator binds profiles oldest first (creation
  time, then name) while both fit, less a reserve for draining profiles, and refuses newer ones with
  `IdentityReady` reason `DispatcherCeiling`; the dispatcher serves the rest. The PlatformAgent's
  `DispatcherCeiling` condition warns at four fifths of either budget and reads
  `ProfilesRefused` while any profile is left out. Past either budget anyway, which only draining
  profiles can cause, the operator runs no dispatcher rather than write a map or a ConfigMap the
  API server refuses. It rewrites the dispatch config with the draining profiles alone and each
  drain's deadline, so the dispatcher comes back once those drains run out. Adding a tier to an older profile can leave the newest one out. #5381 lifts the
  ceiling, by deriving the grants per connection as sessions are, and blocks the 2,500-profile
  target.
  Its per-profile grants are out of the identity map and on 32 bucket connections the callout
  derives (spec-nats-deployment.md); `A2A_DISPATCHER_BUCKETED_GRANTS=false` on the operator puts
  them back in the map on one connection. The operator and the dispatcher move together: a
  dispatcher from before the bucket connections binds nothing under a split map and stays not
  Ready. With bucketed grants, the dispatcher's entry is a fixed size whatever the profile count,
  so the ceiling above measures that entry and cuts on the dispatch config's budget alone.
- **Terminal notice.** A task the dispatcher ends gets a final status on its `.supervisor`
  subject (`lib.TaskSupervisorSubject`, or the six-token `lib.ProfileSupervisorSubject` when
  the dispatch config's `profileSupervisorForm` is `profile`; readers read both, until a reader
  given `A2A_PROFILE_SUPERVISOR_CUTOFF` stops taking the five-token form as a profile task's
  terminal when the server stored it after that time; contract addendum 6, #5301). The operator
  renders that env on the gateway and the MCP server once the `ProfileSupervisorForm` condition
  has latched True, as the condition's `lastTransitionTime` plus ten minutes, in RFC3339 UTC (on
  an install that latched earlier, that time may already be in the past at the upgrade); an
  unlatched install renders none, so its readers take both forms and nothing rolls. The gate
  holds against the gateway, whose grant still reaches the five-token form, so both its inputs
  are the operator's: which addressees are profiles comes only from the profile names file it
  renders beside the env (`A2A_PROFILE_NAMES_FILE`, the `<agent>-a2a-profile-names` ConfigMap:
  the admitted and draining profiles, the same names nats.conf denies the gateway the
  supervisor subjects of), never from the gateway's session record, `.in` traffic, the
  console's status lines or the directory. The gateway's Role writes no ConfigMap but its
  home-channel record and cannot write its own Deployment, and the MCP server has no API
  access. A reader whose names file has never been read takes every addressee as a profile;
  the file is re-read every 30 seconds, and a later read that fails keeps the names last read.
  The gateway's relay doesn't settle a refusal it made before the names were read, since the
  names may take it: it gives the event back to the stream for 30 seconds, up to 20 times, then
  settles it with an Error and counts the drop once. The 20 counts only these holds, by stream
  sequence in the gateway's memory, not the message's deliveries: a give-back after a failed
  KV write, a rollout's give-back or a lapsed ack wait doesn't spend it. A held event the names then take renders
  when it comes back, so it can render after later events of the same task; that ordering
  effect is known and accepted.
  The stored time is the server's, and only where the server alone writes it: a consumer
  delivery's metadata, or the API's JSON reply to a message get on a stream without
  `allow_direct`. Never a direct get's headers: the server appends its `Nats-Time-Stamp` after
  the publisher's own header block, and a blank line inside that block ends it for the
  client's parse, hiding the server's stamp behind a forged one. A direct get therefore gives
  no time; the gateway's busy count and empty read re-read a profile task's five-token subject
  on a short `DeliverLastPerSubject` consumer for its time, and a time that can't be read is
  unknown and refused where it would decide. The replay's horizon is the stream's last
  sequence from `STREAM.INFO`, never a direct get's `Nats-Sequence`, which sits in the same
  header block. A cutoff that doesn't parse is taken as the Unix
  epoch, so every five-token terminal on a profile task is refused; it is logged at Error and
  shown on the gateway's `profile_supervisor_cutoff_timestamp_seconds` series and the MCP
  server's `/healthz`. The terminal never goes on `.events`, which stays
  the executor's alone: `rejected` when it is refused at admission (nothing ran), `failed`
  once a worker had it. The message is `reason: <code> - <detail>`, the executors' form, and
  the gateway's relay and the MCP server's task reader read `.supervisor` beside `.events`,
  under an agreement policy that names the dispatcher (`lib.DispatcherSession`) as a
  supervisor beside the gateway. The codes are
  `tier-not-allowed`, `tier-withdrawn` (queued at a tier its profile now drains, #3659),
  `no-worker` (an unattended task with no runner, under Job mode below),
  `change-needs-job` (a confirmed change whose tier has no Job runner), `verifier-refused`, `parameters-invalid`,
  `queue-timeout`, `script-not-allowed` (the trigger names a script its profile's `spec.scripts` doesn't list, or one
  where the profile has no Job mode at an unattended tier, #5622), and for a child of an AgentProfile task
  `parent-unchecked`, `parent-cycle` and `outside-parent-scope` (the dispatcher's re-check of the
  gateway's chain and scope rules, from the parent's chain and scope in the child's `via`) at
  admission, and `lease-expired`
  and `deadline-exceeded` while it holds, and on a `canceled` terminal `stopped` (the
  conversation's stop) and `tier-ended` (an administrator ended its tier's drain, #5850). A terminal is retried until it lands, except for a
  profile that has left the install config, and on a latched install a refused terminal (below). A dropped profile's `.supervisor` grant goes at the next reconnect,
  so after `DroppedTerminalGiveUp` (fifteen minutes, well past a brief drop from the config) the
  task is settled without one: its slot is freed, the terminal is lost, and the loss is logged as
  an error (#5074).
  On a latched install (`profileSupervisorForm: profile`) a refused six-token terminal is never
  written on the five-token form instead: readers past the cutoff don't take that form as a
  profile task's terminal, so it would free the slot while the requester never saw the end. The
  terminal is retried, a refusal at admission as well as a held task's. Only a permission refusal
  counts toward the bound: an attempt during which the server refused the six-token publish for
  the connection's permissions (nats.go's async `Permissions Violation for Publish to` that
  subject). Any other failure, such as a disconnect, no responders, a timeout with no refusal or a
  JetStream API error, restarts the bound, so a NATS or `TASKS` outage of any length is retried and
  the terminal lands after it. Once refusals have run for `DroppedTerminalGiveUp`, the task is
  settled without one: tombstoned, its slot freed,
  its submission settled, an error logged under the reason `terminal-unpublished`, and the
  dispatcher health line's `supervisorGiveUps` counted. The requester sees no terminal: the
  gateway ends a task with no events as never started, and holds one with executor events until
  the user's stop. A refusal needs a grant that never reached the dispatcher's connection; the
  regrant normally clears that in seconds, well inside the bound. A profile that has left the
  install config keeps its own give-up and log line above, not `terminal-unpublished`. An
  unlatched install keeps retrying, as before.
  A terminal's envelope id is fixed per task, so a retry after a lost ack is a duplicate the stream
  drops. The duplicate window is keyed on the id alone, whatever the subject, so on the six-token
  form a duplicate ack is checked: a direct get of the task's six-token subject must find the
  dispatcher's final status under the terminal's own envelope id, so another tier's or kind's
  terminal for the same task id doesn't confirm it. With none there, the terminal is published
  again under a fresh id, kept for the rest of that retry loop so its re-publishes dedupe onto
  each other. A re-publish that fails is an ordinary failure, retried under the bound above. Only
  a fresh id taken as a duplicate on its first send takes the `terminal-unpublished` settle at
  once. A direct get that fails is not taken as the write landing: the terminal is re-published
  under a fresh id the same way, so a retry whose first write did land can leave two identical
  terminals, and readers fold past the second.
- **Breaker (#3655 PR 6a).** A run of `breakerThreshold` lease expiries in a row on one
  (profile, tier), default 3, opens its breaker. A Job that never starts counts too. While it
  is open, for
  `breakerCooldownSeconds` (default 300), new submissions there are held queued, as over a
  cap, never refused: a refusal would let one input that keeps killing workers fail every
  other user's task on that tier. They still end with `queue-timeout`. Then it is half open:
  the oldest held submission is admitted alone as the probe of that opening, and the rest
  wait while it runs; a child of the probe (sharing its correlationId) is part of its attempt
  and is admitted. The next expiry opens it again. Below the threshold any finished task
  resets the run; once it has opened, only that opening's probe closes it, by finishing or
  by running to its deadline still renewing. A stop never opens or closes the breaker by
  itself, but its worker's silence counts as a loss: a worker that stops renewing, after a
  stop or not, is counted, whether the lease runs out first or the stop's grace does (the
  grace is never shorter than a renewal and two ticks, so one silent since before the stop is
  always past the count's threshold when it ends). The
  state is in memory, and the dispatcher's health lists open and half-open breakers. A held
  task isn't told it is held yet (#4362). The breaker guards the tier's runner: at an
  unattended tier that is Job mode, and at any other tier the warm pool. A permit-carrying
  task's own Job at a pool's tier (#5646) stays clear of that breaker: it doesn't wait on it,
  is never its probe, and its finish or lost worker neither opens nor closes it.
- **Job mode (#3655 PR 6b).** With a Job runner, an unattended task runs on a Job of its own:
  the dispatcher launches it, waits up to `jobStartSeconds` (default 300) for its pod to be
  Ready, and offers that pod the task once, with the same offer, accept and confirm as a warm
  worker. The Job's pod is the warm worker's pod in one-shot mode (`PROFILE_WORKER_ONE_SHOT`):
  it serves that one task and exits. A Job that never comes up, fails or doesn't take the task
  is deleted, and the task fails `no-worker`. Jobs count against the same caps. The Job is
  deleted when the task's outcome is settled, never when the dispatcher stops, so a restart
  re-adopts the task; the lease record says the task ran on a Job (`onJob`), so the Job of a
  task re-adopted at any tier goes when it ends. A Job's name is a hash of the task's whole
  key, its kind included, and a Job pod already holding a task is never offered another.
  A read-tier task with no permit stays on the warm pool, and a Job an earlier pass launched
  for it, while its submission (replaced under the same subject) still carried a permit,
  goes first. Isolating the credential this way leaves the task's bus traffic where it was:
  the Job's pod shares the bus with every other worker (#3889).
  The dispatch config doesn't carry the Job's pod template. It carries one hash for the
  renderings of the profile's `DispatchJobTemplate`s in the profile namespace, the params the
  Job admission policy holds each Job to (`jobTemplateRef`), and `perTier: true` (#5646):
  the profile has one per live tier, each under its tier's ServiceAccount. The
  unattended-mode tier's param is the profile's own, `kube-agents-dispatch-job-profile-<profile>`,
  and any other tier's is `kube-agents-dispatch-job-tier-<profile>.<tier>`, where only
  permit-carrying tasks run. The dispatcher derives each name from the task's (profile,
  tier), never from the config (#5479), and holds the template's own profile and tier labels
  to the task's. It reads that object when it launches the Job, with `get` alone on those
  objects, and uses it only while the object's `kubeagents.x-k8s.io/dispatch-template-hash`
  annotation matches. The one hash covers every tier's rendering, so a change to any of them
  moves it on every param of the profile, and the config's entry stays one hash wide however
  many tiers the profile has. A reference without `perTier` is the unattended-mode tier's
  template alone, as an operator from before #5646 sends it. The admission policy is
  unchanged: it holds a Job to the template of the ServiceAccount it runs as, and those are
  unique per (profile, tier). It reads every param on each Job, so admitting one takes
  longer as params grow: about 1.3s at 2,500 one-tier profiles and 2.5s at 2,500 two-tier
  ones, against a Job's cold start of 30 to 90s. Those are measurements, not a gate:
  `TestTheJobPolicyAtTwentyFiveHundredTwoTierProfiles` (envtest) logs them only when
  `KUBE_AGENTS_JOB_POLICY_SCALE` is set, and asserts no bound. A param whose rendering,
  labels, template and defaulting API server are all current isn't defaulted or applied
  again on the next pass. Any mismatch, a
  missing param, or one with no containers keeps that profile's task queued, shows on the
  dispatcher's health as a Job admission error, and leaves every other profile alone. The
  operator writes params before the config, so the usual transient is a param ahead of the
  dispatcher's cached config. The hash says which rendering a param holds; it is the
  writer's claim, not a digest of the param's contents, and only the operator and cluster
  admins can write params. Copying the template into the config made a two-tier unattended
  profile's entry about 4.4 KB, which put the config's ConfigMap past 1 MiB at about 240
  such profiles. A dispatcher older than this reads only an inline template, so it refuses
  unattended tasks `no-worker` under an operator that sends the reference: move the
  dispatcher's image with the operator.
  Without a Job runner or a Job template, an unattended task has no worker (the operator builds
  warm pools for the read tier only), so admission refuses it at once, `no-worker` ("no runner
  for unattended on this install: Job mode is off"), rather than leave it queued until its
  queue timeout. A pool that serves the tier, were there one, would take the task instead.

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
  rather than admitted later. The gateway writes the record today, and the stream and the
  dispatcher's reader are still to be built.

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
- **Callout narrowing** (`profile_narrowing.go`; the dispatcher's bucket connections in
  `dispatcher_narrowing.go` and `a2a/lib/dispatchgrants.go`) and the CRD's identity rendering: one
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
