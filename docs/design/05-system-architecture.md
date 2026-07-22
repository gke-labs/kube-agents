# Design 05: System Architecture

**Status:** ✅ Agreed — started 2026-07-21

**Charter:** [README.md](README.md) · **Depends on:** 01–04 · **Tier:** Buildable (bridging)

---

## TL;DR

This doc assembles the whole system a builder must stand up. kube-agents is a **hub-and-spoke**
deployment: a **hub cluster** runs Scion (the agent orchestrator), the Platform Agent, and shared services (inference,
GitHub token broker, observability); each **spoke (workload) cluster** runs a Cluster Admin Agent and
hosts Developer Team Agents in their namespaces. The **GitOps repository** and **OKF knowledge base**
are the shared state; agents are **read-only** and only the **customer's CI/CD pipeline** writes
(actuating merged KCC YAML or Terraform HCL — kube-agents is unopinionated about the pipeline and
integrates with existing infrastructure). Each persona is a per-persona **Scion agent template**
(running the Hermes harness); **Scion** launches it as an isolated pod with a per-pod read-only,
tier-scoped SA ([06](06-api-and-data-contracts.md) §1, [08](08-agent-runtime-and-identity.md)).
Everything runs in the `kubeagents-system` namespace convention with telemetry to `gke-managed-otel`.

---

## 1. Component inventory

| #   | Component                              | Responsibility                                                                                                                                                                                                                                                       | Tech / basis                                                           | Status                                              |
| --- | -------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------- | --------------------------------------------------- |
| C1  | **Scion (agent orchestrator/runtime)** | Launches each agent as an **isolated pod** running the Hermes harness; sets per-pod `serviceAccountName` (read-only KSA, Workload Identity), `namespace`, optional `runtimeClassName` sandbox, and a hardened pod-security context; pluggable harness; normalized OTel telemetry. **Supersedes a custom Kubebuilder operator/CRD** for agent lifecycle | [GoogleCloudPlatform/scion](https://github.com/GoogleCloudPlatform/scion) (Go) | New (adopt; k8s runtime early) |
| C2  | **Platform Agent**                     | Project/fleet custodian; chat entrypoint for platform teams                                                                                                                                                                                                          | Hermes harness (launched by Scion, `agents/platform/`)                | Exists                                              |
| C3  | **Cluster Admin Agent**                | Cluster custodian; chat entrypoint for cluster admins                                                                                                                                                                                                                | Hermes harness (launched by Scion; new persona)                       | New                                                 |
| C4  | **Developer Team Agent**               | Namespace self-service; chat entrypoint for dev teams                                                                                                                                                                                                                | Hermes harness (launched by Scion; new persona)                       | New                                                 |
| C5  | **Inference service**                  | Unified Completions API for all agents                                                                                                                                                                                                                               | LiteLLM (hosted models) / vLLM (local GPU)                             | Exists                                              |
| C6  | **GitHub Token Broker (Minty)**        | Brokers short-lived GitHub App tokens                                                                                                                                                                                                                                | GCP KMS + Workload Identity                                            | Exists                                              |
| C7  | **CI/CD actuation pipeline**           | Applies merged artifacts to cluster + cloud (deploy + reconcile) on PR merge; the **privileged writer**, acting only on reviewed state; **customer-provided, kube-agents is unopinionated**                                                                            | GitHub Actions / CircleCI / Jenkins / … (`kubectl apply`, `terraform apply`) | Customer-provided (integration)               |
| C8  | **IaC artifacts + tooling**            | The declarative change format the agent emits and the pipeline applies                                                                                                                                                                                               | **KCC YAML** or **Terraform HCL** (per customer requirements)         | New                                                 |
| C9  | **OKF knowledge base**                 | Durable curated knowledge (SOPs, blueprints, runbooks)                                                                                                                                                                                                               | OKF markdown in git                                                    | New                                                 |
| C10 | **mem0 + Qdrant** _(deferred post-v1)_ | Semantic/cognitive recall — **not in v1** ([02](02-agent-personas.md) §2.3)                                                                                                                                                                                          | mem0ai + Qdrant vector store                                           | Deferred                                            |
| C11 | **Session store**                      | Per-user runtime session state                                                                                                                                                                                                                                       | `session_db.sqlite` + `multiuser_memory`                               | Exists                                              |
| C12 | **Observability pipeline**             | Traces/metrics/logs + attribution                                                                                                                                                                                                                                    | OTel → `gke-managed-otel` → Cloud Trace/Logging/Managed Prometheus     | Exists                                              |
| C13 | **GitOps repository**                  | Shared source of truth for all mutation                                                                                                                                                                                                                              | Git (GitHub)                                                           | Exists (target repo)                                |
| C14 | **Authorization gateway** _(deferred — hardening)_ | Enforces **user-scoped authorization** (K8s `SubjectAccessReview` + GCP IAM) **outside the LLM loop** and down-scopes to the requester ([03](03-security-model.md) §4a). **Not in v1** — v1 secures the human→agent boundary with **trusted-human access + the read-only ceiling** ([08](08-agent-runtime-and-identity.md) §2, §5) | SubjectAccessReview + IAM (`testIamPermissions`/Policy Troubleshooter) | Deferred                                            |

## 2. Topology (hub-and-spoke)

```
                         ┌────────────────────────── HUB CLUSTER (kubeagents-system) ──────────────────────────┐
                         │  C1 Scion   C2 Platform Agent (read-only, Hermes)   C5 inference   C6 Minty        │
   Platform team ──chat──┤                C12 OTel collector                                                    │
                         │        │ proposes KCC YAML / Terraform (PR)              ▲ telemetry                 │
                         └────────┼─────────────────────────────────────────────────┼───────────────────────┘
                                  ▼                                                  │
                       ┌──────────────────┐     read-only agents propose via PR;     │
                       │  C13 GitOps repo │     humans review + merge                │
                       │  + C9 OKF base   │                                          │
                       └────────┬─────────┘                                          │
                                │ on merge → C7 CI/CD pipeline actuates              │
                                ▼    (kubectl apply / terraform apply → cluster+GCP) │
             ┌────────────────────┼───────────────────────────────────────┐        │
             ▼                    ▼                                         ▼        │
   ┌──── SPOKE CLUSTER A ────┐  ┌──── SPOKE CLUSTER B ────┐   ...                    │
   │ C3 Cluster Admin Agent  │  │ C3 Cluster Admin Agent  │   (external CI/CD ───────┘
   │  ns: team-a             │  │  ns: team-x             │    applies to spokes + GCP)
   │   C4 Dev Team Agent     │  │   C4 Dev Team Agent     │  (cluster admin ↔ chat, dev team ↔ chat)
   └─────────────────────────┘  └─────────────────────────┘
```

**Why hub-and-spoke.** It matches the containment hierarchy and failure-isolation goal
([04](04-workflow-model.md) §6): the hub owns fleet/project concerns and shared services once; each
spoke runs its own Cluster Admin + Developer Team agents. Actuation is handled by the customer's CI/CD
pipeline (external to the hub), which applies merged changes to each spoke and to GCP — so a hub
outage doesn't stop already-merged deploys, though spoke _agents_ pause without hub-hosted inference
(see [04](04-workflow-model.md) §6).

> **Alternative considered:** operator-per-cluster with no hub. Rejected as the default because it
> duplicates shared services (inference, Minty) per cluster and complicates fleet-wide
> governance. Small single-cluster installs may collapse hub+spoke into one cluster — see §7.

## 3. Deployment placement

| Component                               |        Hub cluster         |              Spoke cluster               | Namespace                    |
| --------------------------------------- | :------------------------: | :--------------------------------------: | ---------------------------- |
| Scion orchestrator (C1)                 |             ✅             | ✅ (launches that cluster's agent pods)  | `kubeagents-system`          |
| Platform Agent (C2)                     |             ✅             |                    —                     | `kubeagents-system`          |
| Cluster Admin Agent (C3)                |             —              |              ✅ (1/cluster)              | `kubeagents-system`          |
| Developer Team Agent (C4)               |             —              |             ✅ (1/namespace)             | the team's namespace         |
| Authorization gateway (C14) _(deferred)_ |     — (v1: in-agent)      |            — (v1: in-agent)              | `kubeagents-system` (if adopted) |
| Inference (C5), Minty (C6)              |        ✅ (shared)         |            consumed remotely             | `kubeagents-system`          |
| CI/CD actuation pipeline (C7)           |  external (customer CI/CD) |            external / applies to target  | n/a (customer-provided)      |
| OTel collector (C12)                    |             ✅             |                    ✅                    | `gke-managed-otel`           |

cert-manager (v1.13+) is only needed if the deferred attenuation **admission webhook** is adopted
([08](08-agent-runtime-and-identity.md) §5); v1 uses an in-tree `ValidatingAdmissionPolicy` and Scion,
neither of which requires it (`INSTALL.md`).

## 4. Primary data flows

**F1 — Mutation (propose → review → reconcile), the universal write path ([04](04-workflow-model.md) §1):**

1. Intent arrives (chat / heartbeat / escalation). Human-initiated intent comes only from **trusted,
   allowlisted humans** (authenticated chat); v1 does **not** check the requester's own permissions —
   the agent is bounded by its read-only, tier-scoped ceiling ([03](03-security-model.md) §4a,
   [08](08-agent-runtime-and-identity.md) §2). Per-request user-scoped authorization is deferred
   ([08](08-agent-runtime-and-identity.md) §5).
2. Agent (read-only, bounded by the requester) authors a declarative change — **KCC YAML or Terraform
   HCL** (workload manifest, cluster/cloud resource, or child `Agent` CR) — and opens a PR to the
   GitOps repo via Minty-brokered token (`submit-suggestion`).
3. Review gate: security-review suite + human approval per tier ([04](04-workflow-model.md) §2–3).
4. On merge, the **customer's CI/CD pipeline** applies the artifact to cluster + cloud
   (`kubectl apply` / `terraform apply`); **Scion** launches/updates the agent pods from their templates.
5. Outcome reported (human-readable) and audited (trace/session/requester).

**F2 — Read/observe:** agents read cluster/cloud state (read-only RBAC + read-only cloud SA) and
telemetry from the observability pipeline to reason and audit. In v1 reads are bounded by the agent's
own **read-only, tier-scoped** identity — not by the requester (access is limited to trusted humans,
[03](03-security-model.md) §4a). Down-scoping reads to the requester is deferred hardening
([08](08-agent-runtime-and-identity.md) §5).

**F3 — Coordination (indirect):** agents publish/observe shared state — GitOps repo (declarative),
OKF (curated knowledge) — each on its heartbeat. No direct agent-to-agent calls
([02](02-agent-personas.md) §2.3).

**F4 — Provisioning cascade:** Platform Agent → proposes a **cluster-admin** agent (a Scion agent
template **+ its read-only KSA/RBAC/Workload-Identity manifests**); Cluster Admin Agent → proposes
**developer-team** agents the same way. Each is a PR bundling the template + identity manifests
(rendered from tier+scope); **the CI/CD pipeline applies it after review**, and **Scion launches the
agent pod** bound to that read-only SA. Identity is pre-created by the pipeline, not minted by any
operator ([03](03-security-model.md) §4).

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
  **`knowledge/` root** (decided — outside the paths the pipeline deploys, so it is never applied to a
  cluster; a dedicated repo stays optional for later, `06` §5).
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

- **Scion runtime scope — Scion runs per cluster.** Each cluster's Scion launches **only its own
  cluster's** agent pods (hub → the platform-tier agent; each spoke → its cluster-admin +
  developer-team agents, from templates + identity manifests the pipeline applies under
  `clusters/<self>/agents/`). No cross-cluster credentials; a new spoke gets Scion at provisioning
  (bootstrap). This preserves failure isolation ([04](04-workflow-model.md) §6) and least privilege
  ([03](03-security-model.md)).
- **Single-cluster install — collapse topology, not personas.** One cluster plays hub + spoke: Scion +
  all three agent tiers + shared services (inference, Minty) run in it, shared services **once**, and a
  single deploy pipeline covers both `fleet/` and `clusters/<self>/`. All three personas still run; the
  persona model and isolation proof are identical to a multi-cluster install.
- **OKF location — `knowledge/` root in the GitOps repo.** Reuses the same PR/review flow + Minty
  token; it lives outside the paths the pipeline deploys (`clusters/<cluster>/`, `fleet/`), so it is
  never applied to a cluster. A dedicated knowledge repo stays optional if volume/governance later
  requires it ([06](06-api-and-data-contracts.md) §5).
- **Semantic recall (mem0/Qdrant) — deferred post-v1.** v1 coordinates on GitOps + OKF only
  ([02](02-agent-personas.md) §2.3), because OKF-in-git covers durable shared knowledge and the
  semantic-recall need is unproven. If later added: a single shared Qdrant in the hub with
  **server-side** scope isolation; recall best-effort.

## 8. Verification

- **Scion pod spec:** each agent pod Scion launches has `spec.serviceAccountName` = its read-only KSA,
  the correct `namespace`, `runtimeClassName` where required, and a hardened securityContext
  (`runAsNonRoot`, seccomp `RuntimeDefault`, `allowPrivilegeEscalation: false`).
- **Placement:** Platform in the hub (`kubeagents-system`); each Cluster Admin in its cluster; each
  Developer Team in its namespace.
- **Failure isolation (chaos):** kill the hub → spoke workloads keep running (agents pause); kill Scion
  in a cluster → running agent pods continue and no new launches occur; kill a Cluster Admin Agent →
  its Developer Team Agents keep running.
- **Unopinionated actuation:** actuation is the customer's CI/CD; nothing requires a bundled GitOps
  engine (no Config Sync/Connector) to be installed.
