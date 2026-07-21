# Design 06: API & Data Contracts

**Status:** ✅ Agreed — started 2026-07-21

**Charter:** [README.md](README.md) · **Depends on:** 01–05 · **Tier:** Buildable (bridging)

---

## TL;DR

The exact interfaces a builder implements against: a single tier-discriminated **`Agent` CRD**
(reusing today's shared specs), the **identity-minting** contract (read-only per tier), the **GitOps
repo layout** with Config Sync/Connector conventions, the **OKF** knowledge schema, **session** state
keys (semantic-recall/mem0 deferred post-v1), the **review-gate** contract, and the **MCP tool**
changes that make agents read-only. API group is `kubeagents.x-k8s.io`; namespace convention
`kubeagents-system`.

---

## 1. A single `Agent` CRD (tier-discriminated)

All three personas are **one Kubernetes kind** — a single `Agent` CRD with a `tier` discriminator —
not three near-identical CRDs. It reuses the existing shared building blocks in
`k8s-operator/api/v1alpha1/common_types.go`:

- `AgentSpec` = `Deployment` (`DeploymentSpec`) + `Security` (`SecuritySpec`)
- `HarnessSpec` = `ClusterName`, `Location`, `ProjectID`, `Hermes`, `Memory`
- `IntegrationSpec` = `GitHub` (+ chat specs for entrypoint agents)
- `AgentStatus` = `Phase`, `Address`, `Conditions`, `DeploymentStatus`, …

plus the tier/scope/parent fields below. **Why one kind:** the tiers differ only in `tier` + `scope` +
`parentRef` + default (read-only) permissions — a single CRD means **one reconciler, one validating
webhook, and one schema to version** ([02](02-agent-personas.md) §8). Migration: today's
`PlatformAgent` becomes `Agent{tier: platform}`.

### 1.1 Tier, scope, and parent fields

```go
// Tier selects the persona / containment level. Immutable after creation.
type Tier string // "platform" | "cluster-admin" | "developer-team"

// ScopeSpec pins an agent to its level of the containment hierarchy.
type ScopeSpec struct {
    ProjectID   string `json:"projectId"`             // all tiers
    ClusterName string `json:"clusterName,omitempty"` // cluster + namespace tiers
    Namespace   string `json:"namespace,omitempty"`   // namespace tier only
}

// ParentRef links a child Agent to the Agent that governs it (cascade + owner ref).
// Kind is always "Agent"; only the name is needed. Required for non-platform tiers.
type ParentRef struct {
    Name string `json:"name"`
}
```

Standard labels on every `Agent` object (`02` §6.1): `kube-agents/tier`
(`platform|cluster-admin|developer-team`), `kube-agents/parent`. Child CRs carry an `ownerReference`
to the parent `Agent` CR.

### 1.2 Per-tier field usage & cardinality

| `spec.tier` | Required scope fields | `parentRef` | Cardinality guard |
|-------------|-----------------------|-------------|-------------------|
| `platform` | `projectId` | — (root) | 1 per project |
| `cluster-admin` | `projectId`, `clusterName` | parent `Agent{tier: platform}` | 1 per cluster (webhook: unique `clusterName`) |
| `developer-team` | `projectId`, `clusterName`, `namespace` | parent `Agent{tier: cluster-admin}` | 1 per namespace (webhook: unique `namespace`) |

Tier↔scope-field consistency is enforced in-CRD by CEL; cardinality and the parent-tier relationship
are cross-object checks in the validating webhook (§10). Each entrypoint agent may set chat
integration for its audience.

## 2. Identity-minting contract (read-only per tier)

Each agent's identity is **read-only and declarative**. Read-only RBAC (ServiceAccount +
Role/ClusterRole + binding) plus the read-only cloud SA mapping are **rendered from the CR's `tier` +
`ScopeSpec` via a template, committed to the GitOps repo, and applied by Config Sync** after human
review — like all other config. The **only** write capability an agent gets is a Minty-brokered
GitHub token.

**Template-derived, thin spec, GitOps-applied (decided):** identity derives from `tier` +
`ScopeSpec` alone — `SecuritySpec` gains **no** RBAC/scope fields, so a CR cannot express "write" or
"another scope". The kube-agents **operator does not mint RBAC** and holds **no RBAC-granting
(`escalate`/`bind`) permissions** — it only reconciles agent runtime objects (Deployment/Service/
PVC). The sole applier is **Config Sync**. Read-only is enforced **in depth (all v1)**: the
**review-gate** blocks any RBAC granting an agent SA a write verb (shift-left); a
**`ValidatingAdmissionPolicy`** denies agent-SA write verbs and wrong-scope bindings at apply time;
and the **operator's validating webhook** enforces the child ⊆ parent ceiling using CRD lineage
(vetoes only — the operator validates, never grants). See [03](03-security-model.md) §4.

Pattern to generalize from today's `k8s-operator/config/agent_rbac/platformagent.yaml` (which today
still grants writes — those verbs must be **removed** for the end state):

| Tier | K8s permission (minted) | Cloud SA (Workload Identity) |
|------|-------------------------|------------------------------|
| Platform | `get/list/watch` cluster-wide; `get/list/watch` on `kubeagents.x-k8s.io` and `container.cnrm.cloud.google.com` (**no** create/update/delete) | project-scoped **viewer** roles |
| Cluster Admin | `get/list/watch` scoped to its cluster | cluster-scoped viewer |
| Developer Team | `Role` `get/list/watch` in its **one namespace** only | namespace-scoped viewer |

**Downward attenuation ([03](03-security-model.md) §4):** a child's RBAC is a reviewed subset of read
scope rendered by template; the parent (read-only) cannot author broader RBAC. **Enforcement (v1,
defense in depth):** (1) review-gate blocks write/over-scope grants shift-left; (2) a
`ValidatingAdmissionPolicy` denies agent-SA write verbs / wrong-scope bindings at apply time; (3) the
operator's validating webhook enforces the child ⊆ parent ceiling. Config Sync is the sole applier;
the operator validates but holds no RBAC-granting perms.

## 3. GitOps repository layout & propose/apply contract

Single source of truth (`05` C13). Recommended layout:

```
<gitops-repo>/
├── clusters/<cluster>/            # per-cluster desired state (synced by that cluster's Config Sync)
│   ├── config-connector/          # KCC CRs: ContainerCluster, IAMPolicyMember, etc.
│   ├── namespaces/<ns>/           # Namespace, RBAC, NetworkPolicy, ResourceQuota, workloads
│   └── agents/                    # Agent CRs (cluster-admin / developer-team tiers)
├── fleet/                         # project-level policy, Agent CR (platform tier)
├── knowledge/                     # OKF base (§5)
└── policy/                        # admission policies (Gatekeeper/Kyverno)
```

**Propose contract (`submit-suggestion`, `agents/platform/skills/submit-suggestion/`):** branch
`<<tier>>-agent/<change_type>-<target>` → stage only targeted files (never `git add .`) →
Conventional Commit → PR via Minty token. **Apply contract:** each cluster runs a Config Sync
`RootSync` pointed at `clusters/<cluster>/`; the hub also syncs `fleet/`. **Cloud resources** are
KCC CRs under `config-connector/`, reconciled by Config Connector — never direct API calls.

## 4. Config Sync & Config Connector conventions

- **Config Sync:** one `RootSync` per cluster → `clusters/<cluster>/`; `RepoSync` per namespace
  optional for delegated dev-team paths. Sync status is the reconcile signal agents read (F2).
- **Config Connector:** installed per cluster; agents author `ContainerCluster`,
  `ContainerNodePool`, `IAMServiceAccount`, `IAMPolicyMember`, etc. as CRs committed to the repo.
  (Today the Platform Agent has write RBAC on `containerclusters` and writes directly — end state
  moves this authoring into the repo.)

## 5. OKF knowledge contract

OKF = markdown + YAML frontmatter in the GitOps repo's **`knowledge/` root** (decided; a dedicated
repo stays optional for later — see [07](07-implementation-roadmap.md) §3). It lives outside Config
Sync's synced paths (`clusters/<cluster>/`, `fleet/`), so it is never applied to a cluster. Required
frontmatter field: `type`. Convention for kube-agents knowledge types:

| `type` | Purpose | Key frontmatter |
|--------|---------|-----------------|
| `cluster-blueprint` | Standard cluster config baseline | `title, tags, resource, timestamp` |
| `tenancy-model` | Namespace isolation standard | `title, tags` |
| `runbook` | Operational procedure (SRE CUJ) | `title, tags, timestamp` |
| `metric-definition` | Named metric/KPI definition | `title, tags, resource` |
| `escalation` | A cross-tier request not yet a change | `title, tags, timestamp, resource` |
| `observation` | A durable finding worth sharing | `title, tags, timestamp` |

Layout mirrors OKF: `knowledge/{index.md, <type>/…}`; markdown links form the knowledge graph;
optional `log.md` for history. Agents **read** OKF for context and **propose** updates via PR
(curate-as-code); humans approve. OKF holds durable knowledge only — **not** session state.

## 6. Session-state contract (mem0 deferred post-v1)

**Semantic recall (mem0/Qdrant) is deferred post-v1** ([02](02-agent-personas.md) §2.3); v1 ships no
vector store. If introduced later, scope every insert/query by a composite key `{tier}:{scope-id}`
(e.g. `cluster-admin:cluster-a`, `developer-team:cluster-a/team-x`) with **server-side** isolation —
each scope mapped to its own Qdrant collection / access-controlled key, never a client-supplied filter
(a cross-scope read would be an isolation escape, [03](03-security-model.md)) — and TTL entries that
graduate to OKF via PR (§10).

**Session state (existing, `multiuser_memory`):** `session_db.sqlite` keyed by
platform/space/thread; per-user memory in `memories/users/<safe_user_id>.md`; shared SOPs in
`memories/MEMORY.md`. Per-user isolation by runtime `user_id`. This stays as-is; do **not** move it
into OKF or mem0.

## 7. Review-gate contract ([04](04-workflow-model.md) §3)

- **Trigger:** PRs touching `**/config-connector/**`, `**/agents/**`, `**/policy/**`, `**/rbac/**`,
  NetworkPolicies, or agent config/`SOUL.md`.
- **Runners:** `review-security-k8s-main` (general) and `review-security-k8s-agents-main` (agent)
  from `.agents/skills/`; each emits the suite's JSON finding schema
  `[{agent, findings:[{message,file,line}]}]`.
- **Blocking policy (default):** any unmitigated **high/critical** finding blocks merge; medium/low
  are advisory. Findings triaged against project context per the skills' step 3.
- **Where it runs (decided):** **GitHub Actions on PR + a heartbeat re-run** against live state
  (option A); CI is authoritative and runs outside the agent. An in-agent pre-check, if ever added,
  is advisory-only and never the enforcer.

## 8. Audit & attribution contract

Reuse `docs/designs/audit-logging-user-attribution.md`: every agent action carries trace ID, Hermes
session ID, and authenticated requester through OTel resource attributes and Cloud Logging. The
merge/approver identity and PR URL are the durable attribution for any mutation.

## 9. MCP tool changes (make agents read-only)

The concrete code delta that enforces [03](03-security-model.md):

| Tool / server | Today | End state |
|---------------|-------|-----------|
| `create_cluster` (`platform_mcp_server.py`) | Direct GCP mutation | **Removed**; replaced by "author KCC `ContainerCluster` CR + open PR" |
| `gke` MCP (`container.googleapis.com`) | Read + write | **Read-only** subset (describe/list) only |
| Agent K8s RBAC | write on `containerclusters`, `kubeagents.x-k8s.io` | **read-only** (§2) |
| `submit-suggestion` | exists | becomes the sole mutation path for all tiers |

## 10. Open questions (defaults in [07](07-implementation-roadmap.md))

- CRD validation — _resolved (2026-07-21):_ **split by capability.** **CEL `x-kubernetes-validations`**
  (in-CRD, single-object): `tier` immutability, tier↔scope-field consistency (namespace tier requires
  `scope.namespace`; cluster tier sets `scope.clusterName`, not `namespace`; platform tier sets
  neither), and `parentRef` required for non-platform tiers. **Validating webhook** (cross-object):
  cardinality uniqueness, the **parent's tier** is the expected one (developer-team→cluster-admin,
  cluster-admin→platform), and the RBAC attenuation ceiling (child ⊆ parent,
  [03](03-security-model.md) §4). Exact rule set implemented in Phase 2.
- RepoSync delegation — _resolved (2026-07-21):_ **single `RootSync` per cluster** (option A); the
  namespace isolation that matters is enforced by agent RBAC + admission ([03](03-security-model.md)),
  not reconciler topology. Add per-namespace `RepoSync` only when a team needs reconciler-credential
  isolation or a delegated source repo (§4).
- OKF `type` vocabulary — _resolved (2026-07-21):_ **open/extensible** (option A); the six types in
  §5 are the canonical starting set, `type` is a documented convention (not a hard enum), and new
  types are added by PR (curate-as-code) as needs arise.
- mem0 retention/graduation — _deferred post-v1 with mem0:_ if/when semantic recall is introduced,
  mem0 entries **TTL by default** (tunable, ~30–90 days) and durable observations **graduate mem0 →
  OKF** via human-reviewed PR (mem0 disposable recall, OKF the curated record). Not applicable in v1.
