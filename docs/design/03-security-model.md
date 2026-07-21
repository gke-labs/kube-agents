# Design 03: Security & Trust Model

**Status:** ✅ Agreed — started 2026-07-21

**Charter:** [README.md](README.md) · **Depends on:** [01-vision-scope.md](01-vision-scope.md),
[02-agent-personas.md](02-agent-personas.md) · **Feeds:** [04-workflow-model.md](04-workflow-model.md)

---

## TL;DR

The security model makes the persona boundaries from [02](02-agent-personas.md) **provable rather
than aspirational**, and defends against the threats unique to autonomous AI agents operating
infrastructure. It rests on four pillars:

1. **Scoped identity & least privilege** — each agent tier gets an identity (K8s ServiceAccount +
   cloud Workload Identity + RBAC) confined to its scope: project / cluster / namespace.
2. **Downward-only privilege attenuation** — a parent can only cause a child to be granted a
   _strict subset_ of scope, enforced **in depth**: the review-gate blocks over-grants shift-left, a
   runtime admission policy and the operator's **validating** webhook reject them at apply time, and
   Config Sync is the sole applier. The operator **validates** the ceiling but never **grants** it
   (holds no `escalate`/`bind`), so neither a compromised parent nor a bad merge can over-grant.
3. **AI-agent-specific defenses** — prompt-injection resistance, egress/data-exfil control,
   brokered short-lived credentials, and sandboxed code execution.
4. **Declarative-only mutation** — every change is a reviewable, attributable, revertible artifact,
   never a direct cluster write. Direct access is human break-glass only, and audited.

The existing `.agents/skills/review-security-k8s-*` suite is the **continuous control** that audits
conformance to this model.

---

## 1. What we're defending against

Two distinct threat classes, both in scope:

**A. Boundary / isolation threats** — an agent (or a tenant, or a compromised workload) acting
outside its scope: a Developer Team Agent reading another namespace, a Cluster Admin Agent reaching
another cluster, privilege escalation up the hierarchy, or lateral movement between tenants.

**B. AI-agent-specific threats** — risks that exist _because_ the operator is an LLM-driven
autonomous agent:

- **Prompt injection** — malicious instructions smuggled in via chat, cluster object contents,
  tool output, logs, or a GitHub issue, aiming to redirect the agent's actions.
- **Data exfiltration** — the agent coaxed into sending secrets or cluster data to an attacker
  (via egress, a crafted PR, or a tool call).
- **Credential compromise** — theft or misuse of the tokens/identities the agent holds.
- **Untrusted code execution** — the agent running model-generated or externally-sourced code that
  attempts to escape its container.

The threat model treats **all model output and all external input as untrusted**: model output is
never a trusted identity or authorization signal (consistent with
`docs/designs/audit-logging-user-attribution.md` non-goals), and content read from the cluster,
tools, or chat is untrusted input, not instructions.

---

## 2. Trust boundaries

| Boundary | Who ↔ who | Primary risk | Primary control |
|----------|-----------|--------------|-----------------|
| Human → Agent | Authenticated user → agent chat | Impersonation, unauthorized intent | Authenticated chat (`AllowedUsers`), per-audience entrypoints ([02](02-agent-personas.md)) |
| Agent → Agent (tier) | Parent ↔ child across tiers | Privilege escalation up the cascade | Scoped identity + downward attenuation (§3, §4) |
| Agent → Kubernetes API | Agent SA → cluster API | Acting outside scope | **Read-only** RBAC scoped to tier; NetworkPolicy; admission (§4) |
| Agent → Cloud APIs | Workload Identity → cloud SA | Broad cloud blast radius | **Read-only**, per-tier cloud SA, least-privilege IAM |
| Agent → LLM / inference | Agent → LiteLLM/vLLM proxy | Prompt injection, data leak in prompts | Allowlisted egress to inference only; input treated as untrusted (§5) |
| Agent → External input | Chat / issues / cluster data / tool output | Prompt injection, exfil trigger | Untrusted-input handling, egress control, audit (§5) |
| Agent → Git / GitOps | Agent → repo | Credential theft, malicious change | Brokered short-lived tokens (Minty), PR review gate (§5, §7) |

---

## 3. Identity & least privilege per tier

Each persona receives an identity confined to exactly its scope. This is what turns
[02](02-agent-personas.md)'s "provably unable to escalate" into an enforced property.

Every agent is **read-only** on the cluster and cloud APIs — the only thing an agent can change is
the GitOps repo (via a brokered token). Scope defines what it can _read_ and what it can _propose_.

| Tier | Kubernetes API (read-only) | Cloud API (read-only) | Only write path | May NOT |
|------|----------------------------|-----------------------|-----------------|---------|
| **Platform Agent** | Project/fleet-wide read | Project-scoped read | GitOps repo (PRs) via brokered token | Any direct cluster/cloud write; operate tenant workloads |
| **Cluster Admin Agent** | Its one cluster, read | Cluster-scoped read | GitOps repo (PRs) | Any direct write; any other cluster; project scope |
| **Developer Team Agent** | Its one namespace, read | Namespace-scoped read | GitOps repo (PRs) | Any direct write; any other namespace; cluster/project scope |

**Agents hold no write RBAC on the cluster or cloud.** The actual writes are performed by the
**reconcilers** — Config Sync, Config Connector, and the kube-agents operator
([04](04-workflow-model.md) §1.1) — whose own permissions are scoped and which act only on reviewed,
merged declarative state. Even provisioning a lower-tier agent is a read-only agent proposing a CR
to the repo, applied by a reconciler.

Today the operator's `SecuritySpec` carries only `ServiceAccountName` +
`ServiceAccountAnnotations` (for Workload Identity binding), and agents hold direct-mutation tools
(the `create_cluster` MCP tool, a `gke` MCP server). **End state:** the operator provisions, per
tier, a **read-only** ServiceAccount/Role scoped to project/cluster/namespace plus a read-only
cloud SA mapping, and agents lose all direct-mutation tools — so an agent's scope is a read ceiling
the operator guarantees, and all write authority lives in the reconcilers.

---

## 4. Enforcing containment (the load-bearing isolation)

The persona hierarchy is only as strong as the mechanisms that pin each agent to its scope.

**Kubernetes-native isolation.** Namespace boundaries, RBAC (`Role`/`ClusterRole` scoped as in §3),
`NetworkPolicy` (default-deny + explicit allows, cf.
`agents/platform/skills/gke-workload-security/assets/default-deny-netpol.yaml`), `ResourceQuota`,
and admission control together enforce that an agent cannot read or mutate outside its scope.

**Downward-only privilege attenuation (key invariant).** When a parent provisions a child
([02](02-agent-personas.md) §6), it proposes a declarative bundle as a PR — the child CR **plus**
the child's read-only RBAC (SA/Role/RoleBinding), rendered from the child's `tier` + `scope` via a
template. **Config Sync applies it after human review**; the kube-agents operator does not mint
RBAC. Consequences:

- A parent can only ever cause a child to receive a _strict subset_ of **read** scope — the template
  emits read-only RBAC, and the **review-gate blocks any RBAC granting an agent SA write verbs**
  (shift-left).
- No agent can widen its own scope: the RBAC that grants access is a reviewed artifact in the repo,
  not something an agent can author unilaterally or an operator can over-grant.
- **Enforcement is layered (defense in depth), not review-gate-only.** Beyond the shift-left gate,
  two runtime backstops reject a violating grant **at apply time** — even if a bad RBAC PR merges:
  - a **`ValidatingAdmissionPolicy`** (in-tree CEL) hard-denies any `Role`/`RoleBinding` that gives
    an agent ServiceAccount a write verb, or a cluster-scoped grant to a namespace-tier agent;
  - the **operator's validating admission webhook** enforces the cross-object _ceiling_ — a child's
    scope must be ⊆ its parent's — using the CRD parent/child graph, which pure CEL cannot express.
- The operator **validates** but never **grants**: the webhook only vetoes (allow/deny), so the
  operator still holds **no RBAC-granting permissions** (no `escalate`/`bind`) and only reconciles
  agent runtime objects (Deployment/Service/PVC). The sole _applier_ remains **Config Sync**, acting
  on reviewed, merged state.
- Because agents are **read-only** (§3), a subverted agent has no write path to abuse in the first
  place; identity itself is reviewable and revertible like any other config.

**No formal break-glass (initial version).** There is no agent path across a scope boundary, and the
initial version defines **no sanctioned human break-glass** either — every change, including
emergencies, goes through human-approved GitOps. A governed direct-access path (JIT +
detect-and-reconcile) is deferred ([01](01-vision-scope.md) §8, [07](07-implementation-roadmap.md)).

---

## 5. AI-agent-specific defenses

These map directly onto the existing agent security-review sub-skills
(`.agents/skills/review-security-k8s-agents-*`), which define what "good" looks like and audit for
it.

| Threat | Defense (end state) | Review skill |
|--------|---------------------|--------------|
| **Prompt injection** | Treat all external input (chat, cluster data, tool output, issues) as untrusted data, never instructions; model output is never an authz signal; sensitive actions gated by the declarative review flow, not by model assertion | `review-security-k8s-agents-prompt-injection` |
| **Data exfiltration** | Default-deny egress; the agent control loop is allowlisted to only what it needs (inference proxy, cloud APIs, GitOps, and required **MCP tool endpoints** for grounding, e.g. `developer_knowledge`/`gke`); untrusted code runs air-gapped | `review-security-k8s-agents-data-exfil`, `-firewall` |
| **Credential compromise** | No long-lived static creds; short-lived brokered tokens via the **GitHub Token Broker (Minty)** using GCP KMS + Workload Identity (`SOUL.md §8`); cloud identity via Workload Identity, not keys | `review-security-k8s-agents-credentials` |
| **Untrusted code execution** | Execution sandbox with a VM-based `RuntimeClass` (gVisor / Kata) — the `DeploymentSpec.RuntimeClassName` field exists for this; separate the allowlisted control loop from the air-gapped execution sandbox | `review-security-k8s-agents-sandbox` |
| **Insufficient attribution** | Trace/session IDs + authenticated requester carried through telemetry and audit records | `review-security-k8s-agents-audit-logs`, `docs/designs/audit-logging-user-attribution.md` |

**Control-loop vs. execution-sandbox split.** A recurring pattern in the review suite: the agent's
reasoning/control loop is strictly allowlisted (e.g. can reach only the inference API and its
scoped cloud/GitOps endpoints), while any untrusted or model-generated code executes in a separate,
air-gapped, VM-isolated sandbox. Keeping these apart limits both exfil and escape blast radius.

---

## 6. The security-review suite as a continuous control

The `.agents/skills/review-security-k8s-*` suite is not just documentation — it is the **audit
mechanism** for this model. Two orchestrators:

- **`review-security-k8s-main`** — general Kubernetes posture: `rbac`, `nodes`, `network`,
  `gateway`, `namespaces`, `service-accounts`, `storage`, `admission`, `pod` (context from
  `review-security-k8s-understand`).
- **`review-security-k8s-agents-main`** — AI-agent posture: `sandbox`, `firewall`, `credentials`,
  `prompt-injection`, `data-exfil`, `audit-logs`.

Both fan out to sub-reviewers, then **triage findings against project context** (filtering
mitigated/required ones) into a single report.

**Design intent:** this suite runs as a gate in the workflow ([04](04-workflow-model.md)) — e.g.
on changes to agent configs, CRDs, or infrastructure PRs — so the security model is enforced
continuously (shift-left), not just at design time. Exactly _where_ it gates is a workflow decision
([04](04-workflow-model.md) §3).

---

## 7. Declarative-only mutation as a security property

Beyond operational hygiene, the declarative-only rule (`SOUL.md §1`, §4) is a **security control**:

- **Reviewable** — every change is a diff a human or the review suite can inspect before it lands.
- **Attributable** — changes are tied to an authenticated requester and trace/session
  (`docs/designs/audit-logging-user-attribution.md`).
- **Revertible** — GitOps state can be rolled back; direct mutations cannot be as cleanly.
- **Constrained** — the operator reconciles only what the CRDs permit, bounding what any agent can
  effect even if its reasoning is subverted.

This is why "agents propose, the system reconciles" is a safety property, not just a workflow
preference. The mechanics live in [04](04-workflow-model.md).

---

## 8. Defense in depth (summary)

| Layer | Control |
|-------|---------|
| Identity | Per-tier ServiceAccount + Workload Identity, least-privilege cloud SA |
| Authorization | Read-only RBAC scoped to project/cluster/namespace; writes only via reconcilers; downward attenuation |
| Network | Default-deny NetworkPolicy; allowlisted egress; control-loop/sandbox split |
| Runtime | VM-based `RuntimeClass` sandbox for untrusted code |
| Secrets | Brokered short-lived tokens (Minty + KMS), no static creds |
| Change | Declarative-only, reviewed, attributable, revertible |
| Assurance | Continuous security-review suite; audit logging & attribution |

## 9. Goals & non-goals

### Goals

- Make the persona boundaries of [02](02-agent-personas.md) enforced and provable.
- Defend against both isolation threats and AI-agent-specific threats.
- Keep privilege downward-only, enforced **in depth** (review-gate + `ValidatingAdmissionPolicy` +
  operator validating webhook), so no agent can escalate itself or a child. The operator validates
  the ceiling but never grants RBAC.
- Treat all model output and external input as untrusted.
- Use the existing review suite as a continuous, shift-left control.

### Non-goals

- Cryptographic non-repudiation of human identity (per the audit design doc).
- Defending against a malicious operator/cluster-admin _human_ with legitimate break-glass — that
  is governance, not this model (though it remains audited).
- Specifying the exact CI/gate wiring — that is [04](04-workflow-model.md).
- Locking to GCP primitives; controls are expressed in portable K8s terms where possible, with
  GKE/Workload-Identity/KMS as the first implementation (cf. [01](01-vision-scope.md) §6 delta).

## 10. Open questions

_Resolved since drafting:_ **prompt-injection hard controls** are now specified in
[04](04-workflow-model.md) §2.2 — the mandatory human-approval gate classes (destructive,
cross-scope, project-level, security-flagged), applied regardless of agent confidence.

- **Operator-minted identity scope** — _resolved:_ **operator-derived from `tier` + `scope`**. The
  agent CR declares only its tier and `ScopeSpec`; the operator generates the read-only
  SA/Role(ClusterRole)/binding + read-only cloud SA. `SecuritySpec` is **not** extended with RBAC
  fields, so a CR cannot request write or extra scope (see [06](06-api-and-data-contracts.md) §2).
- **Attenuation enforcement point** — _resolved (revisited 2026-07-21):_ **defense in depth, all in
  v1.** RBAC is applied only by **Config Sync** from reviewed PRs; the operator never mints RBAC and
  holds no `escalate`/`bind`. Three enforcement layers: (1) the **review-gate** blocks write /
  over-scope grants shift-left; (2) a **`ValidatingAdmissionPolicy`** denies agent-SA write verbs and
  wrong-scope bindings at apply time; (3) the **operator's validating webhook** enforces the child ⊆
  parent ceiling using CRD lineage. The operator **validates**, never **grants**. (Supersedes the
  earlier "review-gate only, admission deferred" resolution.)
- **Egress allowlist definition** — _resolved:_ **per-tier default-deny NetworkPolicy** (v1),
  allowing only required endpoints: the inference proxy, cloud APIs, GitHub (via Minty), and
  **the MCP tool endpoints agents ground on** (e.g. `developer_knowledge`, `gke`). The allowlist must
  never omit MCP endpoints needed for grounding on live documentation. (mem0 is deferred post-v1 — add
  its endpoint to the allowlist only if/when mem0 is introduced.) An L7 egress proxy for
  hostname-precise allowlisting is deferred to Phase 5 hardening.
- **Multi-tenant inference** — _resolved:_ shared LiteLLM proxy with **per-tier/per-tenant virtual
  keys** (own budget, rate-limit, scoped logging); physically separate proxies only if data
  sensitivity later requires it.
- **Break-glass governance** — _resolved:_ no formal break-glass in the initial version; all changes
  go through GitOps ([01](01-vision-scope.md) §8). A governed path is deferred.
