# Design 05: System Architecture

**Status:** ✅ Agreed

**Overview:** [README.md](README.md) · **Depends on:** 01–04 · **Tier:** Buildable (bridging)

---

## TL;DR

This doc assembles the whole system a builder must stand up. kube-agents is a **hub-and-spoke**
deployment: a **hub cluster** runs the kube-agents controller, the Platform Agent, and shared services
(inference, GitHub token broker, observability); each **spoke (workload) cluster** runs a Cluster Admin
Agent and hosts Developer Team Agents in their namespaces. The **GitOps repository** and **OKF knowledge
base** are the shared state; agents are **read-only** and only the **customer's CI/CD pipeline** writes
(actuating merged KCC YAML or Terraform HCL — kube-agents is unopinionated about the pipeline and
integrates with existing infrastructure). Each persona is an **`Agent` custom resource** (running the
Hermes harness); the **kube-agents controller** reconciles it into an isolated pod with a per-pod
read-only, tier-scoped SA — building the pod on the hardened, per-pod-identity model verified in
**Scion** ([06](06-api-and-data-contracts.md) §1, [08](08-agent-runtime-and-identity.md)). Everything
runs in the `kubeagents-system` namespace convention with telemetry to `gke-managed-otel`.

---

## 1. Component inventory

| #   | Component                                          | Responsibility                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            | Tech / basis                                                                                                                         | Status                          |
| --- | -------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------- |
| C1  | **kube-agents controller (agent runtime)**         | Reconciles each `Agent` CR into an **isolated pod** (single-replica Deployment) running the Hermes harness; sets per-pod `serviceAccountName` (read-only KSA, Workload Identity), `namespace`, optional `runtimeClassName` sandbox, and a hardened pod-security context; owns lifecycle/relaunch, `(tier,scope)` cardinality (validating webhook), and label-stamping; normalized OTel telemetry. **Generalizes today's `PlatformAgent` operator**; builds the pod on **Scion**'s verified per-pod model (Phase-1: call Scion's launch primitive directly)                                | `k8s-operator/` (Go, Kubebuilder), extended; Scion model ([GoogleCloudPlatform/scion](https://github.com/GoogleCloudPlatform/scion)) | Exists (extend)                 |
| C2  | **Platform Agent**                                 | Project/fleet custodian; chat entrypoint for platform teams                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               | Hermes harness (reconciled by the controller, `agents/platform/`)                                                                    | Exists                          |
| C3  | **Cluster Admin Agent**                            | Cluster custodian; chat entrypoint for cluster admins                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     | Hermes harness (reconciled by the controller; new persona)                                                                           | New                             |
| C4  | **Developer Team Agent**                           | Namespace self-service; chat entrypoint for dev teams                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     | Hermes harness (reconciled by the controller; new persona)                                                                           | New                             |
| C5  | **Inference service**                              | Unified Completions API for all agents                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    | LiteLLM (hosted models) / vLLM (local GPU)                                                                                           | Exists                          |
| C6  | **GitHub Token Broker (Minty)**                    | Brokers short-lived GitHub App tokens                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     | GCP KMS + Workload Identity                                                                                                          | Exists                          |
| C7  | **CI/CD actuation pipeline**                       | Applies merged artifacts to cluster + cloud (deploy + reconcile) on PR merge; the **privileged writer**, acting only on reviewed state; **customer-provided, kube-agents is unopinionated**                                                                                                                                                                                                                                                                                                                                                                                               | GitHub Actions / CircleCI / Jenkins / … (`kubectl apply`, `terraform apply`)                                                         | Customer-provided (integration) |
| C8  | **IaC artifacts + tooling**                        | The declarative change format the agent emits and the pipeline applies                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    | **KCC YAML** or **Terraform HCL** (per customer requirements)                                                                        | New                             |
| C9  | **OKF knowledge base**                             | Durable curated knowledge (SOPs, blueprints, runbooks)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    | OKF markdown in git                                                                                                                  | New                             |
| C10 | **mem0 + Qdrant** _(deferred post-v1)_             | Semantic/cognitive recall — **not in v1** ([02](02-agent-personas.md) §2.3)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               | mem0ai + Qdrant vector store                                                                                                         | Deferred                        |
| C11 | **Session store**                                  | Per-user runtime session state                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            | `session_db.sqlite` + `multiuser_memory`                                                                                             | Exists                          |
| C12 | **Observability pipeline**                         | Traces/metrics/logs + attribution                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         | OTel → `gke-managed-otel` → Cloud Trace/Logging/Managed Prometheus                                                                   | Exists                          |
| C13 | **GitOps repository**                              | Shared source of truth for all mutation                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   | Git (GitHub)                                                                                                                         | Exists (target repo)            |
| C14 | **Authorization gateway** _(deferred — hardening)_ | Enforces **user-scoped authorization** (K8s `SubjectAccessReview` + GCP IAM) **outside the LLM loop** and down-scopes to the requester ([03](03-security-model.md) §4a). **Not in v1** — v1 secures the human→agent boundary with **trusted-human access + the read-only ceiling** ([08](08-agent-runtime-and-identity.md) §2, §5)                                                                                                                                                                                                                                                        | SubjectAccessReview + IAM (`testIamPermissions`/Policy Troubleshooter)                                                               | Deferred                        |
| C15 | **ChatOps gateway & router**                       | Single chat ingress (Google Chat + Slack): normalizes both platforms, enforces the **target agent's `AllowedUsers`**, and **routes each message to the addressed agent** — deterministically by slash command / `@<tier>-<scope>` handle, or by NL inference as fallback ([02](02-agent-personas.md) §2.4, [06](06-api-and-data-contracts.md) §2b). Routes to the **separate per-tier pods** (not a co-located multiplexer, [08](08-agent-runtime-and-identity.md) §3; not agent-to-agent, F3). Runs in the Hermes runtime (`hermes gateway`); today fans in to the single Platform Agent | Hermes gateway (bundled), extended via relay patches / hooks / MCP                                                                   | Exists (extend)                 |

## 2. Topology (hub-and-spoke)

```
                         ┌────────────────────────── HUB CLUSTER (kubeagents-system) ──────────────────────────┐
                         │  C1 controller   C2 Platform Agent (read-only, Hermes)   C5 inference   C6 Minty  │
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

| Component                                |                      Hub cluster                       |              Spoke cluster               | Namespace                        |
| ---------------------------------------- | :----------------------------------------------------: | :--------------------------------------: | -------------------------------- |
| kube-agents controller (C1)              |                           ✅                           | ✅ (reconciles that cluster's Agent CRs) | `kubeagents-system`              |
| Platform Agent (C2)                      |                           ✅                           |                    —                     | `kubeagents-system`              |
| Cluster Admin Agent (C3)                 |                           —                            |              ✅ (1/cluster)              | `kubeagents-system`              |
| Developer Team Agent (C4)                |                           —                            |             ✅ (1/namespace)             | the team's namespace             |
| Authorization gateway (C14) _(deferred)_ | — (v1: n/a — trusted-human access + read-only ceiling) |               — (v1: n/a)                | `kubeagents-system` (if adopted) |
| Inference (C5), Minty (C6)               |                      ✅ (shared)                       |            consumed remotely             | `kubeagents-system`              |
| CI/CD actuation pipeline (C7)            |               external (customer CI/CD)                |       external / applies to target       | n/a (customer-provided)          |
| OTel collector (C12)                     |                           ✅                           |                    ✅                    | `gke-managed-otel`               |

cert-manager (v1.13+) provides TLS for the kube-agents controller's **admission webhook** (the
`(tier,scope)` cardinality check, [06](06-api-and-data-contracts.md) §1.2), so it is a **v1
prerequisite** (`INSTALL.md`) — as it already is for today's operator. The in-tree
`ValidatingAdmissionPolicy` (RBAC write-verb / wrong-scope denial) needs no cert-manager but requires
**Kubernetes ≥1.30** (GA) — including the Phase-0 test cluster ([07](07-implementation-roadmap.md) §2);
the deferred cross-object attenuation webhook ([08](08-agent-runtime-and-identity.md) §5) reuses the same
webhook server.

## 4. Primary data flows

**F1 — Mutation (propose → review → reconcile), the universal write path ([04](04-workflow-model.md) §1):**

1. Intent arrives (chat / event trigger / cron / heartbeat backstop / escalation; push preferred over
   polling, [04](04-workflow-model.md) §4). Human-initiated intent comes only from **trusted,
   allowlisted humans** (authenticated chat); v1 does **not** check the requester's own permissions —
   the agent is bounded by its read-only, tier-scoped ceiling ([03](03-security-model.md) §4a,
   [08](08-agent-runtime-and-identity.md) §2). Per-request user-scoped authorization is deferred
   ([08](08-agent-runtime-and-identity.md) §5).
2. Agent (read-only, bounded by its tier-scoped ceiling) authors a declarative change — **KCC YAML or
   Terraform HCL** (workload manifest, cluster/cloud resource, or child `Agent` CR) — and opens a PR to
   the GitOps repo via Minty-brokered token (`submit-suggestion`).
3. Review gate: security-review suite + human approval per tier ([04](04-workflow-model.md) §2–3).
4. On merge, the **customer's CI/CD pipeline** applies the artifact to cluster + cloud
   (`kubectl apply` / `terraform apply`); the **kube-agents controller** reconciles/updates the agent
   pods from their `Agent` CRs.
5. Outcome reported (human-readable) and audited (trace/session/requester).

**F2 — Read/observe:** agents read cluster/cloud state (read-only RBAC + read-only cloud SA) and
telemetry from the observability pipeline to reason and audit. In v1 reads are bounded by the agent's
own **read-only, tier-scoped** identity — not by the requester (access is limited to trusted humans,
[03](03-security-model.md) §4a). Down-scoping reads to the requester is deferred hardening
([08](08-agent-runtime-and-identity.md) §5).

**F3 — Coordination (indirect):** agents publish/observe shared state — GitOps repo (declarative),
OKF (curated knowledge) — reacting via **event triggers where available** (Kubernetes watches, alert /
GitHub webhooks) with the **heartbeat as backstop** ([04](04-workflow-model.md) §4). No direct
agent-to-agent calls ([02](02-agent-personas.md) §2.3).

**F4 — Provisioning cascade:** Platform Agent → proposes a **cluster-admin** agent (an `Agent` CR **+ its
read-only KSA/RBAC/Workload-Identity manifests**); Cluster Admin Agent → proposes **developer-team**
agents the same way. Each is a PR bundling the CR + identity manifests (rendered from tier+scope); **the
CI/CD pipeline applies it after review**, and the **kube-agents controller reconciles the agent pod**
bound to that read-only SA. Identity is pre-created by the pipeline; the controller references it and
**mints no RBAC at runtime** ([03](03-security-model.md) §4).

**F5 — Chat ingress & routing (human → agent):** a human message from Google Chat or Slack enters the
**ChatOps gateway** (C15), which normalizes the platform, resolves the **target agent** —
deterministically from a slash command or `@<tier>-<scope>` handle, or by NL inference as fallback
([02](02-agent-personas.md) §2.4) — enforces that agent's trusted-human allowlist (`AllowedUsers`,
[03](03-security-model.md) §4a) **before** dispatch, and forwards the message to the addressed agent's
pod. Routing is a convenience, **never** an authz signal: a mis-route lands only on an agent the human
may already reach, bounded by its read-only ceiling. The turn is audited with requester + resolved
agent + routing mode ([06](06-api-and-data-contracts.md) §2b). This is gateway→agent message dispatch,
**not** agent-to-agent coordination (which stays indirect, F3).

## 5. Shared services detail

- **Inference (C5):** LiteLLM proxy for hosted models (Gemini/OpenAI), vLLM for local GPU models;
  exposes a unified Completions API; **per-tier/per-tenant virtual keys** provide budget, rate-limit,
  and log isolation on the shared proxy; Prometheus metrics + OTel traces exported.
- **Minty (C6):** the _only_ credential path for repo writes; issues short-lived GitHub App tokens
  via KMS + Workload Identity. No static git creds anywhere.
- **Cross-cluster connectivity (spoke → hub):** spokes consume the hub's Inference (C5) and Minty (C6)
  **remotely**. v1 exposes each as a **VPC-internal endpoint** (an internal LoadBalancer / private
  `Service` on the shared VPC — never public); spoke agents reach them over private networking, and each
  spoke's default-deny egress NetworkPolicy ([03](03-security-model.md) §10) allowlists exactly those two
  hub endpoints (alongside cloud APIs, GitHub-via-Minty, and MCP grounding). Authn is the **LiteLLM
  per-tier/per-tenant virtual key** for inference and **Workload Identity** for Minty — **no
  cross-cluster Kubernetes credentials** (§7).
- **mem0/Qdrant (C10) — deferred post-v1:** semantic recall is **not in v1** ([02](02-agent-personas.md)
  §2.3). If introduced later, default to a single shared Qdrant in the hub with **server-side** scope
  isolation (per-scope collections / access-controlled keys) and treat recall as best-effort.
- **OKF base (C9):** curated knowledge as markdown-in-git; lives in the GitOps repo under the
  **`knowledge/` root** (decided — outside the paths the pipeline deploys, so it is never applied to a
  cluster; a dedicated repo stays optional for later, `06` §5).
- **Observability (C12):** OTel → `gke-managed-otel` → Cloud Trace/Logging + Managed Prometheus;
  carries requester/trace/session for attribution (`docs/designs/audit-logging-user-attribution.md`).
- **v1 security SLIs (audit-log-derived):** two continuous alerts off Cloud Logging / Managed
  Prometheus — (1) **direct-mutation = 0** (fire on any cluster/cloud _write_ whose actor is an agent
  identity); (2) **cross-scope escape = 0** (fire on any agent read or `SubjectAccessReview`-allow
  outside its tier scope). These operationalize [01](01-vision-scope.md) §7's two SLIs beyond the
  point-in-time negative tests (03 §11).

## 6. Non-functional requirements (targets — defaults, tune later)

| Dimension          | Default target                                                                                                                                                                 | Rationale                                                          |
| ------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------ |
| Fleet scale        | ≥ 50 spoke clusters per hub                                                                                                                                                    | Fleet-governance use case                                          |
| Agents per cluster | 1 Cluster Admin + ≤ 200 Dev Team (namespaces)                                                                                                                                  | Namespace density on GKE                                           |
| Chat turn latency  | p95 < 10 s for read/plan; async for mutations. Deterministic routing (slash / handle) adds no inference; NL routing (F5) adds one router call                                  | Mutations are PR-gated, not synchronous                            |
| Availability       | Cluster keeps running last-synced state if hub down; spoke **agents pause** (hub-hosted inference/Minty — [04](04-workflow-model.md) §6); agents stateless-restartable         | No cascade of _reconciled state_; agent reasoning is hub-dependent |
| Recovery           | Agent pod restart < a few s (PVC-backed state, atomic writes)                                                                                                                  | `multiuser_memory` eviction safety                                 |
| Cost               | Shared inference in hub; Spot-eligible agent pods; idle Developer Team Agents can `scaleToZero` (a planned CRD field) to bound the per-namespace pod footprint at ~200/cluster | Avoid per-cluster duplication                                      |

These are **defaults for a builder**, not commitments; revisit under load testing.

## 7. Deployment-model decisions

- **Controller runtime scope — the kube-agents controller runs per cluster.** Each cluster's controller
  reconciles **only its own cluster's** `Agent` CRs (hub → the platform-tier agent; each spoke → its
  cluster-admin + developer-team agents, from CRs + identity manifests the pipeline applies under
  `clusters/<self>/agents/`). No cross-cluster credentials; a new spoke gets the controller at
  provisioning (bootstrap, next bullet). This preserves failure isolation ([04](04-workflow-model.md) §6)
  and least privilege ([03](03-security-model.md)).
- **Spoke bootstrap sequence — provisioned, not self-installed.** A fresh spoke has no controller and no
  agent yet, so bootstrap is part of the **cluster-provisioning PR** the Platform Agent authors: the same
  pipeline run that creates the cluster also installs **cert-manager** + the **kube-agents controller**
  and applies `clusters/<self>/agents/` (the cluster-admin `Agent` CR + its pre-created read-only
  identity). Only then does the spoke's own controller reconcile the Cluster Admin Agent pod. This
  resolves the chicken-and-egg (the in-cluster agent cannot install its own runtime) and keeps identity
  pipeline-applied, never runtime-minted ([03](03-security-model.md) §4,
  [07](07-implementation-roadmap.md) Phase 2).
- **Single-cluster install — collapse topology, not personas.** One cluster plays hub + spoke: the
  controller + all three agent tiers + shared services (inference, Minty) run in it, shared services
  **once**, and a single deploy pipeline covers both `fleet/` and `clusters/<self>/`. All three personas
  still run; the persona model and isolation proof are identical to a multi-cluster install.
- **OKF location — `knowledge/` root in the GitOps repo.** Reuses the same PR/review flow + Minty
  token; it lives outside the paths the pipeline deploys (`clusters/<cluster>/`, `fleet/`), so it is
  never applied to a cluster. A dedicated knowledge repo stays optional if volume/governance later
  requires it ([06](06-api-and-data-contracts.md) §5).
- **Semantic recall (mem0/Qdrant) — deferred post-v1.** v1 coordinates on GitOps + OKF only
  ([02](02-agent-personas.md) §2.3), because OKF-in-git covers durable shared knowledge and the
  semantic-recall need is unproven. If later added: a single shared Qdrant in the hub with
  **server-side** scope isolation; recall best-effort.

## 8. Verification

- **Controller pod spec:** each agent pod the controller reconciles has `spec.serviceAccountName` = its
  read-only KSA, the correct `namespace`, `runtimeClassName` where required, and a hardened
  securityContext (`runAsNonRoot`, seccomp `RuntimeDefault`, `allowPrivilegeEscalation: false`).
- **Placement:** Platform in the hub (`kubeagents-system`); each Cluster Admin in its cluster; each
  Developer Team in its namespace.
- **Failure isolation (chaos):** kill the hub → spoke workloads keep running (agents pause); kill the
  controller in a cluster → running agent pods continue and no new reconciles occur; kill a Cluster
  Admin Agent → its Developer Team Agents keep running.
- **Unopinionated actuation:** actuation is the customer's CI/CD; nothing requires a bundled GitOps
  engine (no Config Sync/Connector) to be installed.
