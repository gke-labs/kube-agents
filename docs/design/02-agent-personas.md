# Design 02: Agent Personas

**Status:** ✅ Agreed

**Overview:** [README.md](README.md) · **Depends on:** [01-vision-scope.md](01-vision-scope.md)

---

## TL;DR

`kube-agents` defines **three agent personas**, one per level of the Kubernetes containment
hierarchy: the **Platform Agent** (1 per project), the **Cluster Admin Agent** (1 per cluster), and
the **Developer Team Agent** (1 per namespace). Each persona shares a common anatomy — a `SOUL.md`
identity, a config, a scoped skill set, memory, event triggers with a heartbeat backstop, and a
controller-reconciled pod — but differs in **scope, authority, skills, and permissions**.

They form a **cascading hierarchy**: each layer holds authority over the layer beneath it and
provisions it — but always through the declarative workflow (CI applies; the controller reconciles), never by direct mutation.
This is the end-state roster; the Platform Agent exists today, the other two are coming soon.

---

## 1. The roster

| Persona                  | Scope                  | Cardinality     | Owns / governs                                                               | Bounded by                                         |
| ------------------------ | ---------------------- | --------------- | ---------------------------------------------------------------------------- | -------------------------------------------------- |
| **Platform Agent**       | GCP/cloud **project**  | 1 per project   | The fleet: clusters, cross-cluster policy, global RBAC, Cluster Admin Agents | Human platform team + project-level approval gates |
| **Cluster Admin Agent**  | A single **cluster**   | 1 per cluster   | Cluster internals: node pools, add-ons, namespaces, Developer Team Agents    | Platform Agent policy + project guardrails         |
| **Developer Team Agent** | A single **namespace** | 1 per namespace | Workloads within its namespace                                               | Cluster Admin policy + cluster/project guardrails  |

Every persona serves SRE critical user journeys within its own scope (see
[01-vision-scope.md](01-vision-scope.md) §3); SRE is not a separate persona.

---

## 2. Shared anatomy of an agent

All three personas are the same _kind_ of thing — a scoped, persona-driven agent — assembled from
the same parts. This uniformity is what makes the roster extensible.

| Part                     | What it is                                                                                                                                        | Current reference                                                        |
| ------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------ |
| **Identity (`SOUL.md`)** | The persona's core instructions, truths, and behavioral guardrails                                                                                | `agents/platform/SOUL.md`                                                |
| **Config**               | MCP servers, toolsets, memory, plugins available to the agent                                                                                     | `agents/platform/config.yaml`                                            |
| **Skills**               | Scoped, loadable capabilities (each a `SKILL.md` + assets/scripts)                                                                                | `agents/platform/skills/`                                                |
| **Governance SOPs**      | Standard operating procedures the agent follows for recurring duties                                                                              | `agents/platform/governance/`                                            |
| **Memory**               | Durable, multi-user memory (pluggable provider)                                                                                                   | `plugins/memory/multiuser_memory/`                                       |
| **Triggers + heartbeat** | Event triggers (watches / alert & GitHub webhooks) for reactivity, plus a scheduled tick as backstop — driving proactive audits & drift detection | `INSTALL.md` §3, `agents/platform/cron/jobs.json` (+ Hermes event hooks) |
| **Deployment**           | A controller-reconciled pod (Hermes harness) with a scoped read-only SA                                                                           | kube-agents controller (`k8s-operator/`, extended)                       |
| **Integrations**         | Chat entrypoint (Google Chat/Slack), GitHub for declarative PRs                                                                                   | `PlatformAgentIntegrationSpec`                                           |

**Design principle:** a new persona is defined by _changing the fills, not the frame_ — a different
`SOUL.md`, a scoped skill set, and scope-appropriate permissions, deployed as an **`Agent` CR**
(running the Hermes harness) with a different `tier`/`scope` (§8).

Every persona also exposes its **own human chat entrypoint**, one per audience: platform teams talk
to the Platform Agent, cluster admins to the Cluster Admin Agent, and developer teams to their
Developer Team Agent. Each persona is a genuine front door for its layer, not a silent internal
tier. _How_ a human addresses a specific agent — by direct handle, slash command, or natural
language through the `@kage` gateway — is defined in §2.4.

### 2.1 Skill allocation

Skills are scoped to the persona whose authority they match. The starting allocation of today's
skill set:

| Skill(s)                                                                                         |     Platform     |  Cluster Admin  |  Developer Team  |
| ------------------------------------------------------------------------------------------------ | :--------------: | :-------------: | :--------------: |
| `gke-cluster-creator`, `gke-cluster-lifecycle`                                                   |        ✅        |                 |                  |
| `gke-cost-analysis`                                                                              |        ✅        |                 |                  |
| `github-issue-resolver`                                                                          |        ✅        |                 |                  |
| `kube-agents-observability` (harness self-obs)                                                   |        ✅        |                 |                  |
| `gke-multi-tenancy`                                                                              | ✅ defines model |   ✅ applies    |                  |
| `gke-compute-classes`, `gke-networking-edge`, `gke-storage`, `gke-backup-dr`, `gke-reliability`  |                  |       ✅        |                  |
| `gke-app-onboarding`, `gke-manifest-generation`, `gke-productionize`, `gke-inference-quickstart` |                  |                 |        ✅        |
| `gke-workload-scaling`, `gke-workload-security`, `gke-workload-troubleshooting`                  |                  |                 |        ✅        |
| `gke-observability`                                                                              |  ✅ fleet view   | ✅ cluster view | ✅ workload view |
| `submit-suggestion` (declarative change submission)                                              |        ✅        |       ✅        |        ✅        |

`submit-suggestion` and `gke-observability` are cross-cutting — every tier submits declarative
changes and observes, each scoped to its own authority. This allocation is a starting point; skills
may be re-scoped as the personas mature.

### 2.2 Agents are read-only; a pipeline actuates

Every persona is **read-only on all Kubernetes and cloud APIs.** No agent ever writes to a cluster
or cloud API directly. The only write an agent performs is committing a proposed declarative change —
**KCC YAML or Terraform HCL** — to the **GitOps repository** (a PR, via a brokered short-lived token).
The actual application of that change is done by the **customer's CI/CD pipeline** (GitHub Actions,
CircleCI, or whatever they already run) — which holds the scoped write permissions, not the agents
(agent pods themselves are reconciled by the **kube-agents controller**, §8). kube-agents is
**unopinionated** about the pipeline and integrates with existing customer infrastructure. See
[04-workflow-model.md](04-workflow-model.md) §1.1 for the reference stack and
[03-security-model.md](03-security-model.md) §3 for enforcement.

This is a deliberate safety property: because agents cannot mutate directly, a subverted agent's
worst case is a _proposed_ change that still faces the review gate — never a live cluster write.

**Each agent has its own read-only identity, reachable only by trusted humans.** The **kube-agents
controller** reconciles each agent's pod bound (by `serviceAccountName`) to its own **Kubernetes
ServiceAccount** — plus a GCP service account via **Workload Identity where it needs cloud access**
(K8s-only agents need no cloud SA). That read-only, tier-scoped identity is the
agent's **ceiling**, and access to the agent is limited to authenticated, allowlisted (trusted) humans.
So no human can drive an agent to mutate (read-only + PR gate) or to read outside its tier. In v1 the
agent's authority is **not** down-scoped to the individual requester — that delegate model (closing the
confused-deputy gap per-request) is deferred hardening. See
[03-security-model.md](03-security-model.md) §3–§4a.

> **Delta from current state:** agents today hold direct-mutation tools (the `create_cluster` MCP
> tool and a `gke` MCP server bound to `container.googleapis.com`). The end state removes direct
> mutation from agents entirely; see [01-vision-scope.md](01-vision-scope.md) §6.

### 2.3 Coordination is indirect (shared state, not direct calls)

Agents **never call each other directly** — there is no agent-to-agent RPC or API. They coordinate
through **shared state**, reacting to it via **event triggers where a signal exists** (Kubernetes
watches, alert/GitHub webhooks) with a periodic **heartbeat as the backstop** ([04](04-workflow-model.md)
§4). Two kinds of state serve two distinct purposes, each with the tool suited to it:

| State layer             | Purpose                                                                                                        | Mechanism                                                                                                       |
| ----------------------- | -------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------- |
| **Declarative / infra** | Desired infrastructure state; the shared source of truth                                                       | **GitOps repository** — agents propose (read-only, via PR); the customer's CI/CD pipeline applies               |
| **Curated knowledge**   | Durable, shareable know-how: SOPs, cluster blueprints, runbooks, metric/tenancy definitions, cross-agent notes | **OKF** (Open Knowledge Format) — markdown + YAML frontmatter in git; agents read/update, humans curate as code |

A third layer — **semantic/cognitive recall (mem0/Qdrant)** — is **deferred post-v1** (see the note
below); v1 coordinates on GitOps + OKF alone.

Runtime **session state** (conversation transcripts, per-user profile facts, mid-task scratch) is a
_separate_ concern — high-frequency, ephemeral, per-user — handled by the existing gateway store
(`session_db.sqlite` + the `multiuser_memory` provider, which isolates state per `user_id`; see
`agents/platform/plugins/memory/multiuser_memory/`). It belongs in neither OKF nor mem0.

How coordination flows: a parent provisioning a child, or an escalation that becomes a change, is a
GitOps commit others observe; an observation or escalation _not yet_ a change is written to curated
knowledge (OKF). Nothing is a direct call. This indirection keeps tiers
loosely coupled and is what makes failure isolation
([04-workflow-model.md](04-workflow-model.md) §6) possible: no agent depends on another being online
at request time.

> **Why OKF for v1 (mem0 deferred):** OKF is the durable, human-curatable, git-backed _knowledge_
> layer — a natural fit for read-only agents that propose via PR and humans who review as code, and it
> adds no new infrastructure. A **semantic-recall layer (mem0/Qdrant) is deferred post-v1**: it is a
> stateful vector store whose value (embedding retrieval a flat markdown corpus can't do) is
> speculative until git/grep/embedding-over-OKF is shown insufficient — add it only on evidence. (The
> file-based `multiuser_memory` choice was about **per-user session isolation in the shared gateway**,
> a separate concern from these coordination layers.)

### 2.4 How humans address agents (the ChatOps gateway)

Humans reach agents through chat (Google Chat / Slack). Because the end-state roster spans three
tiers across many scopes, a human needs an unambiguous way to say _which_ agent they mean.
kube-agents provides a single chat **gateway** — the **`@kage`** bot — as the front door, and
supports **three ways to address an agent**, in strict precedence (deterministic first, inference
last):

| #   | Mode                            | Example                                                          | How the target is resolved                                           | Inference? |
| --- | ------------------------------- | ---------------------------------------------------------------- | -------------------------------------------------------------------- | ---------- |
| 1   | **Deterministic slash command** | `@kage /devteam-charlie why is checkout erroring?`               | Slash command → the handle it names; constant-time dispatch          | No         |
| 2   | **Direct mention (handle)**     | `@cluster-bravo drain node-7`                                    | The `@<tier>-<scope>` handle → its `(tier, scope)` — an alias lookup | No         |
| 3   | **Natural-language routing**    | `@kage why is my app crashing on the bravo cluster, charlie ns?` | The gateway's NL router infers tier + scope from the text and routes | Yes        |

**Handles are derived, not a registry.** An agent's handle is its `<tier>-<scope>` name (§6.1) —
`@platform-<project>`, `@cluster-admin-<cluster>` (short alias `@cluster-<cluster>`), and
`@developer-team-<namespace>` (short alias `@devteam-<namespace>`). Each handle maps
deterministically to the unique `(tier, scope)` **`Agent` CR** the controller already keys
cardinality on (§8), so there is no separate routing table to drift
([06](06-api-and-data-contracts.md) §2b).

**Precedence: deterministic over inference.** A slash command (1) or an explicit handle (2) always
wins and spends **no** inference — the same "prefer deterministic over probabilistic" principle the
workflow model applies to push-over-poll ([04](04-workflow-model.md) §4). Natural-language routing
(3) is the convenience fallback for humans who don't know the exact handle; when the router's
confidence is low it **asks a clarifying question rather than guessing**. Once a thread is routed,
follow-ups **stick to the same agent** (thread affinity via the session store,
[06](06-api-and-data-contracts.md) §6) unless re-addressed.

**Direct handles _are_ the per-audience entrypoints.** Mode 2 is exactly the "own human chat
entrypoint, one per audience" above: cluster admins reach `@cluster-<cluster>`, dev teams reach
`@devteam-<namespace>`. The `@kage` gateway (modes 1 and 3) is a **routing front door over the
separate per-tier agent pods** — _not_ a shared "one pod hosts many agents" multiplexer (that
co-located design is deliberately deferred, [08](08-agent-runtime-and-identity.md) §3), and _not_
an agent calling another agent (coordination stays indirect, §2.3). It routes a _human's_ message
to the addressed agent; agents still never call each other.

**Routing is not an authorization signal.** Which agent a message reaches is a _convenience_, never
a privilege grant. The gateway enforces the target agent's trusted-human allowlist (`AllowedUsers`)
**before** dispatch ([03](03-security-model.md) §4a), and the NL router's output — like all model
output — is never trusted as an authz signal ([03](03-security-model.md) §1). So a mis-route can
only ever land on an agent the human is _already_ allowed to reach, still bounded by that agent's
read-only, tier-scoped ceiling. Every turn is audited with the requester, the resolved agent, and
the routing mode ([06](06-api-and-data-contracts.md) §2b, §8).

---

## 3. Persona: Platform Agent (project scope)

**Cardinality:** 1 per project. **Exists today** (`agents/platform/`).

### Role

The senior custodian and **architect of the fleet and of the other agents**. It is the primary
human chat entrypoint into the harness and the authority at the project level.

### Responsibilities

- Fleet lifecycle: propose and oversee cluster provisioning, upgrades, and deprecation.
- **Provision and govern Cluster Admin Agents** (one per cluster it owns) — see §6.
- Cross-cluster governance: global policy propagation, standardization, compliance audits, fleet
  cost/capacity analysis (see `agents/platform/governance/`).
- Establish the multi-tenancy _model_ and global RBAC boundaries that lower layers inherit.
- Fleet-wide reliability CUJs: version skew, security-baseline drift, IaC drift.

### Authority & limits

- **Read-only, scoped to its one project** (the project's clusters/fleet) — it cannot read or reach
  another project. It proposes changes — including child `Agent` CRs — to the GitOps repo; it holds
  no direct cluster/cloud write (see §2.2, [03](03-security-model.md) §3).
- All infrastructure mutation is declarative (git-reviewed + CI/CD pipeline), never direct `kubectl` (per
  `SOUL.md §1`, §4).
- **Must not** reach _inside_ a namespace to operate workloads — that is the Developer Team Agent's
  scope. The Platform Agent sets the guardrails; it does not do the tenant's work.

---

## 4. Persona: Cluster Admin Agent (cluster scope)

**Cardinality:** 1 per cluster. **Coming soon** (new `Agent` CR + Hermes profile, §8).

### Role

The custodian of a **single cluster**. It operates within one cluster and owns everything cluster-
scoped, bounded by the policy the Platform Agent sets at the project level.

### Responsibilities

- Cluster internals: node pools / compute classes, cluster add-ons, cluster-scoped policy and
  quotas, networking edge config.
- **Provision and govern Developer Team Agents** (one per namespace it hosts) — see §6.
- Namespace/tenant provisioning within the cluster, applying the isolation model handed down from
  the Platform Agent (RBAC, NetworkPolicies, ResourceQuotas).
- Cluster reliability CUJs: node health, cluster-scoped rollouts, cluster capacity.

### Authority & limits

- **Read-only, scoped to its one cluster** — it cannot read or act on any other cluster or at the
  project level ([03](03-security-model.md) §3).
- Cannot override project-level policy from the Platform Agent — it operates _within_ those
  guardrails and escalates upward when a change requires project authority.
- **Must not** operate workloads inside a namespace — that is the Developer Team Agent's scope. It
  provisions and bounds namespaces; it does not do the tenant's workload work.
- Like all personas, mutates only through the declarative workflow, not directly.

---

## 5. Persona: Developer Team Agent (namespace scope)

**Cardinality:** 1 per namespace. **Coming soon** (new `Agent` CR + Hermes profile, §8).

### Role

The self-service agent for a **single developer team**, confined to **one namespace**. This is the
agent most application developers interact with day to day.

### Responsibilities

- Workload lifecycle within the namespace: onboarding, manifest generation, scaling (HPA/VPA),
  productionizing.
- Workload troubleshooting, observability, and workload-level security within the namespace.
- Workload reliability CUJs: debugging unhealthy workloads, right-sizing, rollout safety — all
  scoped to its namespace.

### Authority & limits

- **Read-only, scoped to its one namespace — a hard boundary at the namespace edge.** It is provably
  unable to read or affect other namespaces or escalate to cluster/project scope. This isolation is the
  load-bearing security property of the whole model (enforced by its per-pod SA, per
  [03-security-model.md](03-security-model.md) §3).
- Cannot change cluster- or project-level configuration; it requests such changes upward from the
  Cluster Admin Agent.
- Mutates workloads only through the declarative workflow.

---

## 6. Relationships: cascading provisioning within a declarative workflow

The three personas form a **cascade** that mirrors containment: each layer owns the lifecycle of
the layer beneath it.

```
Platform Agent  (1 / project)
   └─ owns lifecycle of →  Cluster Admin Agent  (1 / cluster)
                              └─ owns lifecycle of →  Developer Team Agent  (1 / namespace)
```

**Authority vs. mechanism — the important distinction.** "Provisions the next layer" describes
_authority_, not a bypass of the safety model. A parent agent never directly mutates the cluster to
spawn a child. Instead it **authors a declarative request** — the child's **`Agent` CR + its
read-only identity manifests** (§8) — submitted through the active GitOps workflow (e.g. via
`submit-suggestion`); after human approval + merge, the **CI/CD pipeline applies it and the controller
reconciles** the child as a running, scoped agent. So:

- The Platform Agent _proposes_ a **cluster-admin** agent (subject to human/project approval gates);
  the controller reconciles it with cluster-scoped read-only identity.
- Each Cluster Admin Agent _proposes_ **developer-team** agents for the namespaces in its cluster;
  the controller reconciles them with namespace-scoped read-only identity.

**Escalation flows the other way.** A lower agent that needs a change outside its scope escalates a
request _upward_ to its parent — **indirectly, via shared state** (§2.3), not a direct call — which
the parent picks up via an **event trigger** (a watch/webhook that wakes it) or, as a backstop, its
**heartbeat**, then either acts within its own authority or escalates further. No agent ever widens its
own scope.

This keeps two invariants simultaneously true: (a) each layer is the authority over the one beneath
it, and (b) every mutation — including agent creation — flows through the declarative workflow
(CI applies; the controller reconciles), never a direct cluster write (per [04-workflow-model.md](04-workflow-model.md)).

### 6.1 Naming & discovery

Parent/child relationships are expressed with Kubernetes-native mechanics so the hierarchy is
discoverable without a side registry:

- **Parent link:** each `Agent` CR sets `parentRef` (the parent's name), and the controller stamps the
  `kube-agents/parent` label on its pod — so lineage (and cascading cleanup) is discoverable via selectors.
- **Labels:** the controller stamps `kube-agents/tier` (`platform` | `cluster-admin` | `developer-team`),
  `kube-agents/scope`, and `kube-agents/parent` (the parent's name) on each agent pod, enabling
  selector-based discovery.
- **Naming convention:** agents are named for their scope — e.g. `platform-<project>`,
  `cluster-admin-<cluster>`, `developer-team-<namespace>` — keeping names unique and legible.

---

## 7. Boundary matrix

A quick view of what each persona may act on. Enforcement mechanics live in
[03-security-model.md](03-security-model.md).

| Action                                |     Platform     |   Cluster Admin   |  Developer Team  |
| ------------------------------------- | :--------------: | :---------------: | :--------------: |
| Provision/upgrade clusters            | ✅ (declarative) |        ❌         |        ❌        |
| Manage node pools / cluster add-ons   |  ➡️ sets policy  |        ✅         |        ❌        |
| Create namespaces & tenancy isolation | ➡️ defines model |        ✅         |        ❌        |
| Provision the agent one layer down    | ✅ Cluster Admin | ✅ Developer Team |        ❌        |
| Operate workloads in a namespace      |        ❌        |        ❌         | ✅ (own ns only) |
| Cross another agent's scope           |        ❌        |        ❌         |        ❌        |
| Direct (non-declarative) mutation     |        ❌        |        ❌         |        ❌        |

Legend: ✅ acts (proposes via GitOps — agents never write the API directly, §2.2) · ➡️ sets the
policy the layer below applies · ❌ forbidden.

**On the workload hard line:** no higher-tier agent ever operates another scope's workloads —
strictly. There is no agent-level break-glass into a namespace, and **no sanctioned human break-glass**
either — every change goes through human-approved GitOps. Break-glass is deliberately out of the
design (see [01-vision-scope.md](01-vision-scope.md) §2). This keeps each layer's isolation provable rather than
conditional.

---

## 8. Runtime & packaging — an `Agent` CR per persona (reconciled by the kube-agents controller)

The three personas are **the same kind of thing**, deployed the same way: each is one instance of a
single, tier-discriminated **`Agent` CRD** (`kubeagents.x-k8s.io`) that selects the **Hermes** harness
with that persona's profile (`SOUL.md`, skills), and the **kube-agents controller** (the extended
`k8s-operator/`) reconciles it into an isolated pod — building the pod on the hardened, per-pod-identity
model verified in **[Scion](https://github.com/GoogleCloudPlatform/scion)**
([05](05-system-architecture.md) C1, [06](06-api-and-data-contracts.md) §1,
[08](08-agent-runtime-and-identity.md)). An `Agent` CR carries:

- `harness: hermes` + `profile` (the persona's `SOUL.md` + skills)
- `tier` (`platform | cluster-admin | developer-team`), `scope`, `parentRef`
- `serviceAccountName` + optional `runtimeClassName` — the pre-created read-only, tier-scoped KSA
  (Workload-Identity-bound) and the optional gVisor execution sandbox (deferred,
  [08](08-agent-runtime-and-identity.md) §5.1); placement derives from `tier` + `scope`

| `tier`           | Scope key fields                  | Identity scope           | Chat entrypoint / handle (§2.4)         |
| ---------------- | --------------------------------- | ------------------------ | --------------------------------------- |
| `platform`       | project                           | project-wide, read fleet | Platform teams — `@platform-<project>`  |
| `cluster-admin`  | project + cluster                 | single cluster           | Cluster admins — `@cluster-<cluster>`   |
| `developer-team` | project + cluster + **namespace** | single namespace         | Developer team — `@devteam-<namespace>` |

**Why one tier-discriminated CRD:** the personas differ only in `tier` + `scope` + `parentRef` +
default (read-only) permissions — otherwise identical, so a single `Agent` CRD expresses all three (one
CR per persona) and the **thin** controller handles pod lifecycle/isolation/identity/sandbox +
`(tier,scope)` cardinality (it references pre-created identity; it mints no RBAC). The three personas
stay three at the **behavior** layer (`SOUL.md`, skills, scope). Migration: today's `PlatformAgent`
CRD/operator is **generalized** into the `Agent` CRD + controller, and today's `PlatformAgent` becomes
the platform-tier instance ([07](07-implementation-roadmap.md)).

---

## 9. Goals & non-goals

### Goals

- Define three scope-bounded personas that map 1:1 onto project / cluster / namespace.
- Keep every persona the same _kind_ of agent (shared anatomy: `Agent` CR + Hermes harness).
- Make the cascade explicit: each layer provisions and governs the next, via declarative workflow.
- Keep SRE as a cross-cutting set of CUJs, not a persona.

### Non-goals

- Defining the exact RBAC/identity implementation — that is [03-security-model.md](03-security-model.md).
- Defining approval-gate and heartbeat mechanics in detail — that is
  [04-workflow-model.md](04-workflow-model.md).
- Enumerating exhaustive per-skill specs — the starting allocation is §2.1; skills may be
  re-scoped later.
- Multi-agent-framework specifics; personas are framework-portable by design.

## 10. Verification

A harness confirms this doc's design with:

- **Cardinality:** `kubectl get pods -l kube-agents/tier=platform` returns exactly **1 per project**;
  `-l kube-agents/tier=cluster-admin` exactly **1 per cluster**; `-l kube-agents/tier=developer-team`
  exactly **1 per namespace**. A second `Agent` CR for the same `(tier, scope)` is **rejected by the
  controller's cardinality webhook**.
- **Per-persona identity:** each agent pod's `spec.serviceAccountName` is its tier/scope read-only KSA
  (03 §3); labels `kube-agents/tier` and `kube-agents/parent` are set.
- **Indirect coordination:** a negative connectivity test shows no agent can open a network connection
  to another agent (NetworkPolicy denies); cross-tier requests appear only as GitOps commits / OKF
  entries, never direct calls.
- **Chat entrypoints & routing:** each persona exposes its own authenticated entrypoint (one per
  audience); the ChatOps gateway resolves a slash command or `@<tier>-<scope>` handle to the matching
  `(tier, scope)` agent **deterministically** (no inference), and enforces that agent's `AllowedUsers`
  before dispatch (§2.4). NL routing falls back to inference and, on low confidence, asks rather than
  guesses.
