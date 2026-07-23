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
   and read-only RBAC, plus a cloud identity via Workload Identity **where the tier calls GCP APIs**)
   confined to its scope: project / cluster / namespace.
2. **Downward-only privilege attenuation** — a parent can only cause a child to be granted a
   _strict subset_ of scope: the review-gate blocks over-grants shift-left and an in-tree
   **`ValidatingAdmissionPolicy`** rejects them at apply time; the CI/CD pipeline is the sole applier
   and nothing grants RBAC at runtime, so neither a compromised parent nor a bad merge can over-grant.
   (The cross-object child ⊆ parent webhook is deferred hardening, §4.)
3. **AI-agent-specific defenses** — prompt-injection resistance, egress/data-exfil control,
   brokered short-lived credentials, and sandboxed code execution.
4. **Declarative-only mutation** — every change is a reviewable, attributable, revertible artifact,
   never a direct cluster write. There is **no break-glass** — no agent path and no sanctioned human
   direct-write path; every change, including emergencies, goes through the reviewed GitOps loop.
5. **Trusted-human access & a read-only ceiling** — the human→agent boundary is controlled by _who
   may reach an agent at all_ (authenticated chat, `AllowedUsers`, per-audience entrypoints): only
   **trusted humans** get access. The agent's ceiling is its **read-only, tier-scoped** identity, so
   no human can drive it to mutate (read-only + PR gate) or read outside its tier. v1 does **not**
   check the requester's own permissions or union/down-scope the agent to them — that user-scoped
   authorization is **deferred hardening** (§4a, [08](08-agent-runtime-and-identity.md) §5).

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
the agent's identity, not the user's). In v1 this is bounded by **limiting agent access to trusted
humans** plus the **read-only agent ceiling** (§4a); per-request down-scoping to the user is deferred
hardening ([08](08-agent-runtime-and-identity.md) §5).

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

| Boundary                          | Who ↔ who                                            | Primary risk                                                             | Primary control                                                                                                                                                                             |
| --------------------------------- | ---------------------------------------------------- | ------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Human → Agent                     | Authenticated user → agent chat                      | Impersonation, unauthorized intent                                       | Authenticated chat (`AllowedUsers`), per-audience entrypoints ([02](02-agent-personas.md))                                                                                                  |
| Human → Agent action (delegation) | Requester's authority → agent acting on their behalf | **Confused deputy** — a trusted human drives the agent beyond their own permissions | **v1:** trusted-human access (row above) + the **read-only agent ceiling** (§3) — no mutation, no reads outside tier. Per-request down-scoping to the user is **deferred** (§4a, [08](08-agent-runtime-and-identity.md) §5) |
| Agent → Agent (tier)              | Parent ↔ child across tiers                          | Privilege escalation up the cascade                                      | Scoped identity + downward attenuation (§3, §4)                                                                                                                                             |
| Agent → Kubernetes API            | Agent SA → cluster API                               | Acting outside scope                                                     | **Read-only** RBAC scoped to tier; NetworkPolicy; admission (§4)                                                                                                                            |
| Agent → Cloud APIs                | Workload Identity → cloud SA                         | Broad cloud blast radius                                                 | **Read-only**, per-tier cloud SA, least-privilege IAM                                                                                                                                       |
| Agent → LLM / inference           | Agent → LiteLLM/vLLM proxy                           | Prompt injection, data leak in prompts                                   | Allowlisted egress to inference only; input treated as untrusted (§5)                                                                                                                       |
| Agent → External input            | Chat / issues / cluster data / tool output           | Prompt injection, exfil trigger                                          | Untrusted-input handling, egress control, audit (§5)                                                                                                                                        |
| Agent → Git / GitOps              | Agent → repo                                         | Credential theft, malicious change                                       | Brokered short-lived tokens (Minty), PR review gate (§5, §7)                                                                                                                                |

---

## 3. Identity & least privilege per tier

Each persona receives an identity confined to exactly its scope. This is what turns
[02](02-agent-personas.md)'s "provably unable to escalate" into an enforced property.

Every agent is **read-only** on the cluster and cloud APIs — the only thing an agent can change is
the GitOps repo (via a brokered token). Scope defines what it can _read_ and what it can _propose_.

**Agent identity.** Each agent runs as its own **Kubernetes ServiceAccount**; agents that call GCP
APIs additionally bind a **read-only GCP service account via Workload Identity** (an agent that only
reads the K8s API needs no cloud SA — Workload Identity is used _where it makes sense_, not
universally). **This per-agent read-only, tier-scoped identity is the ceiling — full stop.** In v1 it
is not narrowed per requester; access is instead limited to trusted humans (§4a). Per-request
down-scoping to the requesting human is deferred hardening (§4a, [08](08-agent-runtime-and-identity.md)
§5).

Exactly **one agent runs per scope** — 1 Platform Agent per **project**, 1 Cluster Admin Agent per
**cluster**, 1 Developer Team Agent per **namespace** — and each is read-only within **exactly its own
level**:

| Tier                     | Kubernetes API (read-only)             | Cloud API (read-only)     | Only write path                      | May NOT                                                                    |
| ------------------------ | -------------------------------------- | ------------------------- | ------------------------------------ | ------------------------------------------------------------------------- |
| **Platform Agent** (1/project) | Read within **its one project** (the project's clusters/fleet) | Project-scoped read | GitOps repo (PRs) via brokered token | Any direct cluster/cloud write; operate tenant workloads; **any other project** |
| **Cluster Admin Agent** (1/cluster) | Read **its one cluster only**   | Cluster-scoped read       | GitOps repo (PRs)                    | Any direct write; **any other cluster**; project scope                    |
| **Developer Team Agent** (1/namespace) | Read **its one namespace only** | Namespace-scoped read   | GitOps repo (PRs)                    | Any direct write; **any other namespace**; cluster/project scope          |

**The controller enforces this ceiling.** For each `Agent` CR, the kube-agents controller sets the pod's
`serviceAccountName` to exactly this SA ([08](08-agent-runtime-and-identity.md)), and the SA's RBAC +
Workload-Identity binding are pre-created read-only and scoped to the tier's level. So the read scope is
enforced by **Kubernetes RBAC + IAM**, not by agent goodwill: a **Developer Team Agent's pod cannot read
another namespace**, a **Cluster Admin Agent's cannot reach another cluster**, and a **Platform Agent's
cannot reach another project**.

**Agents hold no write RBAC on the cluster or cloud.** The actual tenant/cloud writes are performed by
the **actuation pipeline** (the customer's CI/CD — GitHub Actions, CircleCI, …) acting only on reviewed,
merged state; the **kube-agents controller** holds only the narrow write it needs to create/update
**agent pods** (Deployments) in `kubeagents-system` from reviewed `Agent` CRs — it never writes tenant
or cloud resources ([04](04-workflow-model.md) §1.1, [08](08-agent-runtime-and-identity.md)). Even
provisioning a lower-tier agent is a read-only agent proposing a change to the repo, applied by the
pipeline.

Today the operator's `SecuritySpec` carries only `ServiceAccountName` + `ServiceAccountAnnotations` (for
Workload Identity binding), the operator still mints a `view` binding + an "explorer" ClusterRole, and
agents hold direct-mutation tools (the remote `gke` MCP's `create_cluster`). **End state:** per tier, a
**read-only** ServiceAccount/Role scoped to project/cluster/namespace plus a read-only cloud SA mapping
is rendered from `tier` + `scope` and **applied by the CI/CD pipeline** — the controller mints **no**
RBAC at runtime (§4, [06](06-api-and-data-contracts.md) §2) — and agents lose all direct-mutation tools.
An agent's scope is thus a **reviewed** read ceiling, and all write authority lives in the pipeline.
Identity derives from the CR's `tier` + `scope` alone; the `Agent` CRD carries **no** RBAC/scope-granting
fields, so a CR cannot request write or extra scope.

---

## 4. Enforcing containment (the load-bearing isolation)

The persona hierarchy is only as strong as the mechanisms that pin each agent to its scope.

**Kubernetes-native isolation.** Namespace boundaries, RBAC (`Role`/`ClusterRole` scoped as in §3),
`NetworkPolicy` (default-deny + explicit allows, cf.
`agents/platform/skills/gke-workload-security/assets/default-deny-netpol.yaml`), `ResourceQuota`,
and admission control together enforce that an agent cannot read or mutate outside its scope.

**Downward-only privilege attenuation (key invariant).** When a parent provisions a child
([02](02-agent-personas.md) §6), it proposes a declarative bundle as a PR — the child `Agent` CR
**plus** the child's read-only RBAC (SA/Role/RoleBinding), rendered from the child's `tier` + `scope`
via a kustomize overlay. **The CI/CD pipeline applies it after human review**; the controller mints no
RBAC at runtime. Consequences:

- A parent can only ever cause a child to receive a _strict subset_ of **read** scope — the render
  overlay emits read-only RBAC, and the **review-gate blocks any RBAC granting an agent SA write verbs**
  (shift-left).
- No agent can widen its own scope: the RBAC that grants access is a reviewed artifact in the repo,
  not something an agent can author unilaterally or any runtime component can over-grant.
- **Enforcement (v1):** the shift-left gate plus one runtime backstop that rejects a violating grant
  **at apply time** — even if a bad RBAC PR merges:
  - a **`ValidatingAdmissionPolicy`** (in-tree CEL) hard-denies any `Role`/`ClusterRole` whose `rules`
    give an **agent ServiceAccount** a write verb (`create/update/patch/delete/*`), or a cluster-scoped
    grant to a namespace-tier agent. It selects agent RBAC by the **convention the controller stamps** —
    agent SAs live in `kubeagents-system` (or the team namespace), are named `*-agent`, and carry the
    `kube-agents/tier` label — so the policy's `matchConditions` can key on them. v1 scopes the CEL to a
    role's own `rules` (a self-contained check); write-via-referenced-`ClusterRole` and the cross-object
    ceiling below need the webhook.
- **Deferred hardening:** the cross-object _ceiling_ — a child's scope must be ⊆ its parent's — needs a
  validating admission webhook (pure CEL can't express it cross-object). The **kube-agents controller is
  its natural host** (it already runs a webhook server for `(tier,scope)` cardinality); deferred to
  [08](08-agent-runtime-and-identity.md) §5 for effort, not for lack of a host.
- **Nothing grants RBAC at runtime.** The KSA/RBAC are ordinary manifests; the sole _applier_ is the
  **CI/CD pipeline** acting on reviewed, merged state, and the **controller** only references the
  resulting KSA by name ([08](08-agent-runtime-and-identity.md)).

**The actuation pipeline is the privileged writer.** Since agents are read-only, the customer's CI/CD
pipeline holds the scoped credentials that actually apply changes to the cluster and cloud — so it is
a high-value asset. It must act **only on reviewed, merged state**, use **least-privilege deploy
credentials** scoped per environment/target, and emit **audited** run records. Runtime admission (the
in-tree `ValidatingAdmissionPolicy`) still backstops any Kubernetes RBAC the pipeline applies —
admission runs regardless of _who_ applies. Cloud resources applied via Terraform are outside K8s
admission; there the review-gate plus the pipeline's scoped credentials are the controls.
- Because agents are **read-only** (§3), a subverted agent has no write path to abuse in the first
  place; identity itself is reviewable and revertible like any other config.

**No break-glass.** There is no agent path across a scope boundary, and **no sanctioned human
break-glass** either — every change, including emergencies, goes through human-approved GitOps.
Break-glass is deliberately **not part of the design** (kept out for simplicity), not a deferral
([01](01-vision-scope.md) §2).

---

## 4a. Human → agent authorization (v1: trusted humans + a read-only ceiling)

**v1 model.** The human→agent boundary is controlled by _who may reach an agent at all_: authenticated
chat with an explicit allowlist (`AllowedUsers`) and a per-audience entrypoint (§2,
[02](02-agent-personas.md)). Only **trusted humans** get access. Once in, a human can only ever get
the agent to do what the **agent itself** can do — and the agent's ceiling is its **read-only,
tier-scoped identity** (§3) plus proposing human-merged PRs. So **no human — trusted or not — can
drive an agent to mutate anything** (read-only + the PR/merge gate) **or read outside its tier scope.**

**What v1 deliberately does _not_ do.** v1 does **not** verify the requester's own GCP/K8s permissions
and does **not** union/intersect them with the agent's SA. A trusted human with narrow personal
permissions can still use the agent to read anything within the agent's tier scope. This is an
accepted trade for the simplest first model: **security on this boundary is "only trusted humans get
access," and the agent ceiling is the read-only scope — full stop.** The **confused deputy** (§1) is
bounded by access control + read-only, not eliminated per request.

**Deferred hardening — user-scoped authorization ([08](08-agent-runtime-and-identity.md) §5).** When
access must extend beyond fully-trusted humans, add the delegate model: authorize each request against
the requester's own identity (`SubjectAccessReview` for K8s, `testIamPermissions` / Policy
Troubleshooter for GCP) and down-scope the agent's effective authority to **agent scope ∩ requester
permissions** (closing the confused-deputy gap), enforced by an authorization gateway/broker **outside
the LLM loop** with per-run downscoped tokens. Contract sketch: [06](06-api-and-data-contracts.md)
§2a. **Not in v1.**

---

## 5. AI-agent-specific defenses

These map directly onto the existing agent security-review sub-skills
(`.agents/skills/review-security-k8s-agents-*`), which define what "good" looks like and audit for
it.

| Threat                       | Defense (end state)                                                                                                                                                                                                                         | Review skill                                                                              |
| ---------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------- |
| **Prompt injection**         | Treat all external input (chat, cluster data, tool output, issues) as untrusted data, never instructions; model output is never an authz signal; sensitive actions gated by the declarative review flow, not by model assertion             | `review-security-k8s-agents-prompt-injection`                                             |
| **Data exfiltration**        | Default-deny egress; the agent control loop is allowlisted to only what it needs (inference proxy, cloud APIs, GitOps, and required **MCP tool endpoints** for grounding, e.g. `developer_knowledge`/`gke`); untrusted code runs air-gapped | `review-security-k8s-agents-data-exfil`, `-firewall`                                      |
| **Credential compromise**    | No long-lived static creds; short-lived brokered tokens via the **GitHub Token Broker (Minty)** using GCP KMS + Workload Identity (`SOUL.md §8`); cloud identity via Workload Identity, not keys                                            | `review-security-k8s-agents-credentials`                                                  |
| **Untrusted code execution** | Execution sandbox with a VM-based `RuntimeClass` (gVisor / Kata) — the `DeploymentSpec.RuntimeClassName` field exists for this; separate the allowlisted control loop from the air-gapped execution sandbox                                 | `review-security-k8s-agents-sandbox`                                                      |
| **Insufficient attribution** | Trace/session IDs + authenticated requester carried through telemetry and audit records                                                                                                                                                     | `review-security-k8s-agents-audit-logs`, `docs/designs/audit-logging-user-attribution.md` |

**Control-loop vs. execution-sandbox split.** A recurring pattern in the review suite: the agent's
reasoning/control loop is strictly allowlisted (e.g. can reach only the inference API and its
scoped cloud/GitOps endpoints), while any untrusted or model-generated code executes in a separate,
air-gapped, VM-isolated sandbox. Keeping these apart limits both exfil and escape blast radius. (The
same principle is what would place **user-scoped authorization** in a gateway _outside_ the control
loop — the deferred hardening in §4a; not v1.)

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
- **Constrained** — the CI/CD pipeline applies only reviewed, merged manifests, bounding what any agent
  can effect even if its reasoning is subverted.

This is why "agents propose, the system reconciles" is a safety property, not just a workflow
preference. The mechanics live in [04](04-workflow-model.md).

---

## 8. Defense in depth (summary)

| Layer         | Control                                                                                                                                                                         |
| ------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Identity      | Per-tier ServiceAccount + Workload Identity, least-privilege cloud SA                                                                                                           |
| Authorization | Read-only RBAC scoped to project/cluster/namespace; writes only via the CI/CD pipeline; downward attenuation                                                                     |
| Human→agent   | **Trusted-human access** (authenticated chat + `AllowedUsers`) + the **read-only agent ceiling** — a trusted human can't drive the agent to mutate or read outside its tier (§4a). Per-request down-scoping deferred ([08](08-agent-runtime-and-identity.md) §5) |
| Network       | Default-deny NetworkPolicy; allowlisted egress; control-loop/sandbox split                                                                                                      |
| Runtime       | VM-based `RuntimeClass` sandbox for untrusted code                                                                                                                              |
| Secrets       | Brokered short-lived tokens (Minty + KMS), no static creds                                                                                                                      |
| Change        | Declarative-only, reviewed, attributable, revertible                                                                                                                            |
| Assurance     | Continuous security-review suite; audit logging & attribution                                                                                                                   |

## 9. Goals & non-goals

### Goals

- Make the persona boundaries of [02](02-agent-personas.md) enforced and provable.
- Defend against both isolation threats and AI-agent-specific threats.
- Keep privilege downward-only, enforced by the review-gate + an in-tree `ValidatingAdmissionPolicy`
  (the cross-object child ⊆ parent webhook is deferred hardening), so no agent can escalate itself or a
  child. Nothing grants RBAC at runtime.
- Keep the human→agent boundary simple in v1: **only trusted humans get access**, and the **agent
  ceiling is its read-only, tier-scoped identity** — so no human can drive it to mutate or read
  outside its tier. (Per-request down-scoping to the requester — the delegate model — is deferred
  hardening, [08](08-agent-runtime-and-identity.md) §5.)
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

## 11. Verification

The load-bearing security properties are checked with concrete, mostly-**negative** tests; the harness
iterates until all pass:

- **Read-only, per tier (SAR):** for each agent SA, `kubectl auth can-i create|update|delete <res> --as=<agent-sa>`
  returns **no** for every resource; `get|list|watch` returns **yes** only within its tier scope. A
  Developer Team SA returns **no** for reads in any other namespace; a Cluster Admin SA **no** for any
  other cluster; a Platform SA **no** for any other project.
- **No write tools:** no write-capable MCP tool reaches the agent — no cluster-creating tool
  (`create_cluster` not exposed), the `gke` MCP is read-only, and the `platform_mcp_server.py`
  `apply_manifest` / `delete_cluster_manifest` helpers are removed — grep the config / MCP wiring.
- **Attenuation admission:** applying a `Role`/`ClusterRole` whose rules grant an agent SA a write verb,
  or a cluster-scoped binding to a namespace-tier SA, is **rejected** by the `ValidatingAdmissionPolicy`
  (apply the bad manifest to the Phase-0 test cluster — Kind or a scratch GKE cluster,
  [07](07-implementation-roadmap.md) §2; expect denial).
- **No break-glass:** there is no non-GitOps write path — a direct `kubectl apply` / cloud write with
  an agent identity is **forbidden**; the only successful mutation is a merged PR actuated by CI/CD.
- **Trusted-human access:** an unauthenticated or non-`AllowedUsers` request to an agent entrypoint is
  **refused**.
- **Egress default-deny:** from an agent pod, only allowlisted endpoints (inference, cloud APIs,
  GitHub, MCP grounding) are reachable; the cloud metadata server and arbitrary hosts are **not**.
