# Design 06: API & Data Contracts

**Status:** ✅ Agreed

**Overview:** [README.md](README.md) · **Depends on:** 01–05 · **Tier:** Buildable (bridging)

---

## TL;DR

The exact interfaces a builder implements against: the **`Agent` CRD** (per persona, running the Hermes
harness, reconciled by the kube-agents controller), the **identity contract** (read-only per tier;
pre-created KSA/RBAC/WI the controller references by name), the **user-authorization** contract
(_deferred hardening_ — down-scope to the requester), the **GitOps repo layout** + actuation/IaC
conventions, the **OKF** knowledge schema, **session** state keys (semantic-recall/mem0 deferred
post-v1), the **review-gate** contract, and the **MCP tool** changes that make agents read-only.
Namespace convention `kubeagents-system`; agent labels use the `kube-agents/…` prefix.

---

## 1. Agent definition — the `Agent` CRD (per persona)

Each agent is defined by one instance of a single, tier-discriminated **`Agent` custom resource**
(`kubeagents.x-k8s.io`), reconciled by the **kube-agents controller** into an isolated pod
([05](05-system-architecture.md) C1, [08](08-agent-runtime-and-identity.md)). The CR selects the
**Hermes** harness with the persona's profile/skills and carries the tier/scope/parent metadata and the
pod's identity/placement. The controller generalizes today's single `PlatformAgent` CRD + operator
(`k8s-operator/`): the `Agent` CRD **adds `tier` / `scope` / `parentRef`**, and today's `PlatformAgent`
becomes the platform-tier instance.

### 1.1 CR shape

```yaml
# an Agent custom resource (one per persona)
apiVersion: kubeagents.x-k8s.io/v1alpha1
kind: Agent
metadata:
  name: <agent-name> # e.g. platform-<project>, cluster-admin-<cluster>, developer-team-<ns>
spec:
  tier: platform | cluster-admin | developer-team # persona / containment level (immutable)
  scope:
    projectId: <proj> # all tiers
    clusterName: <cluster> # cluster + namespace tiers
    namespace: <ns> # namespace tier only (also the pod's placement namespace)
  parentRef: { name: <parent-agent> } # required for non-platform tiers
  harness: hermes # the harness the controller launches
  profile: <persona-profile-ref> # Hermes SOUL.md + skills for this persona
  serviceAccountName: <read-only-ksa> # pre-created, tier-scoped, Workload-Identity-bound (§2)
  runtimeClassName: <sandbox> # optional gVisor execution sandbox — deferred (08 §5.1)
  iac:
    format: kcc | terraform # which artifact this agent authors (customer standard, §4; default kcc)
  integration: # chat entrypoint(s) for this agent's audience (exists today, nested)
    googleChat: { allowedUsers: [<user>, …] } # trusted-human allowlist — the gateway's authz source (§2b)
    slack: { allowedUsers: [<memberId>, …] } #  same, per platform
```

_Illustrative end-state shape._ It generalizes today's `PlatformAgent`: `tier` / `scope` / `parentRef`
and `iac.format` are **new**; `serviceAccountName` and `runtimeClassName` exist today **nested** under
`spec.security` / `spec.deployment` (`k8s-operator/api/v1alpha1`), `spec.harness` is a **struct** (not
the string shown), and `integration` (with per-platform `allowedUsers`) already exists nested under
`spec.integration.{googleChat,slack}` (`platformagent_types.go:32–108`). `profile` denotes persona
selection — **v1 = a baked per-tier image** (`<tier>-agent:<tag>`, built from `agents/<tier>/` exactly as
the platform image is today; [08](08-agent-runtime-and-identity.md) §2); a mounted profile is deferred.
Phase 1/2 decide only which fields to **promote** vs keep nested ([07](07-implementation-roadmap.md)).

The controller **stamps** `kube-agents/tier`, `kube-agents/scope`, `kube-agents/parent` on the agent
**pod** (`02` §6.1); the agent's **RBAC** objects are pre-created manifests, so the **render overlay**
labels/names them (§2) — the controller mints no RBAC to label. The `serviceAccountName` / placement
`namespace` / `runtimeClassName` pod fields follow the per-pod, hardened model verified in Scion
(`pkg/api/types.go` — `serviceAccountName` is provided _for Workload Identity_;
`pkg/runtime/k8s_runtime.go`); the controller sets them when it builds the pod (natively in v1; via
Scion's launch primitive as a Phase-1 integration, [08](08-agent-runtime-and-identity.md) §2).

### 1.2 Per-tier field usage, cardinality & validation

| `tier`           | Required scope fields                   | `parentRef`             | Cardinality     |
| ---------------- | --------------------------------------- | ----------------------- | --------------- |
| `platform`       | `projectId`                             | — (root)                | 1 per project   |
| `cluster-admin`  | `projectId`, `clusterName`              | parent = platform agent | 1 per cluster   |
| `developer-team` | `projectId`, `clusterName`, `namespace` | parent = cluster-admin  | 1 per namespace |

**Validation (v1).** The `Agent` CR + its identity manifests are reviewed on the PR (the review-gate).
**Cardinality — exactly one agent per `(tier, scope)` — is enforced by the controller's validating
webhook** (a duplicate CR is rejected at apply time), not left to convention. RBAC least-privilege is
enforced at apply time by an in-tree **`ValidatingAdmissionPolicy`** (denies an agent SA any write verb
or a wrong-scope binding, [03](03-security-model.md) §4). The cross-object checks (correct parent tier;
child ⊆ parent attenuation ceiling) are **deferred** to the hardening admission webhook
([08](08-agent-runtime-and-identity.md) §5). Each entrypoint agent may set chat integration for its
audience.

## 2. Identity contract (pre-created, read-only per tier)

Each agent's identity is **read-only and declarative**. The per-agent read-only RBAC (ServiceAccount +
Role/ClusterRole + binding) plus the read-only cloud SA mapping (Workload Identity) are **rendered from
the CR's `tier` + `scope`** (by a kustomize overlay / render script kept beside the `agents/`
manifests) — the **canonical KSA is named `<tier>-agent`** (e.g. `platform-agent`), and the overlay
**stamps the `kube-agents/tier` label** on the SA and its Role/ClusterRole (that label is the VAP's
reliable selector) so the attenuation `ValidatingAdmissionPolicy` can select them
([03](03-security-model.md) §4). These manifests are
committed to the GitOps repo and applied by the CI/CD pipeline after human review — like all other
config. **The controller then sets the pod's `serviceAccountName` to that KSA by name**, so the agent pod
runs as it. The **only** write capability an agent gets is a Minty-brokered GitHub token. _(Migration:
today's operator mints a `view` binding + an "explorer" ClusterRole bound to
`spec.security.serviceAccountName`; Phase 1 unifies these into pre-created manifests bound to the
canonical `<tier>-agent` KSA, [07](07-implementation-roadmap.md).)_

**CR-derived, pre-created, pipeline-applied (decided):** identity derives from `tier` + `scope`
alone; the `Agent` CRD carries **no** RBAC/scope-granting fields, so a CR cannot express "write" or
"another scope". **Nothing mints RBAC at runtime** — the KSA/RBAC/WI are ordinary manifests created by
the CI/CD pipeline (the sole applier); the controller only consumes them (it sets `serviceAccountName`;
it does **not** create Roles/RoleBindings). Read-only is enforced **in depth (all v1)**: the
**review-gate** blocks any RBAC granting an agent SA a write verb (shift-left); an in-tree
**`ValidatingAdmissionPolicy`** denies agent-SA write verbs and wrong-scope bindings at apply time. The
cross-object child ⊆ parent ceiling (via a validating webhook) is deferred hardening
([03](03-security-model.md) §4, [08](08-agent-runtime-and-identity.md) §5).

Pattern to generalize from today's **runtime-minted** RBAC — a built-in `view` ClusterRoleBinding + a
`get/list` "explorer" ClusterRole (`buildPlatformExplorerRole` /`reconcileRBAC`,
`k8s-operator/internal/controller/`), both **already read-only** (there is no
`config/agent_rbac/platformagent.yaml`; the grant lives in code). The end-state delta is **not** "remove
write verbs" (there are none) but **stop minting RBAC at runtime** and pre-create these as reviewed,
tier-scoped manifests (per the table below):

| Tier           | K8s permission (pre-created, read-only)                                                                                                                                                                                                       | Cloud SA (Workload Identity)    |
| -------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------- |
| Platform       | `get/list/watch` cluster-wide; `get/list/watch` on `kubeagents.x-k8s.io` (and provisioning CRs such as KCC `*.cnrm.cloud.google.com` where the customer runs them); cloud state read via the read-only cloud SA (**no** create/update/delete) | project-scoped **viewer** roles |
| Cluster Admin  | `get/list/watch` scoped to its cluster                                                                                                                                                                                                        | cluster-scoped viewer           |
| Developer Team | `Role` `get/list/watch` in its **one namespace** only                                                                                                                                                                                         | namespace-scoped viewer         |

The per-request user-permission check (`SubjectAccessReview` + IAM) and its `create` on
`subjectaccessreviews` grant belong to the **deferred** user-scoped authorization (§2a) — **not in
v1**. v1 secures the human→agent boundary with trusted-human access + the read-only agent ceiling
([03](03-security-model.md) §4a, [08](08-agent-runtime-and-identity.md) §2), so agents need no
SAR-create grant.

**Downward attenuation ([03](03-security-model.md) §4):** a child's RBAC is a reviewed subset of read
scope rendered from the CR's `tier` + `scope`; the parent (read-only) cannot author broader RBAC. **Enforcement
(v1):** (1) review-gate blocks write/over-scope grants shift-left; (2) an in-tree
`ValidatingAdmissionPolicy` denies agent-SA write verbs / wrong-scope bindings at apply time. The
cross-object child ⊆ parent ceiling (a validating webhook) is **deferred hardening**
([08](08-agent-runtime-and-identity.md) §5). The CI/CD pipeline is the sole applier; no runtime
component grants RBAC.

## 2a. User-authorization contract — DEFERRED hardening (down-scope to the requester)

Implements [03](03-security-model.md) §4a — for a human request, the agent's effective authority is
**agent scope ∩ the requester's own permissions** (no confused deputy).

> **Deferred — not in v1 ([08](08-agent-runtime-and-identity.md) §2, §5).** This entire contract is the
> **user-scoped authorization** hardening. **v1 does not check the requester's permissions** and does
> not down-scope the agent to them; the human→agent boundary is secured by trusted-human access + the
> read-only agent ceiling ([03](03-security-model.md) §4a). Everything below applies only once the
> delegate model is adopted.

**Requester identity propagation.** The agent's authenticated chat entrypoint establishes the human
(Google/GCP identity; mapped K8s user + groups) and carries the principal on the session alongside the
trace/session IDs (`docs/designs/audit-logging-user-attribution.md`) — in the hardening path this
moves to the gateway (`05` C14). Model output is never treated as an identity or authorization signal.

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

## 2b. ChatOps addressing & routing contract

How a human names the agent they want ([02](02-agent-personas.md) §2.4). The **ChatOps gateway**
(C15, [05](05-system-architecture.md)) resolves every inbound chat message to exactly one
`(tier, scope)` **`Agent` CR** via three modes, deterministic first. _(This is v1-compatible: it
enforces the existing trusted-human allowlist, adds no per-request authorization, and dispatches to
the separate per-tier pods — it is **not** the deferred co-located multiplexer or the deferred authz
gateway C14, [08](08-agent-runtime-and-identity.md) §3.)_

**Handle grammar.** An agent's handle is its `<tier>-<scope>` name (`02` §6.1):

| Tier             | Canonical handle           | Short alias          | Resolves to `(tier, scope)` |
| ---------------- | -------------------------- | -------------------- | --------------------------- |
| `platform`       | `@platform-<project>`      | —                    | `(platform, project)`       |
| `cluster-admin`  | `@cluster-admin-<cluster>` | `@cluster-<cluster>` | `(cluster-admin, cluster)`  |
| `developer-team` | `@developer-team-<ns>`     | `@devteam-<ns>`      | `(developer-team, ns)`      |

The map is **derived** from the same `(tier, scope)` key the cardinality webhook enforces (§1.2) —
no separate routing registry to maintain or drift.

**Slash-command grammar.** `@kage /<handle> <text>` (e.g. `/cluster-bravo`, `/devteam-charlie`)
dispatches directly to the named agent — constant-time, no inference. On Google Chat a slash command
carries a numeric `commandId`; on Slack it is a registered `/command`. Both map to the handle table
above; the gateway normalizes them to a single dispatch path.

**Resolution order:** (1) slash command → (2) explicit `@handle` → (3) NL inference (fallback; low
confidence → clarify, not guess). Modes 1–2 spend no inference; mode 3 spends one router call.

**Attribution (extends §8).** Every chat turn's audit record adds the **resolved agent** (`tier`,
`scope`) and the **routing mode** (`slash` | `handle` | `inference`) alongside the requester +
trace/session IDs. Thread affinity (sticky routing) is keyed on the session store's `thread_id`
(§6). Routing is **never** an authz signal ([03](03-security-model.md) §4a): the gateway checks the
target agent's `AllowedUsers` before dispatch, and the NL router (model output) is never trusted for
authorization.

**Allowlist source & enforcement.** The gateway resolves the **target** agent's trusted-human allowlist
by reading that `(tier, scope)` **`Agent` CR's** `integration.{googleChat,slack}.allowedUsers` (§1.1) —
the same field each agent pod's own Hermes gateway already reads (today rendered to
`GOOGLE_CHAT_ALLOWED_USERS` / `SLACK_ALLOWED_USERS` env on the pod). In today's single-agent install the
pod's own gateway enforces its allowlist; as the central router fronts multiple per-tier pods it resolves
the target CR and checks `allowedUsers` **before** dispatch, with the target pod's gateway remaining a
defense-in-depth backstop. An empty/absent `allowedUsers` means "all authenticated users" (today's
default) — a closed allowlist must be set explicitly.

## 3. GitOps repository layout & propose/apply contract

Single source of truth (`05` C13) — the **customer's own GitOps repository** (configured via the agent's
`integration.github.gitRepo`, checked out into the agent workspace), **separate from the kube-agents
source tree**; `submit-suggestion` pushes a branch here and opens the PR. This layout is scaffolded in
Phase 0 ([07](07-implementation-roadmap.md)); it does not exist in the source repo today. Recommended
layout:

```
<gitops-repo>/
├── clusters/<cluster>/            # per-cluster desired state (applied by that target's pipeline)
│   ├── provisioning/              # cloud/cluster resources: KCC YAML or Terraform HCL (per customer)
│   ├── namespaces/<ns>/           # Namespace, RBAC, NetworkPolicy, ResourceQuota, workloads
│   └── agents/                    # Agent CRs + per-agent identity (KSA/RBAC/WI) manifests
├── fleet/                         # project-level policy; platform-tier Agent CR + identity
├── knowledge/                     # OKF base (§5)
├── policy/                        # admission policies (ValidatingAdmissionPolicy; Gatekeeper/Kyverno)
└── .github/workflows/ (or .ci/)   # the actuation pipeline config (customer's CI/CD)
```

**Propose contract (`submit-suggestion`, `agents/platform/skills/submit-suggestion/`):** branch
`<<tier>>-agent/<change_type>-<target>` → stage only targeted files (never `git add .`) →
Conventional Commit → PR via Minty token. **Apply contract:** on merge, the **customer's CI/CD
pipeline** applies the changed paths — `kubectl apply` for Kubernetes/KCC YAML, `terraform apply` for
HCL — to the target cluster and cloud. kube-agents never calls the cloud/cluster APIs directly.

## 4. Actuation & IaC conventions (unopinionated)

kube-agents integrates with the customer's existing pipeline and IaC rather than mandating one:

- **Artifact format:** the agent generates **KCC YAML _or_ Terraform HCL**, selected by the agent's
  **`spec.iac.format`** (`kcc` | `terraform`, default `kcc`; §1.1) — typically uniform per install but
  settable per agent/target. Provisioning resources (clusters, node pools, IAM) live under
  `provisioning/`; workloads and namespace config as manifests under `namespaces/<ns>/`.
- **Actuation:** a pipeline per target (cluster/environment) applies the merged artifact on merge —
  GitHub Actions, CircleCI, Jenkins, or an existing GitOps engine (Argo/Flux/Atlantis) if the customer
  already runs one. Drift correction is a scheduled pipeline re-apply and/or an agent heartbeat that
  proposes a corrective PR (§04 §5.1). The pipeline's run/resource status is the signal agents read
  (F2).
- **Credentials:** the pipeline holds least-privilege deploy credentials scoped per target; agents
  hold none (they are read-only). (Today the Platform Agent writes directly via the remote `gke` MCP's
  `create_cluster` — a **cloud** write through its cloud SA's IAM, **not** K8s RBAC, which is already
  read-only — and the end state moves all authoring into the repo and all applying into the pipeline.)

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
into OKF or mem0. The gateway also uses these keys (`thread_id` / `chat_id`) for **routing thread
affinity** — a thread stays bound to the agent it was first routed to (§2b) until re-addressed.

## 7. Review-gate contract ([04](04-workflow-model.md) §3)

- **Trigger:** PRs touching `**/provisioning/**`, `**/agents/**`, `**/namespaces/**` (tenant RBAC /
  NetworkPolicy / quota / workloads), `**/policy/**`, or agent config/`SOUL.md`.
- **Runners:** `review-security-k8s-main` (general) and `review-security-k8s-agents-main` (agent)
  from `.agents/skills/`; each emits the suite's JSON finding schema
  `[{agent, findings:[{message,file,line}]}]`.
- **Blocking policy:** any unmitigated **high/critical** finding blocks merge; medium/low are
  advisory. Findings triaged against project context per the skills' step 3.
- **Where it runs:** **GitHub Actions on PR + a heartbeat re-run** against live state; CI is
  authoritative and runs outside the agent. An in-agent pre-check, if ever added, is advisory-only and
  never the enforcer.
- **Executor:** the `review-security-k8s-*` skills are **agent-driven** (they launch sub-agents), so the
  CI job runs them via a **headless harness invocation** (a Hermes/agent runner step in the Action with
  read-only cluster/repo creds), not as a plain shell script. It emits the JSON finding schema above,
  which a scoring step turns into the blocking decision.

## 8. Audit & attribution contract

Reuse `docs/designs/audit-logging-user-attribution.md`: every agent action carries trace ID, Hermes
session ID, and authenticated requester through OTel resource attributes and Cloud Logging.
Chat-initiated actions additionally carry the **resolved agent** (`tier`, `scope`) and the **routing
mode** (§2b). The merge/approver identity and PR URL are the durable attribution for any mutation.

## 9. MCP tool changes (make agents read-only)

The concrete code delta that enforces [03](03-security-model.md):

| Tool / server                                                           | Today                                                                                                                                                                                                                                                                                                                                    | End state                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| ----------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `create_cluster` (remote `gke` MCP → `container.googleapis.com`)        | Direct GCP mutation via the remote `gke` MCP proxy (`config.yaml`) — **not** a `platform_mcp_server.py` tool                                                                                                                                                                                                                             | No cluster-creating tool reaches agents; provisioning becomes "author KCC YAML or Terraform HCL + open PR"                                                                                                                                                                                                                                                                                                                                                                                                                         |
| `gke` MCP wiring — **operator-rendered config**                         | The runtime config is generated by the operator's `renderConfigYAML()` (`k8s-operator/internal/controller/platformagent_manifests.go`) into a ConfigMap **mounted read-only over** `/opt/data/config.yaml`; it hard-codes the `gke` remote proxy + `mcp-gke` toolset. The baked `agents/platform/config.yaml` is **shadowed at runtime** | Edit **`renderConfigYAML()`** (primary — runtime-authoritative): front the remote `gke` server with a **read-only tool-allowlist proxy** or drop it from `platform_toolsets` (a remote MCP's toolset can't be subset client-side). Also update the baked configs (`agents/platform/config.yaml` **and** `deploy/shared/defaults/config.yaml`) for consistency — but editing **only** them leaves the deployed agent write-capable. The config ConfigMap subPath mount should also set `readOnly: true` (currently omitted in code) |
| `apply_manifest` / `delete_cluster_manifest` (`platform_mcp_server.py`) | Undecorated helpers running `kubectl apply` / `kubectl delete` — present but **not** exposed as MCP tools                                                                                                                                                                                                                                | **Remove** the dead helpers so no `kubectl` write path can be re-exposed                                                                                                                                                                                                                                                                                                                                                                                                                                                           |
| `gke-cluster-creator` skill (`agents/platform/skills/`)                 | `SKILL.md` invokes the `create_cluster` tool                                                                                                                                                                                                                                                                                             | **Retire/adjust** — it must author KCC YAML or Terraform + open a PR, not call `create_cluster`                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| Agent K8s RBAC                                                          | **already read-only** — runtime-minted `view` binding + `get/list` "explorer" ClusterRole (no write on `containerclusters`/`kubeagents.x-k8s.io`)                                                                                                                                                                                        | **read-only, pre-created** — stop runtime-minting; render per-tier manifests (§2)                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| `submit-suggestion`                                                     | exists                                                                                                                                                                                                                                                                                                                                   | becomes the sole mutation path for all tiers                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |

## 10. Verification

- **CR schema:** every `Agent` CR validates against the CRD — `spec.tier` ∈ {platform, cluster-admin,
  developer-team}; the required `spec.scope` fields for its tier; `parentRef` present for non-platform
  tiers; `serviceAccountName` set. Creating a second CR for the same `(tier, scope)` is **rejected** by
  the controller's cardinality webhook.
- **Repo layout:** the tree matches §3 (`clusters/<cluster>/{provisioning,namespaces,agents}`,
  `fleet/`, `knowledge/`, `policy/`, and the pipeline config).
- **Identity manifests:** for each agent a read-only KSA + Role/ClusterRole + binding + Workload-Identity
  annotation exist and are referenced by the CR's `serviceAccountName`; `kubectl auth can-i` confirms
  read-only, in-scope access.
- **MCP delta:** no cluster-creating tool reaches agents (`create_cluster` unavailable); the agent's
  `gke` MCP exposes describe/list/get only — verified against the **operator-rendered** config
  (`renderConfigYAML()` / the mounted ConfigMap), **not** only the baked `agents/platform/config.yaml`;
  the `platform_mcp_server.py` `apply_manifest` / `delete_cluster_manifest` helpers are removed; agent
  RBAC read-only (grep + SAR).
- **OKF:** a validator script confirms every `knowledge/` file carries a valid `type` frontmatter and
  its markdown links resolve. (A richer OKF _visualizer_ is optional tooling to build later, not a gate.)
- **Review-gate:** the security-review suite runs on the trigger paths and **blocks** a PR with an
  unmitigated high/critical finding.
- **ChatOps routing (§2b):** a slash command (`/cluster-<c>`) and a direct handle (`@cluster-<c>`)
  each resolve to the matching `(cluster-admin, <c>)` agent **without** an inference call; an
  ambiguous NL message triggers a clarifying question, not a guess; a message addressed to an agent
  whose `AllowedUsers` excludes the requester is **refused before dispatch**; the audit record carries
  the resolved agent + routing mode.
