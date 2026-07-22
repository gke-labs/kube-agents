# Design 03: Security & Trust Model

**Status:** ✅ Agreed — started 2026-07-21

**Charter:** [README.md](README.md) · **Depends on:** [01-vision-scope.md](01-vision-scope.md),
[02-agent-personas.md](02-agent-personas.md) · **Feeds:** [04-workflow-model.md](04-workflow-model.md)

---

## TL;DR

The security model makes the persona boundaries from [02](02-agent-personas.md) **provable rather
than aspirational**, and defends against the threats unique to autonomous AI agents operating
infrastructure. It rests on five pillars:

1. **Scoped identity & least privilege** — each agent tier gets its own identity (a K8s ServiceAccount
   + read-only RBAC, plus a cloud identity via Workload Identity **where the tier calls GCP APIs**)
   confined to its scope: project / cluster / namespace.
2. **Downward-only privilege attenuation** — a parent can only cause a child to be granted a
   _strict subset_ of scope, enforced **in depth**: the review-gate blocks over-grants shift-left, a
   runtime admission policy and the operator's **validating** webhook reject them at apply time, and
   Config Sync is the sole applier. The operator **validates** the ceiling but never **grants** it
   (holds no `escalate`/`bind`), so neither a compromised parent nor a bad merge can over-grant.
3. **AI-agent-specific defenses** — prompt-injection resistance, egress/data-exfil control,
   brokered short-lived credentials, and sandboxed code execution.
4. **Declarative-only mutation** — every change is a reviewable, attributable, revertible artifact,
   never a direct cluster write. There is **no break-glass** — no agent path and no sanctioned human
   direct-write path; every change, including emergencies, goes through the reviewed GitOps loop.
5. **User-scoped authorization (delegate, not amplifier)** — the agent acts only within the
   intersection of its own tier scope **and the requesting human's own GCP + Kubernetes permissions**.
   Every user-driven read or proposal is authorized against the requester's identity
   (`SubjectAccessReview` for K8s, IAM checks for GCP) and down-scoped to them, so a human can never use
   an agent to exceed what they could do themselves (no **confused deputy**). See §4a.

The existing `.agents/skills/review-security-k8s-*` suite is the **continuous control** that audits
conformance to this model.

---

## 1. What we're defending against

Two distinct threat classes, both in scope:

**A. Boundary / isolation threats** — an agent (or a tenant, or a compromised workload) acting
outside its scope: a Developer Team Agent reading another namespace, a Cluster Admin Agent reaching
another cluster, privilege escalation up the hierarchy, or lateral movement between tenants. A
distinct sub-case is the **confused deputy** — a low-privilege human using a higher-privilege agent to
read or change something they themselves are not permitted to (absent a check, the API only ever sees
the agent's identity, not the user's). Addressed by user-scoped authorization (§4a).

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
| Human → Agent action (delegation) | Requester's authority → agent acting on their behalf | **Confused deputy** — user drives the agent beyond their own permissions | **User-scoped authorization** (§4a): each request checked against the requester's K8s (`SubjectAccessReview`) + GCP (IAM) permissions; effective authority = agent scope ∩ user permissions |
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

**Agent identity.** Each agent runs as its own **Kubernetes ServiceAccount**; agents that call GCP
APIs additionally bind a **read-only GCP service account via Workload Identity** (an agent that only
reads the K8s API needs no cloud SA — Workload Identity is used _where it makes sense_, not
universally). This per-agent identity is the **ceiling**; the actual authority of any user-driven
action is further **down-scoped to the requesting human** (§4a) — the effective authority is the
_intersection_ of the two.

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
(the `create_cluster` MCP tool, a `gke` MCP server). **End state:** per tier, a **read-only**
ServiceAccount/Role scoped to project/cluster/namespace plus a read-only cloud SA mapping is rendered
from `tier` + `scope` and **applied by Config Sync** — not minted by the operator (§4,
[06](06-api-and-data-contracts.md) §2) — and agents lose all direct-mutation tools. An agent's scope
is thus a **reviewed** read ceiling, and all write authority lives in the reconcilers. Identity
derives from `tier` + `ScopeSpec` alone; `SecuritySpec` gains no RBAC/scope fields, so a CR cannot
request write or extra scope.

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

**No break-glass.** There is no agent path across a scope boundary, and **no sanctioned human
break-glass** either — every change, including emergencies, goes through human-approved GitOps.
Break-glass is deliberately **not part of the design** (kept out for simplicity), not a deferral
([01](01-vision-scope.md) §2).

---

## 4a. User-scoped authorization (the agent is a delegate, not a privilege amplifier)

Scoped agent identity (§3) bounds what an agent _can_ touch. It does **not**, by itself, stop a
**confused deputy**: without an extra check, a low-privilege human could ask a high-privilege agent to
read or propose something they are not entitled to, and the API would only ever see the agent's
identity. The control that closes this: **every user-driven action is authorized against the
requesting human's own identity, and the agent's effective authority is down-scoped to that user.**

**Invariant:** for a request from user _U_, effective authority = **(agent tier scope) ∩ (U's own GCP
+ Kubernetes permissions)**. The agent may never read or propose anything _U_ could not read or propose
themselves.

**Two identities, always distinct:**

- **Agent identity** — the agent's own K8s ServiceAccount (+ Workload-Identity cloud SA where needed,
  §3). Used to _perform_ reads and to _propose_ via the brokered token.
- **Requester identity** — the authenticated human (their Google/GCP identity and mapped K8s
  user/groups), carried on the session (`docs/designs/audit-logging-user-attribution.md`). Used to
  _authorize_ the request. Model output is never an authorization signal (§1).

**How the check works (check-then-act, no impersonation):**

- **Kubernetes:** before a read or a proposal, verify the requester via a **`SubjectAccessReview`** —
  "can `user`/`groups` do `verb` on `resource` in `namespace`?" The agent's SA holds only the
  delegated-authz permission to _create_ `SubjectAccessReviews` (the standard `system:auth-delegator`
  pattern); it **checks** the user's rights, it does not impersonate them.
- **GCP:** verify the requester holds the needed permissions on the target resource/project via **IAM**
  (`testIamPermissions` / Policy Troubleshooter) evaluated for the user's principal.
- **Decision:** proceed only if the requester is authorized; otherwise the agent refuses and explains,
  attributed to the requester. The result bounds **both** reads (return only what _U_ may see —
  stopping data disclosure via the deputy) **and** proposals (never author a change _U_ couldn't make;
  the PR is attributed to _U_ and still faces the human-merge gate, §7 / [04](04-workflow-model.md)).

**Enforcement points (defense in depth — the agent is not trusted as the sole gate):**

- **Authoritative — outside the LLM loop:** a **policy-enforcing gateway / scoped data-access layer**
  in front of the agent performs the requester's `SubjectAccessReview` / IAM check and filters reads,
  so the down-scoping holds even if the agent's reasoning is subverted (consistent with the
  control-loop/sandbox split, §5). This is the trusted enforcer.
- **Shift-left — in the agent:** the agent also pre-checks and refuses early, for fast feedback and to
  bound its proposals; never the sole gate.
- **At merge:** the existing human-approval gate remains ([04](04-workflow-model.md) §2–3).

**Why not impersonate the user's credentials?** Holding user tokens would enlarge the credential
surface and the prompt-injection blast radius (a subverted agent holding user creds is far worse).
Check-then-act keeps the agent read-only and credential-light while still enforcing the user's ceiling.
Platform-enforced impersonation (K8s user impersonation / GCP token exchange) is a stronger _reads_
option that can be layered later, at that credential-surface cost.

The mechanism contract (SAR shape, IAM check, requester propagation, agent-SA grant) is in
[06](06-api-and-data-contracts.md) §2a; the loop placement is in [04](04-workflow-model.md) §1–2.

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
air-gapped, VM-isolated sandbox. Keeping these apart limits both exfil and escape blast radius. The
same principle places **user-scoped authorization** (§4a) in a gateway _outside_ the control loop, so
a subverted agent still cannot exceed the requesting user's own permissions.

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
| Delegation | **User-scoped authorization**: every request bounded by the requester's own K8s (`SubjectAccessReview`) + GCP (IAM) permissions; effective authority = agent scope ∩ user (§4a) |
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
- Bound every user-driven action to the requesting human's own permissions (agent as **delegate**, not
  privilege amplifier) — no confused deputy.
- Treat all model output and external input as untrusted.
- Use the existing review suite as a continuous, shift-left control.

### Non-goals

- Cryptographic non-repudiation of human identity (per the audit design doc).
- Defending against a malicious operator/cluster-admin _human_ with legitimate cluster credentials
  outside this system — that is governance, not this model (though such access remains audited).
- Specifying the exact CI/gate wiring — that is [04](04-workflow-model.md).
- Locking to GCP primitives; controls are expressed in portable K8s terms where possible, with
  GKE/Workload-Identity/KMS as the first implementation (cf. [01](01-vision-scope.md) §6 delta).

## 10. Egress allowlist & inference isolation (specifics)

Two details that the trust-boundary and defense tables above rely on:

- **Egress:** a **per-tier default-deny NetworkPolicy** allows only the inference proxy, cloud APIs,
  GitHub (via Minty), and the MCP tool endpoints agents ground on (e.g. `developer_knowledge`, `gke`).
  The allowlist must never omit MCP endpoints needed for grounding on live docs. An L7 egress proxy
  for hostname-precise allowlisting is a Phase 5 hardening item ([07](07-implementation-roadmap.md)).
- **Multi-tenant inference:** a shared LiteLLM proxy with **per-tier/per-tenant virtual keys** (own
  budget, rate-limit, scoped logging); physically separate proxies only if data sensitivity later
  requires it.
