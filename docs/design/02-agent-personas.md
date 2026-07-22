# Design 02: Agent Personas

**Status:** ✅ Agreed — started 2026-07-21

**Charter:** [README.md](README.md) · **Depends on:** [01-vision-scope.md](01-vision-scope.md)

---

## TL;DR

`kube-agents` defines **three agent personas**, one per level of the Kubernetes containment
hierarchy: the **Platform Agent** (1 per project), the **Cluster Admin Agent** (1 per cluster), and
the **Developer Team Agent** (1 per namespace). Each persona shares a common anatomy — a `SOUL.md`
identity, a config, a scoped skill set, memory, a heartbeat, and an operator-managed deployment —
but differs in **scope, authority, skills, and permissions**.

They form a **cascading hierarchy**: each layer holds authority over the layer beneath it and
provisions it — but always through the declarative workflow and operator, never by direct mutation.
This is the end-state roster; the Platform Agent exists today, the other two are coming soon.

---

## 1. The roster

| Persona | Scope | Cardinality | Owns / governs | Bounded by |
|---------|-------|-------------|----------------|------------|
| **Platform Agent** | GCP/cloud **project** | 1 per project | The fleet: clusters, cross-cluster policy, global RBAC, Cluster Admin Agents | Human platform team + project-level approval gates |
| **Cluster Admin Agent** | A single **cluster** | 1 per cluster | Cluster internals: node pools, add-ons, namespaces, Developer Team Agents | Platform Agent policy + project guardrails |
| **Developer Team Agent** | A single **namespace** | 1 per namespace | Workloads within its namespace | Cluster Admin policy + cluster/project guardrails |

Every persona serves SRE critical user journeys within its own scope (see
[01-vision-scope.md](01-vision-scope.md) §3); SRE is not a separate persona.

---

## 2. Shared anatomy of an agent

All three personas are the same _kind_ of thing — a scoped, persona-driven agent — assembled from
the same parts. This uniformity is what makes the roster extensible.

| Part | What it is | Current reference |
|------|-----------|-------------------|
| **Identity (`SOUL.md`)** | The persona's core instructions, truths, and behavioral guardrails | `agents/platform/SOUL.md` |
| **Config** | MCP servers, toolsets, memory, plugins available to the agent | `agents/platform/config.yaml` |
| **Skills** | Scoped, loadable capabilities (each a `SKILL.md` + assets/scripts) | `agents/platform/skills/` |
| **Governance SOPs** | Standard operating procedures the agent follows for recurring duties | `agents/platform/governance/` |
| **Memory** | Durable, multi-user memory (pluggable provider) | `plugins/memory/multiuser_memory/` |
| **Heartbeat** | A scheduled tick driving proactive audits & drift detection | `INSTALL.md` §3, `cron/jobs.json` |
| **Deployment** | An operator-reconciled CRD → Pod/Deployment with scoped identity | `k8s-operator/` (`PlatformAgent` today) |
| **Integrations** | Chat entrypoint (Google Chat/Slack), GitHub for declarative PRs | `PlatformAgentIntegrationSpec` |

**Design principle:** a new persona is defined by _changing the fills, not the frame_ — a different
`SOUL.md`, a scoped skill set, and scope-appropriate permissions, deployed as the shared **`Agent`**
CRD with a different `tier`/`scope`, reusing the `AgentSpec` / `HarnessSpec` / `IntegrationSpec`
building blocks (§8).

Every persona also exposes its **own human chat entrypoint**, one per audience: platform teams talk
to the Platform Agent, cluster admins to the Cluster Admin Agent, and developer teams to their
Developer Team Agent. Each persona is a genuine front door for its layer, not a silent internal
tier.

### 2.1 Skill allocation

Skills are scoped to the persona whose authority they match. The starting allocation of today's
skill set:

| Skill(s) | Platform | Cluster Admin | Developer Team |
|----------|:--------:|:-------------:|:--------------:|
| `gke-cluster-creator`, `gke-cluster-lifecycle` | ✅ | | |
| `gke-cost-analysis` | ✅ | | |
| `github-issue-resolver` | ✅ | | |
| `kube-agents-observability` (harness self-obs) | ✅ | | |
| `gke-multi-tenancy` | ✅ defines model | ✅ applies | |
| `gke-compute-classes`, `gke-networking-edge`, `gke-storage`, `gke-backup-dr`, `gke-reliability` | | ✅ | |
| `gke-app-onboarding`, `gke-manifest-generation`, `gke-productionize`, `gke-inference-quickstart` | | | ✅ |
| `gke-workload-scaling`, `gke-workload-security`, `gke-workload-troubleshooting` | | | ✅ |
| `gke-observability` | ✅ fleet view | ✅ cluster view | ✅ workload view |
| `submit-suggestion` (declarative change submission) | ✅ | ✅ | ✅ |

`submit-suggestion` and `gke-observability` are cross-cutting — every tier submits declarative
changes and observes, each scoped to its own authority. This allocation is a starting point; skills
may be re-scoped as the personas mature.

### 2.2 Agents are read-only; reconcilers mutate

Every persona is **read-only on all Kubernetes and cloud APIs.** No agent ever writes to a cluster
or cloud API directly. The only write an agent performs is committing a proposed declarative change
to the **GitOps repository** (a PR, via a brokered short-lived token). The actual application of
that change is done by **reconcilers** — Config Sync (repo → cluster), Config Connector (cloud
resources), and the kube-agents operator (the `Agent` CRD) — which hold the scoped write permissions,
not the agents. See [04-workflow-model.md](04-workflow-model.md) §1.1 for the reference stack and
[03-security-model.md](03-security-model.md) §3 for enforcement.

This is a deliberate safety property: because agents cannot mutate directly, a subverted agent's
worst case is a _proposed_ change that still faces the review gate — never a live cluster write.

**Each agent has its own identity, and acts as the requesting user.** Every agent runs under its own
**Kubernetes ServiceAccount** — plus a GCP service account via **Workload Identity where it needs cloud
access** (K8s-only agents need no cloud SA). That identity is the agent's _ceiling_. On top of it,
every request is executed under the **authority of the human who made it**: the requester's own GCP +
Kubernetes permissions are checked and the agent's effective authority is **down-scoped to them**, so
an agent is never a way to exceed one's own access (no confused deputy). See
[03-security-model.md](03-security-model.md) §3–§4a.

> **Delta from current state:** agents today hold direct-mutation tools (the `create_cluster` MCP
> tool and a `gke` MCP server bound to `container.googleapis.com`). The end state removes direct
> mutation from agents entirely; see [01-vision-scope.md](01-vision-scope.md) §6.

### 2.3 Coordination is indirect (shared state, not direct calls)

Agents **never call each other directly** — there is no agent-to-agent RPC or API. They coordinate
through **shared state** that each observes on its own heartbeat. Two kinds of state serve two
distinct purposes, each with the tool suited to it:

| State layer | Purpose | Mechanism |
|-------------|---------|-----------|
| **Declarative / infra** | Desired infrastructure state; the shared source of truth | **GitOps repository** — agents propose (read-only, via PR), reconcilers apply |
| **Curated knowledge** | Durable, shareable know-how: SOPs, cluster blueprints, runbooks, metric/tenancy definitions, cross-agent notes | **OKF** (Open Knowledge Format) — markdown + YAML frontmatter in git; agents read/update, humans curate as code |

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

- **Read-only** on all cluster and cloud APIs (fleet-wide visibility for auditing). It proposes
  changes — including child-agent CRs — to the GitOps repo; it holds no direct cluster/cloud write
  (see §2.2).
- All infrastructure mutation is declarative (GitOps + reconcilers), never direct `kubectl` (per
  `SOUL.md §1`, §4).
- **Must not** reach _inside_ a namespace to operate workloads — that is the Developer Team Agent's
  scope. The Platform Agent sets the guardrails; it does not do the tenant's work.

---

## 4. Persona: Cluster Admin Agent (cluster scope)

**Cardinality:** 1 per cluster. **Coming soon** (new CRD, §8).

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

- Authority is confined to its one cluster; it cannot act on other clusters or at the project
  level.
- Cannot override project-level policy from the Platform Agent — it operates _within_ those
  guardrails and escalates upward when a change requires project authority.
- **Must not** operate workloads inside a namespace — that is the Developer Team Agent's scope. It
  provisions and bounds namespaces; it does not do the tenant's workload work.
- Like all personas, mutates only through the declarative workflow, not directly.

---

## 5. Persona: Developer Team Agent (namespace scope)

**Cardinality:** 1 per namespace. **Coming soon** (new CRD, §8).

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

- **Hard boundary at the namespace edge.** It is provably unable to read or affect other namespaces
  or escalate to cluster/project scope. This isolation is the load-bearing security property of the
  whole model (enforced per [03-security-model.md](03-security-model.md)).
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
spawn a child. Instead it **authors a declarative request** — a child agent custom resource
(§8) submitted through the active GitOps workflow (e.g. via `submit-suggestion`) — which the
**operator reconciles** into a running, scoped agent. So:

- The Platform Agent _proposes_ an `Agent{tier: cluster-admin}` CR (subject to human/project approval
  gates); the operator provisions it with cluster-scoped identity.
- Each Cluster Admin Agent _proposes_ `Agent{tier: developer-team}` CRs for the namespaces in its
  cluster; the operator provisions them with namespace-scoped identity.

**Escalation flows the other way.** A lower agent that needs a change outside its scope escalates a
request _upward_ to its parent — **indirectly, via shared state** (§2.3), not a direct call — which
the parent observes on its heartbeat and either acts on within its own authority or escalates
further. No agent ever widens its own scope.

This keeps two invariants simultaneously true: (a) each layer is the authority over the one beneath
it, and (b) every mutation — including agent creation — flows through the declarative workflow and
operator, never a direct cluster write (per [04-workflow-model.md](04-workflow-model.md)).

### 6.1 Naming & discovery

Parent/child relationships are expressed with Kubernetes-native mechanics so the hierarchy is
discoverable without a side registry:

- **Owner references:** a child agent CR carries an `ownerReference` to its parent agent CR, so the
  lineage (and cascading cleanup) is intrinsic to the API objects.
- **Labels:** each agent carries `kube-agents/tier` (`platform` | `cluster-admin` | `developer-team`)
  and `kube-agents/parent` (the parent's name), enabling selector-based discovery.
- **Naming convention:** agents are named for their scope — e.g. `platform-<project>`,
  `cluster-admin-<cluster>`, `developer-team-<namespace>` — keeping names unique and legible.

---

## 7. Boundary matrix

A quick view of what each persona may act on. Enforcement mechanics live in
[03-security-model.md](03-security-model.md).

| Action | Platform | Cluster Admin | Developer Team |
|--------|:--------:|:-------------:|:--------------:|
| Provision/upgrade clusters | ✅ (declarative) | ❌ | ❌ |
| Manage node pools / cluster add-ons | ➡️ sets policy | ✅ | ❌ |
| Create namespaces & tenancy isolation | ➡️ defines model | ✅ | ❌ |
| Provision the agent one layer down | ✅ Cluster Admin | ✅ Developer Team | ❌ |
| Operate workloads in a namespace | ❌ | ❌ | ✅ (own ns only) |
| Cross another agent's scope | ❌ | ❌ | ❌ |
| Direct (non-declarative) mutation | ❌ | ❌ | ❌ |

Legend: ✅ acts (proposes via GitOps — agents never write the API directly, §2.2) · ➡️ sets the
policy the layer below applies · ❌ forbidden.

**On the workload hard line:** no higher-tier agent ever operates another scope's workloads —
strictly. There is no agent-level break-glass into a namespace, and **no sanctioned human break-glass**
either — every change goes through human-approved GitOps. Break-glass is deliberately out of the
design (see [01-vision-scope.md](01-vision-scope.md) §8). This keeps each layer's isolation provable rather than
conditional.

---

## 8. CRD model (end state) — a single `Agent` kind

The three personas are **one Kubernetes kind, not three.** The operator today defines `PlatformAgent`
(`k8s-operator/api/v1alpha1/platformagent_types.go`); the end state generalizes it into a single
**`Agent`** CRD discriminated by a `tier` field, composed from the existing shared building blocks:

- `AgentSpec` — `Deployment` + `Security` (RBAC, Pod Security, Workload Identity)
- `HarnessSpec` — `ClusterName`, `Location`, `ProjectID`, `Hermes`, `Memory`
- `IntegrationSpec` — `GitHub` (GitOps repo), plus chat integrations for the entrypoint agent
- **`Tier`** (`platform | cluster-admin | developer-team`), **`Scope`**, and **`ParentRef`** (see
  [06](06-api-and-data-contracts.md) §1)

| `spec.tier` | Scope key fields | Identity scope | Chat entrypoint |
|-------------|------------------|----------------|-----------------|
| `platform` | project | project-wide, read fleet | Yes — platform teams |
| `cluster-admin` | project + cluster | single cluster | Yes — cluster admins |
| `developer-team` | project + cluster + **namespace** | single namespace | Yes — developer team |

**Why one kind, not three:** the personas differ only in `tier` + `scope` + `parentRef` + default
(read-only) permissions — otherwise identical. A single CRD means **one reconciler, one validating
webhook, and one schema to version**, instead of three ~90%-identical copies. `tier`/`scope`/`parent`
consistency and cardinality are enforced by validation ([06](06-api-and-data-contracts.md) §1.2, §10).
The three personas stay three at the **behavior** layer (`SOUL.md`, skills, scope) — only the
Kubernetes _kind_ is unified. Migration: `PlatformAgent` → `Agent{tier: platform}`
([07](07-implementation-roadmap.md)).

---

## 9. Goals & non-goals

### Goals

- Define three scope-bounded personas that map 1:1 onto project / cluster / namespace.
- Keep every persona the same _kind_ of agent (shared anatomy, shared CRD building blocks).
- Make the cascade explicit: each layer provisions and governs the next, via declarative workflow.
- Keep SRE as a cross-cutting set of CUJs, not a persona.

### Non-goals

- Defining the exact RBAC/identity implementation — that is [03-security-model.md](03-security-model.md).
- Defining approval-gate and heartbeat mechanics in detail — that is
  [04-workflow-model.md](04-workflow-model.md).
- Enumerating exhaustive per-skill specs — the starting allocation is §2.1; skills may be
  re-scoped later.
- Multi-agent-framework specifics; personas are framework-portable by design.

## 10. Resolved decisions & deferrals

### Resolved

- **Skill allocation** — settled in §2.1 (starting allocation; skills may be re-scoped as personas
  mature).
- **Chat entrypoints** — all three personas are team-facing, one chat front door per audience
  (§2, §8).
- **Workload hard line** — strict: higher tiers never operate a namespace's workloads; there is **no
  break-glass** (agent or human) — every change goes through human-approved GitOps (§7).
- **Naming & discovery** — owner references + `kube-agents/tier` / `kube-agents/parent` labels +
  scope-based naming (§6.1).

### Deferred to [04-workflow-model.md](04-workflow-model.md)

- **Approval authority per tier** — which cascade actions require human approval vs. run
  autonomously.
- **Failure isolation** — fallback behavior when a tier is unavailable (e.g. a down Cluster Admin
  Agent and its Developer Team Agents / the Platform Agent's view of that cluster).
