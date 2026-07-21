# Design 07: Implementation Roadmap

**Status:** ✅ Agreed — started 2026-07-21

**Charter:** [README.md](README.md) · **Depends on:** 01–06 · **Tier:** Buildable (bridging)

---

## TL;DR

The sequence to build kube-agents from its current state (direct-mutation agents, only
`PlatformAgent`) to the end state (three read-only, scope-bounded personas — one tier-discriminated
`Agent` CRD — coordinating via GitOps + OKF; semantic-recall/mem0 deferred post-v1). Eight phases,
each with **acceptance criteria** that gate advancement. A **Defaults** table resolves every open
question across 01–06 so an autonomous builder never stalls. The **Definition of Done** makes
[01](01-vision-scope.md) §7 concrete.

---

## 1. Current state → end state (delta summary)

| Aspect | Current | End state |
|--------|---------|-----------|
| Agents | 1 (Platform), can mutate directly (MCP + write RBAC) | 3 tiers, **read-only** |
| Mutation path | Direct API / KCC CR written by agent | GitOps PR → Config Sync + Config Connector |
| CRDs | `PlatformAgent` | single **`Agent`** CRD, tier-discriminated (`PlatformAgent` → `Agent{tier: platform}`) |
| GitOps engine | none | Config Sync (`RootSync` per cluster) |
| Coordination | ad-hoc / per-user memory | GitOps repo + OKF, indirect (mem0 deferred post-v1) |
| Security gate | none in CI | review-gate on PR + heartbeat audit |

## 2. Phases

Each phase is independently shippable and leaves the system working. Do not advance until
acceptance criteria pass.

### Phase 0 — Foundations
- **Goal:** repo layout + guardrails exist before behavior changes.
- **Work:** create GitOps repo layout ([06](06-api-and-data-contracts.md) §3); scaffold `knowledge/`
  OKF base with `index.md` + one `cluster-blueprint`; add the per-tier **read-only RBAC template**
  (SA/Role/RoleBinding) and branch protection requiring human review on `**/rbac/**` and
  `**/agents/**`; ship the **`ValidatingAdmissionPolicy`** that hard-denies any `Role`/`RoleBinding`
  granting an agent ServiceAccount a write verb or a wrong-scope (e.g. cluster-scoped for a
  namespace tier) binding — the runtime backstop for attenuation ([03](03-security-model.md) §4).
  (Automated review-gate CI lands in Phase 5; the operator's child ⊆ parent validating webhook lands
  in Phase 2 with the cascade.)
- **Accept:** repo tree matches 06 §3; the operator runs with **no RBAC-granting permissions**; a
  deliberately-bad RBAC PR (agent write verb) is caught by human review **and**, if merged anyway, is
  **rejected at apply time by the `ValidatingAdmissionPolicy`**; OKF visualizer renders `knowledge/`.

### Phase 1 — Read-only Platform Agent + GitOps loop
- **Goal:** close the biggest delta — remove direct mutation from the Platform Agent.
- **Work:** introduce the single **`Agent`** CRD (tier discriminator) and migrate `PlatformAgent` →
  `Agent{tier: platform}` ([06](06-api-and-data-contracts.md) §1); remove `create_cluster`; restrict
  `gke` MCP to read-only ([06](06-api-and-data-contracts.md) §9); strip write verbs from
  `platform-agent-role`; install Config Sync (`RootSync` → `fleet/`) and Config Connector in the hub;
  route all infra changes through `submit-suggestion`.
- **Accept:** Platform Agent can provision a cluster **only** by opening a PR with a KCC
  `ContainerCluster` CR that Config Sync applies and Config Connector reconciles; a direct-mutation
  attempt fails (no RBAC/tool); audit record ties the change to requester + PR.

### Phase 2 — Cluster Admin Agent + cascade
- **Goal:** second tier, provisioned by the first.
- **Work:** enable the **`cluster-admin` tier** of the `Agent` CRD ([06](06-api-and-data-contracts.md)
  §1) in the (single) reconciler; the cluster-scoped **read-only** RBAC is template-rendered and
  applied by Config Sync (§2), not minted by the operator; Platform Agent proposes the CR via GitOps
  (cascade F4); per-cluster Config Sync/Connector. Add the **operator's validating admission webhook**
  enforcing the child ⊆ parent attenuation ceiling using CRD lineage ([03](03-security-model.md) §4) —
  vetoes only, no `escalate`/`bind`.
- **Accept:** Platform Agent proposes an `Agent{tier: cluster-admin}`; after human approval + merge, it
  runs with read-only cluster identity and can read only its cluster; it has its own chat entrypoint; a
  CR (or its RBAC) requesting scope broader than the Platform Agent's is **rejected by the operator
  webhook**, even if merged.

### Phase 3 — Developer Team Agent + isolation proof
- **Goal:** third tier + the load-bearing isolation property.
- **Work:** enable the **`developer-team` tier** (namespace-scoped read-only identity) in the same
  reconciler; Cluster Admin Agent proposes them; default-deny NetworkPolicy + ResourceQuota per
  namespace.
- **Accept:** a Developer Team Agent operates only in its namespace; it is **provably unable** to
  read another namespace or escalate (negative test passes); cross-tier requests go via shared
  state, never a direct call.

### Phase 4 — Coordination & knowledge
- **Goal:** turn on indirect coordination (GitOps + OKF; no vector store in v1).
- **Work:** wire OKF read/update into all tiers ([06](06-api-and-data-contracts.md) §5); define
  per-tier heartbeat SOPs ([04](04-workflow-model.md) §4) for Cluster Admin + Developer Team.
  (Semantic recall / mem0 is **deferred post-v1** — [02](02-agent-personas.md) §2.3.)
- **Accept:** an escalation written by a lower tier is picked up by its parent on heartbeat (no
  direct call); an agent retrieves a runbook via OKF; per-tier heartbeats run scoped audits.

### Phase 5 — Security gate & hardening
- **Goal:** make the security model continuously enforced.
- **Work:** review-gate CI ([06](06-api-and-data-contracts.md) §7) on PR + heartbeat; egress
  allowlists per tier; VM-based `RuntimeClass` sandbox for untrusted code; end-to-end attribution.
  (Attenuation admission enforcement already landed — `ValidatingAdmissionPolicy` in Phase 0, the
  operator validating webhook in Phase 2.)
- **Accept:** a PR with an unmitigated high finding is blocked; egress outside the allowlist is
  denied; untrusted code runs sandboxed; every mutation is attributable.

### Phase 6 — Failure-isolation & resilience validation
- **Goal:** prove no cascade failure ([04](04-workflow-model.md) §6).
- **Work:** chaos tests killing hub, a Cluster Admin Agent, and the operator.
- **Accept:** hub down → spoke clusters keep running their **last-synced state** (workloads + local
  Config Sync), though spoke **agents pause** (hub-hosted inference/Minty — [04](04-workflow-model.md)
  §6) and resume on recovery; Cluster Admin down → its Dev Team Agents keep running, new provisioning
  pauses and resumes on recovery; operator self-heals deployments.

### Phase 7 — Cloud-agnostic seams (later)
- **Goal:** reduce GKE coupling ([01](01-vision-scope.md) §6).
- **Work:** abstract provisioning (Config Connector ↔ Crossplane) and GitOps (Config Sync ↔
  Argo/Flux) and observability behind provider-neutral seams.
- **Accept:** a second target (EKS/AKS/vanilla) passes the Phase 1–3 acceptance on core concepts.

## 3. Defaults — resolving every open question so a builder never stalls

**Confirmed 2026-07-21.** These are decided defaults, not proposals — a builder uses them without
stalling. Any can still be revisited by a human, but building never waits on debate. Source column
points to the doc that raised the question.

| Question | Source | **Default decision** |
|----------|--------|----------------------|
| Operator scope (central vs per-cluster) | 05 §7 | **One operator per cluster**, each reconciling only its own cluster's agent CRs (hub → the platform-tier `Agent`; spoke → its cluster-admin + developer-team `Agent` CRs). No cross-cluster creds; preserves failure isolation + least privilege |
| OKF location | 05 §7 | **`knowledge/` root in the GitOps repo** (reuse review flow; outside Config Sync's synced paths, so never applied); dedicated repo optional later |
| mem0 placement | 05 §7 | **Deferred post-v1** — semantic recall (mem0/Qdrant) not in v1; OKF-in-git covers durable shared knowledge and the need is unproven. If later added: single shared hub Qdrant with server-side scope isolation, best-effort |
| Single-cluster install | 05 §7 | **Collapse topology, not personas**: one cluster plays hub+spoke; all three tiers + shared services (run once) present; single `RootSync` covers `fleet/` + `clusters/<self>/`; persona model & isolation proof identical to multi-cluster |
| Attenuation enforcement point | 03 §10 | **RBAC applied via Config Sync from reviewed PRs; operator holds no RBAC-granting perms.** Enforced **in depth, all in v1**: (1) review-gate shift-left, (2) `ValidatingAdmissionPolicy` denies agent-SA write verbs / wrong-scope bindings at apply time, (3) operator **validating** webhook enforces child ⊆ parent ceiling. Operator validates, never grants. (Revisited 2026-07-21 — supersedes "admission deferred") |
| Egress allowlist | 03 §10 | **Per-tier default-deny NetworkPolicy** (v1) allowing inference, cloud APIs, GitHub, **and MCP tool endpoints** (e.g. developer-knowledge, gke) for live-doc grounding (mem0 endpoint added only if mem0 is later introduced); L7 egress proxy deferred to Phase 5 |
| Multi-tenant inference isolation | 03 §10 | **Per-tier/per-tenant LiteLLM virtual keys** (own budget/rate-limit/logging) on the shared proxy; separate proxies only if data-sensitivity later requires |
| CI wiring for review gate | 04 §8, 06 §7 | **GitHub Actions on PR** (paths in 06 §7) **+ heartbeat re-run**; high/critical blocks |
| Approval UX | 04 §8 | **PR merge is the sole source of truth; no auto-merge for any tier.** Chat approval cards are **not in v1** (may later mirror the PR, both audited; PR stays authoritative) |
| Autonomy tuning per tier | 04 §8 | **Uniform mandatory-gate list** (04 §2.2) across tiers; **no auto-merge for any tier**. Dev Team may *propose* more freely (small blast radius), but every change is still human-merged |
| Heartbeat scoping | 04 §8 | Cluster Admin + Dev Team get **scoped subsets** of the Platform Agent's 10 governance jobs, by persona responsibility (concrete mapping in 04 §4) |
| Break-glass governance & reconciliation | 01 §8, 03 §10, 04 §8 | **None** — all changes (incl. emergencies) go through human-approved GitOps. Break-glass is deliberately **not part of the design** (simplicity), not a deferral; no JIT / direct-access path exists |
| Recovery ladder for declarative failures | 04 §8 | Reconcile-failure corrective-PR loop (04 §5.1): detect → diagnose → classify transient (defer to backoff) vs. terminal (corrective/revert PR) → escalate at the cap; all corrections human-merged |
| Staging milestones (augment→replace) | 01 §8 | **Safety model invariant across stages** — read-only agents + human-approved GitOps at every stage; no autonomy-to-mutate ramp. "Full replacement" = interface only. Phases build capability, not reduced oversight |
| Portability target | 01 §8 | **Deferred to Phase 7**; no committed second platform yet |
| Success metrics (SLIs) | 01 §8 | **v1: two continuous SLIs only** — zero direct (non-GitOps) mutations, zero cross-scope isolation escapes; other DoD items are qualitative per-phase acceptance |
| CRD validation | 06 §10 | **Split by capability**: CEL `x-kubernetes-validations` for single-object rules (`tier` immutability, tier↔scope fields, `parentRef` required for non-platform tiers); validating webhook for cross-object (cardinality uniqueness, correct parent tier, attenuation ceiling child ⊆ parent) |
| RepoSync per-namespace delegation | 06 §10 | **Single `RootSync` per cluster**; namespace isolation is via agent RBAC + admission, not reconciler topology; add per-namespace `RepoSync` only for reconciler-credential isolation / delegated repos |
| OKF type vocabulary | 06 §10 | **Open/extensible**: the six types in 06 §5 are the canonical starting set; `type` is a documented convention (not a hard enum), extended by PR as needs arise |
| mem0 retention / graduation | 06 §10 | **Deferred post-v1 with mem0.** If added: TTL by default (~30–90 days) + graduate durable observations mem0 → OKF via human-reviewed PR |

## 4. Definition of Done (product-level acceptance)

Built end-to-end means all of these pass — the concrete form of [01](01-vision-scope.md) §7:

1. A platform operator provisions a cluster **only** through the Platform Agent (PR → Config Sync →
   Config Connector), zero manual `kubectl`/console, fully attributed.
2. A cluster admin provisions a namespace + Developer Team Agent through the Cluster Admin Agent,
   within Platform-set guardrails, human-approved.
3. A developer team self-serves a workload via its agent and is **provably unable** to affect
   another namespace or escalate.
4. All three agents are **read-only** on cluster/cloud APIs; the only write path is a reviewed PR.
5. Agents coordinate **only** indirectly (GitOps + OKF); a negative test confirms no direct
   agent-to-agent call path exists.
6. The review-gate blocks an unmitigated high-severity change; every mutation is attributable and
   revertible.
7. Failure-isolation chaos tests (Phase 6) pass — no cascade.

## 5. Risks

- **Runtime coupling to Hermes** — the persona model assumes the Hermes agent runtime; the
  framework-portability non-goal ([02](02-agent-personas.md) §9) bounds this.
- **Config Connector coverage** — not every GCP resource has a KCC CRD; gaps may force a documented,
  audited exception path (never silent direct mutation).
- **Migration window** — Phase 1 removes tools agents rely on today; sequence behind read-only RBAC
  so there is no period where agents can both mutate directly *and* via PR.
- **mem0/Qdrant operational cost (deferred)** — a stateful vector store was the cost concern; v1
  **defers mem0 entirely** and coordinates on GitOps + OKF, removing this footprint. Revisit only with
  evidence that semantic recall over OKF is insufficient.
- **Hub is a shared-fate dependency for agent reasoning** — inference + Minty are hub-hosted
  ([05](05-system-architecture.md) §3), so a hub outage pauses spoke *agents* (reconciled cluster
  state keeps running). Phase 6 chaos tests must assert the honest property ([04](04-workflow-model.md)
  §6), not "agents keep operating." Regional/per-spoke inference is the (deferred) mitigation.
