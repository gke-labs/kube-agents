# Design 04: Workflow Model

**Status:** ✅ Agreed — started 2026-07-21

**Charter:** [README.md](README.md) · **Depends on:** [01-vision-scope.md](01-vision-scope.md),
[02-agent-personas.md](02-agent-personas.md), [03-security-model.md](03-security-model.md)

---

## TL;DR

Every change in `kube-agents` follows one loop: **an agent proposes a declarative change → a human
approves it (PR merge) → the operator reconciles it into reality.** Agents never mutate
infrastructure directly, and **no change reaches a cluster without a human approving the merge —
there is no auto-merge for any tier.** "Autonomy" governs how the agent **proposes**, not whether a
human approves: the agent authors and opens PRs proactively for reversible, in-scope work; for
destructive, irreversible, cross-scope, or high-sensitivity actions it additionally **halts and flags
for the specific tier authority** (§2.2) — regardless of the agent's confidence. Either way, a human
merges.

Proactivity is driven by a **heartbeat**: scheduled audits detect drift and propose fixes through
the same loop. Blockers are handled by a bounded **recovery ladder** before any human escalation.
Because each agent is an independent, operator-reconciled deployment, tiers **fail in isolation**,
not in cascade.

This doc resolves the deferrals from [02](02-agent-personas.md) (approval authority, failure
isolation) and [03](03-security-model.md) (where the review suite gates, prompt-injection hard
gates).

---

## 1. The core loop: propose → review → reconcile

```
Intent (human chat, heartbeat, or escalation)
        │
        ▼
  AUTHORIZE the requester (human-initiated only)  ← check the human's OWN GCP + K8s permissions
   K8s SubjectAccessReview + GCP IAM;                (§2.4, [03] §4a); gateway-enforced, outside
   deny if unauthorized; reads/proposals             the LLM loop. (Heartbeat/escalation intents
   down-scoped to the user                           have no human requester — agent's own scope.)
        │
        ▼
  Agent authors a DECLARATIVE change     ← never a direct kubectl/console mutation
   (manifest / CR / policy) on a branch     (bounded by the requester's authority)
        │  via `submit-suggestion` (GitHub PR) or the environment's GitOps mechanism
        ▼
  REVIEW gate                            ← human approval and/or security-review suite (§3)
        │
        ▼
  Reconcilers apply merged state → actual  ← Config Sync / Config Connector / operator (§1.1)
        │                                     agents are READ-ONLY; only reconcilers write
        ▼
  Outcome reported back (human-readable) + audited (trace/session/requester)
```

This is mandated by `SOUL.md §1, §4`: agents are "strictly forbidden from executing direct, manual
cluster mutations." The `submit-suggestion` skill is the reference implementation of the "propose"
step (branch → stage _only_ targeted files → commit → PR); other environments may use Config
Connector, Argo/Flux, or a pipeline — the shape is the same.

Why this shape is load-bearing (from [03](03-security-model.md) §7): declarative changes are
**reviewable, attributable, revertible, and constrained** — so the workflow is itself a security
control, not just an operational convenience.

### 1.1 Reference implementation stack (GKE-first)

The loop is mechanism-agnostic, but the reference implementation for the GKE-first target is:

| Concern                                              | Mechanism                                                   | Instead of                                                    |
| ---------------------------------------------------- | ----------------------------------------------------------- | ------------------------------------------------------------- |
| Shared source of truth                               | **GitOps repository**                                       | — (agents propose PRs here)                                   |
| Config → cluster reconciliation                      | **Config Sync**                                             | ArgoCD / Flux                                                 |
| Cloud resource provisioning (clusters, IAM, buckets) | **Config Connector**                                        | Terraform / direct API calls                                  |
| Agent lifecycle                                      | **kube-agents operator**                                    | —                                                             |
| Curated shared knowledge                             | **OKF** (markdown+frontmatter in git)                       | ad-hoc wikis / tribal knowledge                               |
| Semantic recall                                      | **mem0** (Qdrant) — _deferred post-v1_                      | —                                                             |
| Session / runtime state                              | **`session_db.sqlite` + `multiuser_memory`**                | —                                                             |
| Cross-agent coordination                             | **shared state, observed on heartbeat** (GitOps repo + OKF) | direct agent-to-agent calls ([02](02-agent-personas.md) §2.3) |

**Agents are read-only** on every cluster and cloud API; write permission lives only in the
reconcilers above, which act solely on reviewed, merged declarative state. Concretely:

- **Cluster provisioning** = a Config Connector `ContainerCluster` CR committed to the repo, applied
  by Config Sync, reconciled to GCP — **not** a direct `create_cluster` API call.
- **Workload / config deployment** = manifests in the repo, applied by **Config Sync** — not
  ArgoCD, not direct `kubectl`.

Cloud-agnostic analogs (cf. [01](01-vision-scope.md) §6) are **Crossplane** for provisioning and
**Argo/Flux** for GitOps; Config Connector + Config Sync are the GKE-first choices. (This replaces
today's direct-mutation path, where agents call `create_cluster` / the `gke` MCP server directly.)

---

## 2. Autonomy vs. human approval

**Every mutation is human-approved at merge — there is no auto-merge for any tier** (the #1
invariant). "Autonomy" is about how the agent _proposes_, never about skipping approval: the default
is **biased toward proposing proactively** for safe work, and **hard-stops that halt-and-flag for a
specific tier authority** for consequential work. This reconciles `SOUL.md`'s "User Intent Priority"
(act — i.e. _propose_ — when the answer would just be "yes") with its "destructive operations always
require confirmation" rule.

### 2.1 Propose autonomously (no pre-ask) when…

- The change is **reversible** and **within the agent's own scope** ([03](03-security-model.md) §3).
- The expected human answer to any clarification would simply be "yes / go ahead" (`SOUL.md §1`).
- The user signaled intent ("fix it", "do it", "loop until done").

Here the agent authors and opens the PR without a clarifying back-and-forth — but the change **still
requires a human merge** and then flows through the declarative loop (§1); the agent reports the
outcome. Autonomy removes the _pre-ask_, never the _approval_.

### 2.2 Stop for explicit human approval when… (mandatory gates)

These gates are **unconditional** — they apply regardless of the agent's confidence, and they are
the answer to [03](03-security-model.md)'s "prompt-injection hard controls" question. Even a
perfectly-reasoned agent (or a subverted one) cannot bypass them:

| Gate class                         | Examples                                                                                                |
| ---------------------------------- | ------------------------------------------------------------------------------------------------------- |
| **Destructive / irreversible**     | Cluster deletion, tenant offboarding, PVC/data deletion, broad IAM/RBAC revocation                      |
| **Cross-scope / privilege change** | Provisioning a lower-tier agent, widening any scope, editing RBAC/identity, changing tenancy boundaries |
| **Project-level blast radius**     | Project-wide config, fleet-wide policy changes, cluster provisioning                                    |
| **Security-sensitive**             | Changes flagged by the security-review suite (§3) as unmitigated findings                               |

The gate is the **review step of the loop** (§1): approval is a human merging/approving the
declarative change, so the gate is auditable and cannot be satisfied by the agent asserting it is
fine.

### 2.3 Approval authority per tier

Who approves depends on the blast radius, aligned to the containment hierarchy:

| Change                                           | Proposed by          | Approved by                                        |
| ------------------------------------------------ | -------------------- | -------------------------------------------------- |
| Workload change in a namespace                   | Developer Team Agent | That team's human owner (PR merge — no auto-merge) |
| Namespace/tenant creation, cluster-scoped config | Cluster Admin Agent  | Cluster administrator (human)                      |
| Provisioning a Developer Team Agent              | Cluster Admin Agent  | Cluster administrator (human)                      |
| Cluster provisioning, fleet policy               | Platform Agent       | Platform team (human)                              |
| Provisioning a Cluster Admin Agent               | Platform Agent       | Platform team (human)                              |

**Rule:** an agent may propose changes to the tier it governs, and **a human always approves the
merge — every change, no exceptions, no auto-merge.** Mandatory-gate classes (§2.2) additionally
require the approver to be the **human owning that tier** (not just any reviewer). Agents never
approve other agents' — or their own — changes; approval authority stays with humans at the
appropriate layer.

### 2.4 Authorize the requester first (user-scoped, mandatory pre-check)

Approval authority (§2.3) governs _who signs off at merge_. A separate, **earlier** check governs
_whether the requesting human was entitled to ask at all_: before the agent reads or authors anything
on a human's behalf, the request is **authorized against that human's own GCP + Kubernetes
permissions**, and the agent's effective authority is **down-scoped to the requester** (agent scope ∩
user permissions). This closes the **confused deputy** — a user cannot drive an agent past their own
access ([03](03-security-model.md) §4a).

- **Mechanism:** K8s `SubjectAccessReview` + GCP IAM check against the requester (check-then-act, no
  impersonation); **authoritatively enforced by a gateway outside the LLM loop**, with an in-agent
  shift-left pre-check. Contract in [06](06-api-and-data-contracts.md) §2a.
- **Scope:** applies to **human-initiated** requests. Heartbeat- and escalation-driven actions have no
  human requester and run under the agent's own read-only scope — still bounded by the mandatory gates
  (§2.2) and the human-merge gate (§2.3).
- **Deny behavior:** if the requester lacks permission, the agent refuses and explains, attributed to
  the requester — it never proceeds "because the agent could."

---

## 3. Where security review gates

The `.agents/skills/review-security-k8s-*` suite ([03](03-security-model.md) §6) runs at two points:

1. **Pre-merge gate (shift-left).** On any PR that touches infrastructure manifests, agent
   configs/`SOUL.md`, CRDs, RBAC, or NetworkPolicies, the appropriate orchestrator runs:
   - `review-security-k8s-main` for general K8s posture,
   - `review-security-k8s-agents-main` for agent-specific posture.
     Unmitigated findings block the merge (a §2.2 security-sensitive gate).
2. **Continuous audit (heartbeat).** The scheduled compliance/standardization audits (§4) re-run
   posture checks against live state to catch drift that bypassed review, and propose remediations
   through the loop.

**Where it runs (decided):** **GitHub Actions on PR** (trigger paths + severity policy in
[06](06-api-and-data-contracts.md) §7) **plus the heartbeat re-run** above. CI is authoritative and
runs **outside** the agent — an in-agent self-check is never the enforcer (an optional agent
pre-check for faster feedback may be added later without changing the trust model). This is the only
gate for the agent-specific threat classes (prompt-injection, data-exfil, credentials) that have no
runtime admission backstop, so it must live in a trust domain the agent cannot rewrite.

---

## 4. Proactive operations: the heartbeat

Agents do not only react. A scheduled heartbeat drives continuous fleet stewardship — the
Platform Agent already ships **10 governance jobs** (`agents/platform/cron/jobs.json`) mapped to
SOPs in `agents/platform/governance/`:

| Cadence      | Jobs (examples)                                                         |
| ------------ | ----------------------------------------------------------------------- |
| Hourly       | Policy propagation, global capacity orchestration                       |
| Every 30 min | GitHub issue resolver                                                   |
| Daily        | Blueprint sync, cost analysis, security patch scan, obtainability audit |
| Weekly       | Compliance audit, standardization validator                             |
| Monthly      | Lifecycle / deprecation manager                                         |

The heartbeat pattern (`INSTALL.md §3`): read the relevant SOP → run due checks → update
heartbeat state → if healthy respond `NO_REPLY`, else surface concise blockers. **Anything the
heartbeat wants to change goes through the propose→review→reconcile loop** (§1), never a direct
mutation.

**End state — per-tier heartbeat (scoped by persona responsibility).** Proactivity exists at every
layer, but each tier stewards **only its own scope**. Fleet-only jobs stay at Platform;
cluster/namespace concerns cascade down as scoped subsets:

| Tier                           | Heartbeat jobs (scoped to its authority)                                                                                                                                                                      | Not run here (owned by a higher tier)                                                                                         |
| ------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------- |
| **Platform** (fleet)           | All 10 governance jobs above                                                                                                                                                                                  | —                                                                                                                             |
| **Cluster Admin** (cluster)    | Cluster capacity / node health; security patch scan (its cluster); compliance audit (cluster-policy conformance); standardization validator (config vs. blueprint); Config Sync drift detection (its cluster) | Policy propagation, lifecycle/deprecation, blueprint sync (authoring), fleet cost, obtainability audit, GitHub issue resolver |
| **Developer Team** (namespace) | Workload health / reliability; workload security posture; cost / right-sizing; drift detection — all **its namespace only**                                                                                   | Everything cluster- and fleet-level                                                                                           |

Each tier's heartbeat still routes any proposed change through the propose→review→reconcile loop
(§1) with a human merge — a heartbeat never mutates directly, and never auto-merges.

---

## 5. Autonomous recovery: the recovery ladder

When execution hits a transient blocker (auth, IAM, identity, bootstrap), the agent follows the
bounded **Worker Recovery Ladder** (`SOUL.md §5`) before escalating:

1. Re-run / re-query to capture the exact failure.
2. Inspect identity context (SA annotations, Workload Identity, IAM bindings).
3. Inspect platform recovery mechanisms (Config Connector, Argo/Flux, GKE Hub).
4. Apply an allowed self-repair (e.g. token refresh via `scripts/github_token_refresh.py`) — never
   a direct cluster mutation; repairs still route through the declarative workflow.
5. Re-run and resume the original task.
6. Escalate to a human only as last resort.

**Cap:** 5 iterations or ~10 minutes of wall time per distinct blocker. This keeps "loop until
done" from becoming "loop forever," and ensures a real permission boundary escalates promptly.

### 5.1 Reconcile-failure recovery (post-merge)

The ladder above covers blockers during the agent's own execution (**before** a PR merges). A
distinct case is a **reconcile failure**: a PR is approved and merged, but Config Sync can't apply,
Config Connector can't reconcile the cloud resource, or the operator can't reconcile the CR. The
agent is read-only and only _observes_ reconcile status ([05](05-system-architecture.md) F2), so
recovery is a **corrective-PR loop**, never a direct fix:

1. **Detect** — the proposing agent watches its PR's reconcile status; the tier heartbeat (§4) also
   catches failures that slip past.
2. **Diagnose** — read Config Sync / Config Connector / operator **status + events**.
3. **Classify** transient vs. terminal:
   - **Transient** (quota, rate-limit, dependency-not-ready): defer to the reconciler's own backoff —
     wait and re-observe; do **not** act (the agent must not fight a reconciler that is already
     retrying).
   - **Terminal** (invalid config, schema/policy rejection): author a **corrective PR** — a fix, or a
     **revert** of the offending change — through the normal human-merged loop (§1).
4. **Escalate** to a human at the cap.

**Cap:** a few heartbeat cycles (or ~equivalent wall time), mirroring §5's intent. Every correction
is a human-merged PR — never a direct cluster write, never an auto-merge.

---

## 6. Failure isolation across tiers

The parent→child relationship is one of **authority and lifecycle, not runtime dependency**. Each
agent is an independent, operator-reconciled deployment with its own identity. Therefore:

| Failure                            | Effect                                                                                                                   | Recovery                                                                                  |
| ---------------------------------- | ------------------------------------------------------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------- |
| A **Developer Team Agent** is down | Only that namespace loses self-service; other namespaces unaffected                                                      | Operator reconciles the deployment; Cluster Admin Agent can re-propose it                 |
| A **Cluster Admin Agent** is down  | New namespace provisioning in that cluster pauses; existing Developer Team Agents keep running (independent deployments) | Operator self-heals; Platform Agent detects via heartbeat and re-provisions declaratively |
| The **Platform Agent** is down     | New cluster/fleet operations pause; running Cluster Admin & Developer Team agents keep operating within their scope      | Operator self-heals the deployment                                                        |
| The **operator** is down           | No new reconciliation; running agents continue; no new provisioning                                                      | Operator restart (standard controller recovery)                                           |

**Design intent:** no cascading failure. Because tiers don't call each other at runtime for their
core function — they're bound by declarative CRs the operator reconciles — an outage at one layer
degrades that layer's _new_ work, not the running state of the others.

> **Honest scoping — the hub is a shared-fate dependency for agent _reasoning_.** Inference (C5) and
> the GitHub token broker (Minty, C6) are hub-hosted shared services ([05](05-system-architecture.md)
> §3). If the **hub** is down, spoke **agents cannot reason (no inference) or propose changes (no
> brokered token)** — they pause. What survives is the **already-reconciled cluster state and running
> workloads**, because each spoke's Config Sync reconciles locally from the last-synced repo. So
> "spoke autonomy when the hub is down" means _the cluster keeps running its desired state_, **not**
> _the spoke agents keep operating_. True agent autonomy under hub loss would require regional/
> per-spoke inference — deliberately out of scope for v1 (a cost trade-off, see
> [05](05-system-architecture.md) §6).

---

## 7. End-to-end change lifecycle (worked example)

_Cluster admin asks their agent: "give team-payments a namespace with standard isolation."_

1. **Intent** — request arrives via the Cluster Admin Agent's chat entrypoint (authenticated user).
2. **Propose** — the agent authors declarative manifests (Namespace, RBAC, default-deny
   NetworkPolicy, ResourceQuota) and, if the team wants an agent, an `Agent{tier: developer-team}` CR
   — on a branch via `submit-suggestion`.
3. **Review** — security-review suite runs (§3); because this creates a namespace + a lower-tier
   agent, it hits mandatory gates (§2.2): a **human cluster administrator approves** (§2.3).
4. **Reconcile** — on merge, **Config Sync** applies the namespace + the template-rendered
   **namespace-scoped read-only RBAC** (the operator does not mint it), and the operator reconciles
   the `Agent{tier: developer-team}` CR into a runtime deployment. The downward attenuation ceiling is
   enforced in depth — `ValidatingAdmissionPolicy` + the operator's validating webhook
   ([03](03-security-model.md) §4).
5. **Report & audit** — the agent reports outcome in human-readable form; trace/session/requester
   are recorded ([03](03-security-model.md) §5, `docs/designs/audit-logging-user-attribution.md`).

Every step is declarative, reviewed, attributable, and revertible.

---

## 8. Goals & non-goals

### Goals

- One universal change loop (propose → review → reconcile) for all agents and all tiers.
- Clear, unconditional gates for consequential actions; autonomy for safe, in-scope work.
- Human approval authority anchored at the tier that owns the blast radius.
- Proactive, heartbeat-driven stewardship at every layer, all changes via the loop.
- Bounded autonomous recovery; failure isolation without cascade.

### Non-goals

- Prescribing one GitOps tool — the loop is mechanism-agnostic (GitHub PR, Config Connector,
  Argo/Flux, pipeline).
- Redefining identity/RBAC internals (that is [03](03-security-model.md)).
- Specifying chat/UX details of approval prompts.
