# Design 07: Implementation Roadmap

**Status:** ✅ Agreed — started 2026-07-21

**Charter:** [README.md](README.md) · **Depends on:** 01–06 · **Tier:** Buildable (bridging)

---

## TL;DR

The sequence to build kube-agents from its current state (direct-mutation agents, only
`PlatformAgent`) to the end state (three read-only, scope-bounded personas — each an **`Agent` CR**
running the **Hermes** harness, reconciled by the **kube-agents controller** on **Scion**'s verified
per-pod model — coordinating via GitOps + OKF; semantic-recall/mem0 deferred post-v1). Eight phases,
each with **acceptance criteria** that gate advancement. Every design decision a builder needs lives
in the specs (01–06, 08); this doc is sequencing only. The **Definition of Done** makes
[01](01-vision-scope.md) §7 concrete.

---

## 1. Current state → end state (delta summary)

| Aspect             | Current                                              | End state                                                                                      |
| ------------------ | ---------------------------------------------------- | ---------------------------------------------------------------------------------------------- |
| Agents             | 1 (Platform), can mutate directly (MCP + write RBAC) | 3 tiers, **read-only**                                                                         |
| Mutation path      | Direct API / KCC CR written by agent                 | GitOps PR → **customer's CI/CD pipeline** applies (KCC YAML or Terraform HCL)                  |
| Agent runtime      | single `PlatformAgent` CRD + Kubebuilder operator (mints RBAC) | **kube-agents controller** (the operator, generalized to a tier-discriminated `Agent` CRD) reconciles each agent (Hermes) into an isolated pod on **Scion**'s verified per-pod model; controller mints **no** RBAC |
| Actuation          | none                                                 | **Customer CI/CD** (GitHub Actions / CircleCI / …); unopinionated, no bundled GitOps engine    |
| Coordination       | ad-hoc / per-user memory                             | GitOps repo + OKF, indirect (mem0 deferred post-v1)                                            |
| Human→agent access | anyone who can reach the agent                        | **Trusted-human access** (authenticated + `AllowedUsers`) + **read-only agent ceiling**; per-request user down-scoping deferred ([08](08-agent-runtime-and-identity.md) §5) |
| Security gate      | none in CI                                           | review-gate on PR + heartbeat audit                                                            |

## 2. Phases

Each phase is independently shippable and leaves the system working. Do not advance until
acceptance criteria pass.

### Phase 0 — Foundations

- **Goal:** repo layout + guardrails + a test cluster exist before behavior changes.
- **Work:** scaffold the **customer GitOps repo layout** ([06](06-api-and-data-contracts.md) §3 — it
  does not exist yet); scaffold `knowledge/` OKF base with `index.md` + one `cluster-blueprint` and an
  **OKF validator script** (valid `type` frontmatter + resolving links); add the per-tier **read-only
  RBAC render overlay** (SA/Role/RoleBinding) and branch protection requiring human review on
  `**/agents/**`, `**/namespaces/**`, and `**/policy/**`; stand up a **test cluster** (`local-dev/` Kind
  bootstrap or a scratch GKE cluster — neither exists yet; **K8s ≥1.30** for `ValidatingAdmissionPolicy`
  GA) for the negative tests; ship the **`ValidatingAdmissionPolicy`** that hard-denies any
  `Role`/`ClusterRole` whose rules grant an **agent ServiceAccount** a write verb or a wrong-scope (e.g.
  cluster-scoped for a namespace tier) grant — selecting agent RBAC by the **`kube-agents/tier` label**
  the **render overlay** stamps on the pre-created `Role`/`ClusterRole` (the reliable selector; agent SAs
  are also named `<tier>-agent`), the controller minting no RBAC to label, with CEL scoped to a role's own
  `rules` — the runtime backstop for attenuation
  ([03](03-security-model.md) §4). (Automated review-gate
  CI lands in Phase 5; the cross-object child ⊆ parent validating webhook is deferred hardening,
  [08](08-agent-runtime-and-identity.md) §5.)
- **Accept:** repo tree matches 06 §3; the **pre-created RBAC overlay + CI are the only RBAC path
  exercised in Phase 0** (the controller isn't deployed to the Phase-0 negative-test cluster yet —
  removing its runtime RBAC-minting is **Phase 1**); a deliberately-bad RBAC PR (agent write verb) is
  caught by human review **and**, if merged anyway, is **rejected at apply time by the
  `ValidatingAdmissionPolicy`** on the test cluster; the **OKF validator script passes** on `knowledge/`.

### Phase 1 — Read-only Platform Agent + GitOps loop

- **Goal:** close the biggest delta — remove direct mutation from the Platform Agent.
- **Work:** extend `k8s-operator/` into the **kube-agents controller** — generalize `PlatformAgent` →
  the tier-discriminated **`Agent` CRD** (add `tier`/`scope`/`parentRef`), **stop minting RBAC** (the
  `view` binding + "explorer" ClusterRole become pre-created manifests), and add the **`(tier,scope)`
  cardinality validating webhook** ([06](06-api-and-data-contracts.md) §1, §1.2,
  [08](08-agent-runtime-and-identity.md)); pin the controller image + deploy manifests
  ([05](05-system-architecture.md) §3); author the **platform-tier `Agent` CR** (Hermes harness),
  migrating today's `PlatformAgent`. **Spike:** wire the controller's pod-construction to Scion's launch
  primitive ([08](08-agent-runtime-and-identity.md) §2), falling back to the operator's native Deployment
  build if Scion's K8s mode is not ready. Pre-create the platform read-only KSA/RBAC/WI (applied by CI)
  and reference it via the CR's `serviceAccountName`, **unifying the canonical `<tier>-agent` KSA** the
  pre-created view/explorer manifests bind to; make the agent read-only by editing the operator's
  **`renderConfigYAML()`** — the runtime-authoritative config; the baked `agents/platform/config.yaml` is
  shadowed at runtime — so no cluster-creating tool reaches it and the `gke` MCP is describe/list only
  ([06](06-api-and-data-contracts.md) §9), and **retire the `gke-cluster-creator` skill's `create_cluster`
  call**; strip write verbs from `platform-agent-role` and **drop the controller's RBAC-granting
  kubebuilder markers** (`clusterroles`/`clusterrolebindings` create/bind) so it mints no RBAC
  ([08](08-agent-runtime-and-identity.md) §4); wire an **actuation pipeline** (the customer's CI/CD — reference: a GitHub
  Actions workflow) that applies merged artifacts (KCC YAML or Terraform HCL) to the target
  ([06](06-api-and-data-contracts.md) §4); route all infra changes through `submit-suggestion`; lock the
  human→agent boundary to **trusted-human access** — authenticated chat + an explicit `AllowedUsers`
  allowlist ([03](03-security-model.md) §4a, [08](08-agent-runtime-and-identity.md) §2). (Per-request
  user-scoped authorization + the external gateway are deferred hardening,
  [08](08-agent-runtime-and-identity.md) §5.)
- **Accept:** Platform Agent can provision a cluster **only** by opening a PR with a KCC or Terraform
  artifact that the CI/CD pipeline applies on merge; a direct-mutation attempt fails (no RBAC/tool);
  audit record ties the change to requester + PR; **only allowlisted (trusted) humans can reach the
  agent, and the agent can only read within its tier + propose** (no direct mutation, no reads outside
  tier).

### Phase 2 — Cluster Admin Agent + cascade

- **Goal:** second tier, provisioned by the first.
- **Work:** author a **cluster-admin `Agent` CR** + its cluster-scoped read-only KSA/RBAC/WI manifests
  (applied by the CI/CD pipeline, §2 — not minted at runtime); **resolve the persona→runtime mapping**
  (per-persona image vs a mounted profile, [06](06-api-and-data-contracts.md) §1.1) so a non-platform
  persona is buildable; Platform Agent proposes them via GitOps (cascade F4); the controller reconciles
  the pod bound to that SA; a per-target actuation pipeline.
  RBAC least-privilege is enforced by the `ValidatingAdmissionPolicy` (Phase 0); the cross-object
  child ⊆ parent ceiling webhook is deferred hardening ([03](03-security-model.md) §4,
  [08](08-agent-runtime-and-identity.md) §5).
- **Accept:** Platform Agent proposes a cluster-admin agent; after human approval + merge, the
  controller runs it with read-only cluster identity and it can read only its cluster; it has its own
  chat entrypoint; RBAC granting an agent SA a write verb or a wrong-scope binding is **rejected at apply
  time by the `ValidatingAdmissionPolicy`**, even if merged.

### Phase 3 — Developer Team Agent + isolation proof

- **Goal:** third tier + the load-bearing isolation property.
- **Work:** author a **developer-team `Agent` CR** + namespace-scoped read-only identity
  manifests; Cluster Admin Agent proposes them; the controller reconciles them in the team's namespace;
  default-deny NetworkPolicy + ResourceQuota per namespace.
- **Accept:** a Developer Team Agent operates only in its namespace; it is **provably unable** to
  read another namespace or escalate (negative test passes) — this holds regardless of who is asking,
  because the agent's SA is namespace-scoped; cross-tier requests go via shared state, never a direct
  call. (Per-user confused-deputy protection is deferred, [03](03-security-model.md) §4a.)

### Phase 4 — Coordination & knowledge

- **Goal:** turn on indirect coordination (GitOps + OKF; no vector store in v1).
- **Work:** wire OKF read/update into all tiers ([06](06-api-and-data-contracts.md) §5); define
  per-tier heartbeat SOPs ([04](04-workflow-model.md) §4) for Cluster Admin + Developer Team, **including
  the Platform Agent's drift-detection SOP that opens a corrective PR unprompted**. (Semantic recall /
  mem0 is **deferred post-v1** — [02](02-agent-personas.md) §2.3.)
- **Accept:** an escalation written by a lower tier is picked up by its parent on heartbeat (no
  direct call); an agent retrieves a runbook via OKF; per-tier heartbeats run scoped audits; **inject
  drift** (RBAC / NetworkPolicy / version skew) → the Platform Agent detects it on heartbeat and opens a
  **corrective PR unprompted** — never a direct fix (satisfies [01](01-vision-scope.md) §7 SC4).

### Phase 5 — Security gate & hardening

- **Goal:** make the security model continuously enforced.
- **Work:** review-gate CI ([06](06-api-and-data-contracts.md) §7) on PR + heartbeat, run via the
  headless harness runner (the skills are agent-driven, [06](06-api-and-data-contracts.md) §7); egress
  allowlists per tier; the **`runtimeClassName` VM sandbox** for untrusted code (the CR's
  `runtimeClassName`, set by the controller on Scion's model, [08](08-agent-runtime-and-identity.md));
  end-to-end attribution.
  (Attenuation `ValidatingAdmissionPolicy` already landed in Phase 0; the cross-object webhook is
  deferred hardening, [08](08-agent-runtime-and-identity.md) §5.)
- **Accept:** a PR with an unmitigated high finding is blocked; egress outside the allowlist is
  denied; untrusted code runs sandboxed; every mutation is attributable.

### Phase 6 — Failure-isolation & resilience validation

- **Goal:** prove no cascade failure ([04](04-workflow-model.md) §6).
- **Work:** chaos tests killing the hub, a Cluster Admin Agent, and the controller.
- **Accept:** hub down → spoke clusters keep running their **last-applied state** (workloads keep
  running; the external CI/CD can still apply already-merged changes), though spoke **agents pause**
  (hub-hosted inference/Minty — [04](04-workflow-model.md) §6) and resume on recovery; Cluster Admin
  down → its Dev Team Agents keep running, new provisioning pauses and resumes on recovery; the
  controller relaunches agent pods.

### Phase 7 — Cloud-agnostic seams (later)

- **Goal:** reduce GKE coupling ([01](01-vision-scope.md) §6).
- **Work:** exercise the already-unopinionated seams — generate Terraform HCL as well as KCC YAML,
  actuate via a second CI/CD (e.g. CircleCI), and abstract observability behind provider-neutral
  seams.
- **Accept:** a second target (EKS/AKS/vanilla) passes the Phase 1–3 acceptance on core concepts,
  using the customer's IaC + pipeline of choice.

## 3. Definition of Done (product-level acceptance)

Built end-to-end means all of these pass — the concrete form of [01](01-vision-scope.md) §7:

1. A platform operator provisions a cluster **only** through the Platform Agent (PR → CI/CD pipeline
   applies KCC YAML or Terraform), zero manual `kubectl`/console, fully attributed.
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
8. **The human→agent boundary is secured by trusted-human access + the read-only ceiling** — only
   authenticated, allowlisted humans can reach an agent, and no human (trusted or not) can drive it to
   mutate or read outside its tier ([03](03-security-model.md) §4a). _(Per-request user down-scoping —
   the confused-deputy fix — is deferred hardening, [08](08-agent-runtime-and-identity.md) §5.)_
9. The Platform Agent detects an **injected drift** (RBAC / NetworkPolicy / version) and opens a
   **corrective PR unprompted** — never a direct fix (SC4, [01](01-vision-scope.md) §7).

## 4. Risks

- **Runtime coupling to Hermes** — the persona model assumes the Hermes agent runtime; the
  framework-portability non-goal ([02](02-agent-personas.md) §9) bounds this.
- **Scion integration maturity** — Scion's K8s runtime is early; v1 does **not** depend on it for
  lifecycle (the kube-agents controller owns that). Wiring the controller's pod-construction to Scion's
  launch primitive is a Phase-1 **spike** with a fallback to the operator's native Deployment build
  ([08](08-agent-runtime-and-identity.md) §2), so a Scion gap cannot block the build.
- **IaC coverage** — a chosen artifact format may not cover every resource (not every GCP resource
  has a KCC CRD; a Terraform provider may lag); gaps may force switching format for that resource or a
  documented, audited exception path (never silent direct mutation).
- **Pipeline as privileged writer** — actuation moves the write credentials into the customer's CI/CD
  ([03](03-security-model.md) §4). That pipeline is a high-value target: require least-privilege
  scoped deploy credentials, apply only on merged/reviewed state, and audit every run.
- **Migration window** — Phase 1 removes tools agents rely on today; sequence behind read-only RBAC
  so there is no period where agents can both mutate directly _and_ via PR.
- **mem0/Qdrant operational cost (deferred)** — a stateful vector store was the cost concern; v1
  **defers mem0 entirely** and coordinates on GitOps + OKF, removing this footprint. Revisit only with
  evidence that semantic recall over OKF is insufficient.
- **Hub is a shared-fate dependency for agent reasoning** — inference + Minty are hub-hosted
  ([05](05-system-architecture.md) §3), so a hub outage pauses spoke _agents_ (reconciled cluster
  state keeps running). Phase 6 chaos tests must assert the honest property ([04](04-workflow-model.md)
  §6), not "agents keep operating." Regional/per-spoke inference is the (deferred) mitigation.

## 5. Verification loop (how the harness iterates to "done")

Every spec carries a **Verification** section of concrete, mostly-runnable checks — 02 §10, 03 §11,
04 §9, 05 §8, 06 §10, 08 §7 — and every phase in §2 has **acceptance criteria**. Build by phase and,
after each phase, run:

1. the **phase acceptance** for the current phase (§2),
2. the **Verification** checks of every spec whose surface that phase touched, and
3. the **Definition of Done** (§3) once all phases are complete.

**Iterate until green.** If any check fails, fix and re-run — do not advance a phase (or open the final
PR) until its checks pass. The two load-bearing suites are the **negative security tests** (03 §11 —
read-only, attenuation, no-break-glass, isolation) and the **failure-isolation chaos tests** (05 §8); a
build is not "done" until both are green. Record which checks ran (and any deliberately skipped, e.g.
deferred-hardening items) in the PR.
