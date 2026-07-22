# Design 06: API & Data Contracts

**Status:** ✅ Agreed — started 2026-07-21

**Charter:** [README.md](README.md) · **Depends on:** 01–05 · **Tier:** Buildable (bridging)

---

## TL;DR

The exact interfaces a builder implements against: a single tier-discriminated **`Agent` CRD**
(reusing today's shared specs), the **identity-minting** contract (read-only per tier), the
**user-authorization** contract (down-scope to the requester), the **GitOps repo layout** with Config
Sync/Connector conventions, the **OKF** knowledge schema, **session** state keys (semantic-recall/mem0
deferred post-v1), the **review-gate** contract, and the **MCP tool** changes that make agents
read-only. API group is `kubeagents.x-k8s.io`; namespace convention `kubeagents-system`.

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

| `spec.tier`      | Required scope fields                   | `parentRef`                         | Cardinality guard                             |
| ---------------- | --------------------------------------- | ----------------------------------- | --------------------------------------------- |
| `platform`       | `projectId`                             | — (root)                            | 1 per project                                 |
| `cluster-admin`  | `projectId`, `clusterName`              | parent `Agent{tier: platform}`      | 1 per cluster (webhook: unique `clusterName`) |
| `developer-team` | `projectId`, `clusterName`, `namespace` | parent `Agent{tier: cluster-admin}` | 1 per namespace (webhook: unique `namespace`) |

**Validation.** Single-object rules are CEL `x-kubernetes-validations` in the CRD — `tier`
immutability, tier↔scope-field consistency (namespace tier requires `scope.namespace`; cluster tier
sets `scope.clusterName`, not `namespace`; platform tier sets neither), and `parentRef` required for
non-platform tiers. Cross-object rules are the operator's **validating webhook** — cardinality
uniqueness, the parent's `tier` is the expected one (developer-team→cluster-admin,
cluster-admin→platform), and the RBAC attenuation ceiling (child ⊆ parent, [03](03-security-model.md)
§4). Each entrypoint agent may set chat integration for its audience.

## 2. Identity-minting contract (read-only per tier)

Each agent's identity is **read-only and declarative**. Read-only RBAC (ServiceAccount +
Role/ClusterRole + binding) plus the read-only cloud SA mapping are **rendered from the CR's `tier` +
`ScopeSpec` via a template, committed to the GitOps repo, and applied by the CI/CD pipeline** after
human review — like all other config. The **only** write capability an agent gets is a Minty-brokered
GitHub token.

**Template-derived, thin spec, pipeline-applied (decided):** identity derives from `tier` +
`ScopeSpec` alone — `SecuritySpec` gains **no** RBAC/scope fields, so a CR cannot express "write" or
"another scope". The kube-agents **operator does not mint RBAC** and holds **no RBAC-granting
(`escalate`/`bind`) permissions** — it only reconciles agent runtime objects (Deployment/Service/
PVC). The sole applier is the **CI/CD pipeline**. Read-only is enforced **in depth (all v1)**: the
**review-gate** blocks any RBAC granting an agent SA a write verb (shift-left); a
**`ValidatingAdmissionPolicy`** denies agent-SA write verbs and wrong-scope bindings at apply time;
and the **operator's validating webhook** enforces the child ⊆ parent ceiling using CRD lineage
(vetoes only — the operator validates, never grants). See [03](03-security-model.md) §4.

Pattern to generalize from today's `k8s-operator/config/agent_rbac/platformagent.yaml` (which today
still grants writes — those verbs must be **removed** for the end state):

| Tier           | K8s permission (minted)                                                                                                                      | Cloud SA (Workload Identity)    |
| -------------- | -------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------- |
| Platform       | `get/list/watch` cluster-wide; `get/list/watch` on `kubeagents.x-k8s.io` (and provisioning CRs such as KCC `*.cnrm.cloud.google.com` where the customer runs them); cloud state read via the read-only cloud SA (**no** create/update/delete) | project-scoped **viewer** roles |
| Cluster Admin  | `get/list/watch` scoped to its cluster                                                                                                       | cluster-scoped viewer           |
| Developer Team | `Role` `get/list/watch` in its **one namespace** only                                                                                        | namespace-scoped viewer         |

All tiers additionally hold **`create` on `subjectaccessreviews`** (delegated-authz, the
`system:auth-delegator` pattern) so they can _check_ a requester's permissions for the
user-authorization pre-check (§2a) — a check, never impersonation or a workload write. The
authorization gateway (`05` C14) holds the same for authoritative enforcement.

**Downward attenuation ([03](03-security-model.md) §4):** a child's RBAC is a reviewed subset of read
scope rendered by template; the parent (read-only) cannot author broader RBAC. **Enforcement (v1,
defense in depth):** (1) review-gate blocks write/over-scope grants shift-left; (2) a
`ValidatingAdmissionPolicy` denies agent-SA write verbs / wrong-scope bindings at apply time; (3) the
operator's validating webhook enforces the child ⊆ parent ceiling. The CI/CD pipeline is the sole
applier; the operator validates but holds no RBAC-granting perms.

## 2a. User-authorization contract (down-scope to the requester)

Implements [03](03-security-model.md) §4a — for a human request, the agent's effective authority is
**agent scope ∩ the requester's own permissions** (no confused deputy).

**Requester identity propagation.** The authorization gateway (`05` C14) authenticates the human
(Google/GCP identity; mapped K8s user + groups) and carries the principal on the session alongside the
trace/session IDs (`docs/designs/audit-logging-user-attribution.md`). Model output is never treated as
an identity or authorization signal.

**Kubernetes check — `SubjectAccessReview` (check-then-act, no impersonation):**

```yaml
apiVersion: authorization.k8s.io/v1
kind: SubjectAccessReview
spec:
  user: <requester> # from the authenticated session
  groups: [<requester-groups>]
  resourceAttributes:
    verb: get # or list/watch, or the proposed change's verb
    resource: pods
    namespace: team-a # the target of the request
```

Allowed only if `status.allowed == true`. The checking identity (gateway SA — and the agent SA for its
shift-left pre-check) needs just **`create` on `subjectaccessreviews`** (`system:auth-delegator`
delegated authz) — a check, not impersonation, and not a write to any workload.

**GCP check — IAM.** Verify the requester holds the required permissions on the target
resource/project via `iam.testIamPermissions` (or the Policy Troubleshooter API), evaluated for the
requester's principal.

**Application.**

- **Reads:** the gateway filters results to what the requester may see (down-scoped reads); the agent
  never returns data the user couldn't read themselves.
- **Proposals:** the agent will not author a change the requester lacks permission to make; the PR is
  attributed to the requester and still passes the review-gate + human merge (§7,
  [04](04-workflow-model.md)).
- **Deny:** unauthorized → refuse, explained and attributed to the requester.

**Enforcement:** authoritative at the gateway (outside the LLM loop); the agent's own pre-check is
shift-left only. Heartbeat/escalation actions have no requester and run under the agent's own
read-only scope.

## 3. GitOps repository layout & propose/apply contract

Single source of truth (`05` C13). Recommended layout:

```
<gitops-repo>/
├── clusters/<cluster>/            # per-cluster desired state (applied by that target's pipeline)
│   ├── provisioning/              # cloud/cluster resources: KCC YAML or Terraform HCL (per customer)
│   ├── namespaces/<ns>/           # Namespace, RBAC, NetworkPolicy, ResourceQuota, workloads
│   └── agents/                    # Agent CRs (cluster-admin / developer-team tiers)
├── fleet/                         # project-level policy, Agent CR (platform tier)
├── knowledge/                     # OKF base (§5)
├── policy/                        # admission policies (Gatekeeper/Kyverno)
└── .github/workflows/ (or .ci/)   # the actuation pipeline config (customer's CI/CD)
```

**Propose contract (`submit-suggestion`, `agents/platform/skills/submit-suggestion/`):** branch
`<<tier>>-agent/<change_type>-<target>` → stage only targeted files (never `git add .`) →
Conventional Commit → PR via Minty token. **Apply contract:** on merge, the **customer's CI/CD
pipeline** applies the changed paths — `kubectl apply` for Kubernetes/KCC YAML, `terraform apply` for
HCL — to the target cluster and cloud. kube-agents never calls the cloud/cluster APIs directly.

## 4. Actuation & IaC conventions (unopinionated)

kube-agents integrates with the customer's existing pipeline and IaC rather than mandating one:

- **Artifact format:** the agent generates **KCC YAML _or_ Terraform HCL**, per the customer's
  standard. Provisioning resources (clusters, node pools, IAM) live under `provisioning/`; workloads
  and namespace config as manifests under `namespaces/<ns>/`.
- **Actuation:** a pipeline per target (cluster/environment) applies the merged artifact on merge —
  GitHub Actions, CircleCI, Jenkins, or an existing GitOps engine (Argo/Flux/Atlantis) if the customer
  already runs one. Drift correction is a scheduled pipeline re-apply and/or an agent heartbeat that
  proposes a corrective PR (§04 §5.1). The pipeline's run/resource status is the signal agents read
  (F2).
- **Credentials:** the pipeline holds least-privilege deploy credentials scoped per target; agents
  hold none (they are read-only). (Today the Platform Agent has write RBAC on `containerclusters` and
  writes directly — the end state moves all authoring into the repo and all applying into the
  pipeline.)

## 5. OKF knowledge contract

OKF = markdown + YAML frontmatter in the GitOps repo's **`knowledge/` root** (a dedicated repo stays
optional for later). It lives outside the paths the pipeline deploys (`clusters/<cluster>/`,
`fleet/`), so it is never applied to a cluster. Required frontmatter field: `type`. Convention for
kube-agents knowledge types:

| `type`              | Purpose                               | Key frontmatter                    |
| ------------------- | ------------------------------------- | ---------------------------------- |
| `cluster-blueprint` | Standard cluster config baseline      | `title, tags, resource, timestamp` |
| `tenancy-model`     | Namespace isolation standard          | `title, tags`                      |
| `runbook`           | Operational procedure (SRE CUJ)       | `title, tags, timestamp`           |
| `metric-definition` | Named metric/KPI definition           | `title, tags, resource`            |
| `escalation`        | A cross-tier request not yet a change | `title, tags, timestamp, resource` |
| `observation`       | A durable finding worth sharing       | `title, tags, timestamp`           |

The six types are the canonical starting set; `type` is an **open convention, not a hard enum** — new
types are added by PR as needs arise. Layout mirrors OKF: `knowledge/{index.md, <type>/…}`; markdown
links form the knowledge graph; optional `log.md` for history. Agents **read** OKF for context and
**propose** updates via PR (curate-as-code); humans approve. OKF holds durable knowledge only —
**not** session state.

## 6. Session-state contract (mem0 deferred post-v1)

**Semantic recall (mem0/Qdrant) is deferred post-v1** ([02](02-agent-personas.md) §2.3); v1 ships no
vector store. If introduced later, scope every insert/query by a composite key `{tier}:{scope-id}`
(e.g. `cluster-admin:cluster-a`, `developer-team:cluster-a/team-x`) with **server-side** isolation —
each scope mapped to its own Qdrant collection / access-controlled key, never a client-supplied filter
(a cross-scope read would be an isolation escape, [03](03-security-model.md)) — and TTL entries
(default ~30–90 days) that graduate durable observations to OKF via a human-reviewed PR.

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
- **Blocking policy:** any unmitigated **high/critical** finding blocks merge; medium/low are
  advisory. Findings triaged against project context per the skills' step 3.
- **Where it runs:** **GitHub Actions on PR + a heartbeat re-run** against live state; CI is
  authoritative and runs outside the agent. An in-agent pre-check, if ever added, is advisory-only and
  never the enforcer.

## 8. Audit & attribution contract

Reuse `docs/designs/audit-logging-user-attribution.md`: every agent action carries trace ID, Hermes
session ID, and authenticated requester through OTel resource attributes and Cloud Logging. The
merge/approver identity and PR URL are the durable attribution for any mutation.

## 9. MCP tool changes (make agents read-only)

The concrete code delta that enforces [03](03-security-model.md):

| Tool / server                               | Today                                               | End state                                                             |
| ------------------------------------------- | --------------------------------------------------- | --------------------------------------------------------------------- |
| `create_cluster` (`platform_mcp_server.py`) | Direct GCP mutation                                 | **Removed**; replaced by "author KCC YAML or Terraform HCL + open PR" |
| `gke` MCP (`container.googleapis.com`)      | Read + write                                        | **Read-only** subset (describe/list) only                             |
| Agent K8s RBAC                              | write on `containerclusters`, `kubeagents.x-k8s.io` | **read-only** (§2)                                                    |
| `submit-suggestion`                         | exists                                              | becomes the sole mutation path for all tiers                          |
