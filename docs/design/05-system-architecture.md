# Design 05: System Architecture

**Status:** ✅ Agreed — started 2026-07-21

**Charter:** [README.md](README.md) · **Depends on:** 01–04 · **Tier:** Buildable (bridging)

---

## TL;DR

This doc assembles the whole system a builder must stand up. kube-agents is a **hub-and-spoke**
deployment: a **hub cluster** runs the operator, the Platform Agent, and shared services (inference,
GitHub token broker, observability); each **spoke (workload) cluster** runs a Cluster
Admin Agent plus the GitOps reconcilers (Config Sync + Config Connector) and hosts Developer Team
Agents in their namespaces. The **GitOps repository** and **OKF knowledge base** are the shared
state; agents are **read-only** and only the reconcilers write. All three personas are one
tier-discriminated **`Agent`** CRD ([06](06-api-and-data-contracts.md) §1). Everything runs in the
`kubeagents-system` namespace convention with telemetry to `gke-managed-otel`.

---

## 1. Component inventory

| #   | Component                              | Responsibility                                                                                                                                                                                                                                                       | Tech / basis                                                           | Status                                              |
| --- | -------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------- | --------------------------------------------------- |
| C1  | **kube-agents operator**               | Reconciles the single tier-discriminated **`Agent`** CRD → agent runtime objects (Deployment/Service/PVC); **low-privilege**, no RBAC-granting; hosts a **validating** webhook enforcing the child ⊆ parent attenuation ceiling (vetoes only — no `escalate`/`bind`) | Go / Kubebuilder (`k8s-operator/`)                                     | Exists (as `PlatformAgent`; generalizes to `Agent`) |
| C2  | **Platform Agent**                     | Project/fleet custodian; chat entrypoint for platform teams                                                                                                                                                                                                          | Hermes agent runtime (`agents/platform/`)                              | Exists                                              |
| C3  | **Cluster Admin Agent**                | Cluster custodian; chat entrypoint for cluster admins                                                                                                                                                                                                                | Hermes agent runtime (new blueprint)                                   | New                                                 |
| C4  | **Developer Team Agent**               | Namespace self-service; chat entrypoint for dev teams                                                                                                                                                                                                                | Hermes agent runtime (new blueprint)                                   | New                                                 |
| C5  | **Inference service**                  | Unified Completions API for all agents                                                                                                                                                                                                                               | LiteLLM (hosted models) / vLLM (local GPU)                             | Exists                                              |
| C6  | **GitHub Token Broker (Minty)**        | Brokers short-lived GitHub App tokens                                                                                                                                                                                                                                | GCP KMS + Workload Identity                                            | Exists                                              |
| C7  | **Config Sync**                        | Reconciles GitOps repo → cluster state                                                                                                                                                                                                                               | Config Sync (`RootSync`/`RepoSync`)                                    | New                                                 |
| C8  | **Config Connector (KCC)**             | Reconciles CRs → GCP resources (clusters, IAM)                                                                                                                                                                                                                       | `*.cnrm.cloud.google.com`                                              | Partially (RBAC/CRs exist; not via GitOps)          |
| C9  | **OKF knowledge base**                 | Durable curated knowledge (SOPs, blueprints, runbooks)                                                                                                                                                                                                               | OKF markdown in git                                                    | New                                                 |
| C10 | **mem0 + Qdrant** _(deferred post-v1)_ | Semantic/cognitive recall — **not in v1** ([02](02-agent-personas.md) §2.3)                                                                                                                                                                                          | mem0ai + Qdrant vector store                                           | Deferred                                            |
| C11 | **Session store**                      | Per-user runtime session state                                                                                                                                                                                                                                       | `session_db.sqlite` + `multiuser_memory`                               | Exists                                              |
| C12 | **Observability pipeline**             | Traces/metrics/logs + attribution                                                                                                                                                                                                                                    | OTel → `gke-managed-otel` → Cloud Trace/Logging/Managed Prometheus     | Exists                                              |
| C13 | **GitOps repository**                  | Shared source of truth for all mutation                                                                                                                                                                                                                              | Git (GitHub)                                                           | Exists (target repo)                                |
| C14 | **Authorization gateway**              | Fronts each agent's chat + data access; authenticates the requester and enforces **user-scoped authorization** (K8s `SubjectAccessReview` + GCP IAM) **outside the LLM loop**; down-scopes reads/proposals to the requester ([03](03-security-model.md) §4a)         | SubjectAccessReview + IAM (`testIamPermissions`/Policy Troubleshooter) | New                                                 |

## 2. Topology (hub-and-spoke)

```
                         ┌────────────────────────── HUB CLUSTER (kubeagents-system) ──────────────────────────┐
                         │  C1 operator   C2 Platform Agent (read-only)   C5 inference   C6 Minty              │
   Platform team ──chat──┤                C12 OTel collector                                                    │
                         │        │ proposes CRs/manifests (PR)                     ▲ telemetry                 │
                         └────────┼─────────────────────────────────────────────────┼───────────────────────┘
                                  ▼                                                  │
                       ┌──────────────────┐        reads/writes (read-only agents    │
                       │  C13 GitOps repo │◀────────propose via PR; humans approve)  │
                       │  + C9 OKF base   │                                          │
                       └──────────────────┘                                          │
                                  │ Config Sync pulls (C7)                           │
             ┌────────────────────┼───────────────────────────────────────┐        │
             ▼                    ▼                                         ▼        │
   ┌──── SPOKE CLUSTER A ────┐  ┌──── SPOKE CLUSTER B ────┐   ...                    │
   │ C3 Cluster Admin Agent  │  │ C3 Cluster Admin Agent  │   C7 Config Sync ───────┘
   │ C7 Config Sync          │  │ C8 Config Connector     │   C8 Config Connector
   │  ns: team-a             │  │  ns: team-x             │
   │   C4 Dev Team Agent ────┼──┤   C4 Dev Team Agent     │  (cluster admin ↔ chat, dev team ↔ chat)
   └─────────────────────────┘  └─────────────────────────┘
```

**Why hub-and-spoke.** It matches the containment hierarchy and failure-isolation goal
([04](04-workflow-model.md) §6): the hub owns fleet/project concerns and shared services once; each
spoke's Config Sync keeps the cluster's reconciled state running even if the hub is down (though spoke
_agents_ pause without hub-hosted inference — see [04](04-workflow-model.md) §6). Config Sync in each
spoke pulls the same repo, so desired state propagates without the hub imperatively pushing.

> **Alternative considered:** operator-per-cluster with no hub. Rejected as the default because it
> duplicates shared services (inference, Minty) per cluster and complicates fleet-wide
> governance. Small single-cluster installs may collapse hub+spoke into one cluster — see §7.

## 3. Deployment placement

| Component                               |        Hub cluster         |              Spoke cluster               | Namespace                    |
| --------------------------------------- | :------------------------: | :--------------------------------------: | ---------------------------- |
| Operator (C1)                           |             ✅             | ✅ (reconciles that cluster's agent CRs) | `kubeagents-system`          |
| Platform Agent (C2)                     |             ✅             |                    —                     | `kubeagents-system`          |
| Cluster Admin Agent (C3)                |             —              |              ✅ (1/cluster)              | `kubeagents-system`          |
| Developer Team Agent (C4)               |             —              |             ✅ (1/namespace)             | the team's namespace         |
| Authorization gateway (C14)             | ✅ (fronts Platform Agent) |   ✅ (fronts Cluster Admin + Dev Team)   | `kubeagents-system`          |
| Inference (C5), Minty (C6)              |        ✅ (shared)         |            consumed remotely             | `kubeagents-system`          |
| Config Sync (C7), Config Connector (C8) |             ✅             |                    ✅                    | per their install convention |
| OTel collector (C12)                    |             ✅             |                    ✅                    | `gke-managed-otel`           |

cert-manager (v1.13+) is a prerequisite in every cluster for operator webhook TLS
(`INSTALL.md`).

## 4. Primary data flows

**F1 — Mutation (propose → review → reconcile), the universal write path ([04](04-workflow-model.md) §1):**

1. Intent arrives (chat / heartbeat / escalation). For **human-initiated** intent, the
   **authorization gateway (C14)** authenticates the requester and checks their own GCP + K8s
   permissions (`SubjectAccessReview` + IAM); unauthorized requests are denied before the agent acts,
   and the agent's authority is down-scoped to the requester ([03](03-security-model.md) §4a).
2. Agent (read-only, bounded by the requester) authors a declarative change — manifest, KCC
   `containercluster` CR, or child agent CR — and opens a PR to the GitOps repo via Minty-brokered
   token (`submit-suggestion`).
3. Review gate: security-review suite + human approval per tier ([04](04-workflow-model.md) §2–3).
4. On merge, **Config Sync** applies repo → cluster; **Config Connector** reconciles cloud CRs →
   GCP; the **operator** reconciles agent CRs → Deployments + identity.
5. Outcome reported (human-readable) and audited (trace/session/requester).

**F2 — Read/observe:** agents read cluster/cloud state (read-only RBAC + read-only cloud SA) and
telemetry from the observability pipeline to reason and audit. For human-initiated reads, the
authorization gateway (C14) **down-scopes results to the requester's own permissions** — the agent
returns only what the user could see themselves ([03](03-security-model.md) §4a).

**F3 — Coordination (indirect):** agents publish/observe shared state — GitOps repo (declarative),
OKF (curated knowledge) — each on its heartbeat. No direct agent-to-agent calls
([02](02-agent-personas.md) §2.3).

**F4 — Provisioning cascade:** Platform Agent → proposes an `Agent{tier: cluster-admin}` CR; Cluster
Admin Agent → proposes `Agent{tier: developer-team}` CRs. Each is a PR bundling the child CR **+ its
read-only RBAC** (rendered from tier+scope); **Config Sync applies it after review**, and the operator
reconciles the CR into runtime objects. Read-only identity is applied via Config Sync, not minted by
the operator ([03](03-security-model.md) §4).

## 5. Shared services detail

- **Inference (C5):** LiteLLM proxy for hosted models (Gemini/OpenAI), vLLM for local GPU models;
  exposes a unified Completions API; **per-tier/per-tenant virtual keys** provide budget, rate-limit,
  and log isolation on the shared proxy; Prometheus metrics + OTel traces exported.
- **Minty (C6):** the _only_ credential path for repo writes; issues short-lived GitHub App tokens
  via KMS + Workload Identity. No static git creds anywhere.
- **mem0/Qdrant (C10) — deferred post-v1:** semantic recall is **not in v1** ([02](02-agent-personas.md)
  §2.3). If introduced later, default to a single shared Qdrant in the hub with **server-side** scope
  isolation (per-scope collections / access-controlled keys) and treat recall as best-effort.
- **OKF base (C9):** curated knowledge as markdown-in-git; lives in the GitOps repo under the
  **`knowledge/` root** (decided — outside Config Sync's synced paths; a dedicated repo stays
  optional for later, `06` §5).
- **Observability (C12):** OTel → `gke-managed-otel` → Cloud Trace/Logging + Managed Prometheus;
  carries requester/trace/session for attribution (`docs/designs/audit-logging-user-attribution.md`).

## 6. Non-functional requirements (targets — defaults, tune later)

| Dimension          | Default target                                                                                                                                                         | Rationale                                                          |
| ------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------ |
| Fleet scale        | ≥ 50 spoke clusters per hub                                                                                                                                            | Fleet-governance use case                                          |
| Agents per cluster | 1 Cluster Admin + ≤ 200 Dev Team (namespaces)                                                                                                                          | Namespace density on GKE                                           |
| Chat turn latency  | p95 < 10 s for read/plan; async for mutations                                                                                                                          | Mutations are PR-gated, not synchronous                            |
| Availability       | Cluster keeps running last-synced state if hub down; spoke **agents pause** (hub-hosted inference/Minty — [04](04-workflow-model.md) §6); agents stateless-restartable | No cascade of _reconciled state_; agent reasoning is hub-dependent |
| Recovery           | Agent pod restart < a few s (PVC-backed state, atomic writes)                                                                                                          | `multiuser_memory` eviction safety                                 |
| Cost               | Shared inference in hub; Spot-eligible agent pods                                                                                                                      | Avoid per-cluster duplication                                      |

These are **defaults for a builder**, not commitments; revisit under load testing.

## 7. Deployment-model decisions

- **Operator scope — one operator per cluster.** Each reconciles **only its own cluster's** agent CRs
  (hub → the platform-tier `Agent`; each spoke → its cluster-admin + developer-team `Agent` CRs,
  delivered by Config Sync under `clusters/<self>/agents/`). No cross-cluster credentials; a new spoke
  gets its operator at provisioning (bootstrap). This preserves failure isolation
  ([04](04-workflow-model.md) §6) and least privilege ([03](03-security-model.md)).
- **Single-cluster install — collapse topology, not personas.** One cluster plays hub + spoke:
  operator + all three agent tiers + shared services (inference, Minty) run in it, shared services
  **once**, a single Config Sync `RootSync` covers both `fleet/` and `clusters/<self>/`. All three
  personas still run; the persona model and isolation proof are identical to a multi-cluster install.
- **OKF location — `knowledge/` root in the GitOps repo.** Reuses the same PR/review flow + Minty
  token; it lives outside Config Sync's synced paths (`clusters/<cluster>/`, `fleet/`), so it is never
  applied to a cluster. A dedicated knowledge repo stays optional if volume/governance later requires
  it ([06](06-api-and-data-contracts.md) §5).
- **Semantic recall (mem0/Qdrant) — deferred post-v1.** v1 coordinates on GitOps + OKF only
  ([02](02-agent-personas.md) §2.3), because OKF-in-git covers durable shared knowledge and the
  semantic-recall need is unproven. If later added: a single shared Qdrant in the hub with
  **server-side** scope isolation; recall best-effort.
