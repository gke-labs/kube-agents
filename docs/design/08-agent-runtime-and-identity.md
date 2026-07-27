# Design 08: Agent Runtime & Identity (simple v1)

**Status:** ✅ Agreed

**Overview:** [README.md](README.md) · **Depends on:** [02](02-agent-personas.md),
[03](03-security-model.md), [04](04-workflow-model.md), [06](06-api-and-data-contracts.md) ·
**Tier:** Buildable (bridging)

---

## TL;DR

The simplest runtime that satisfies the multi-agent requirements: a **thin kube-agents controller**
(the existing Kubebuilder operator in `k8s-operator/`, extended) reconciles a single, tier-discriminated
**`Agent` CRD** into **one isolated pod per agent** — a **Hermes** harness bound to **one read-only,
tier-scoped ServiceAccount** (Workload Identity). The controller **owns pod lifecycle/relaunch,
`(tier,scope)` cardinality, and label-stamping**; it constructs the pod following the hardened,
per-pod-identity model verified in **[Scion](https://github.com/GoogleCloudPlatform/scion)** (Google's
multi-agent orchestrator: per-pod `serviceAccountName` _for Workload Identity_, `namespace`,
`runtimeClassName`, and a hardened pod-security context). **Wiring the controller to call Scion's launch
primitive** (rather than building the Deployment natively, as the operator does today) is a named
Phase-1 integration (§2, [07](07-implementation-roadmap.md)) — not a v1 blocker. **We own the identity;
the controller only references it** — the read-only KSA + RBAC + WI binding are pre-created via reviewed
PR → the customer's CI/CD, and **nothing mints RBAC at runtime**. Agents are read-only; the only write
path is a human-merged PR → the customer's CI/CD. Cron runs in-pod under that same SA. The human→agent
boundary is secured by **trusted-human access + the read-only ceiling** — v1 does **not** check the
requester's own permissions ([03](03-security-model.md) §4a). No scope broker, no co-located
multiplexer, no per-run token exchange, no CLI credential shims, no external authorization gateway, and
no untrusted-code-execution sandbox (v1 agents don't run untrusted code) — deferred hardening (§5). This deliberately prioritizes **simplicity over defense-in-depth**; trade-offs
in §4.

---

## 1. What this doc decides

Runtime packaging, deployment topology, and identity for the personas in
[02](02-agent-personas.md) — i.e. _how each agent actually runs and authenticates_, at the simplest
bar that meets the requirements (tiered agents, per-agent scoped identity, cron, read-only + PR,
user-permission awareness).

## 2. The solution

1. **Persona = an `Agent` custom resource, reconciled by the kube-agents controller.** Each persona is
   one instance of a single, tier-discriminated **`Agent` CRD** (`kubeagents.x-k8s.io`, generalizing
   today's `PlatformAgent`; it adds `tier` / `scope` / `parentRef`). The CR selects the **Hermes**
   harness with that persona's profile (`SOUL.md`, skills, cron) and references the pod's
   identity/placement. The CR + the identity manifests (item 4) are the **agent definition** — the
   controller is the runtime, and the custom `tier`/`scope`/`parentRef` fields live in **our** CRD, not
   in any third-party template. _(Persona→runtime is **decided for v1: a baked per-tier image**
   (`<tier>-agent:<tag>`) built from `agents/<tier>/` — `SOUL.md` + `config.yaml` + `skills/` + cron +
   governance — exactly as the platform image builds today (`deploy/docker/Dockerfile`,
   `FROM agent-base AS platform`); `profile` selects the image. A **mounted** profile is deferred
   hardening. Phase 1 migrates the existing platform image; Phase 2 adds the cluster-admin image the same
   way, [07](07-implementation-roadmap.md).)_
2. **The controller reconciles one isolated pod per agent.** The kube-agents controller (the extended
   `k8s-operator/`) turns each `Agent` CR into **one isolated pod** (a single-replica Deployment),
   setting: `spec.serviceAccountName` (a pre-created read-only, tier-scoped KSA — Workload-Identity-bound),
   the target `namespace` (placement), an optional `runtimeClassName` (sandbox), and a **hardened
   pod-security context** (non-root, seccomp `RuntimeDefault`, no privilege-escalation) by default. This
   is the exact per-pod, hardened-runtime shape verified in **Scion** (`pkg/api/types.go`,
   `pkg/runtime/k8s_runtime.go`). **v1 builds the pod natively** (as the operator does today); **wiring
   the controller to call Scion's launch primitive** for pod construction is a Phase-1 integration/spike
   ([07](07-implementation-roadmap.md)).
3. **The controller owns lifecycle, cardinality, and pod labels.** It **relaunches** a failed agent pod
   (a Deployment's ReplicaSet self-heals crashes; the controller re-reconciles drift), enforces
   **exactly one agent per `(tier, scope)`** via its validating webhook, and **stamps** the identifying
   labels `kube-agents/tier`, `kube-agents/scope`, and `kube-agents/parent` on the agent **pod**. The
   agent's **RBAC** objects are pre-created manifests (item 4), so the controller cannot label them — the
   **render overlay** labels (`kube-agents/tier`) and names (`*-agent`) them instead, and that is the
   selection convention the attenuation `ValidatingAdmissionPolicy` keys on ([03](03-security-model.md)
   §4, [06](06-api-and-data-contracts.md) §2).
4. **We own the identity; the controller references it (it does not mint it).** The per-agent KSA +
   read-only RBAC + Workload-Identity binding are **pre-created via reviewed PR → the customer's CI/CD**
   ([06](06-api-and-data-contracts.md) §2), scoped to the tier (project / cluster / namespace,
   [03](03-security-model.md) §3). The controller sets the pod's `serviceAccountName` to that KSA by
   name, so the pod runs as our **read-only, tier-scoped identity — the ceiling**. **Nothing grants RBAC
   at runtime** — the KSA/RBAC/WI are ordinary manifests the pipeline applies; the controller only
   consumes them. (This is a delta from today's operator, which still mints a `view` binding + an
   "explorer" ClusterRole — those move into pre-created, reviewed manifests, [07](07-implementation-roadmap.md).)
5. **Ambient identity is fine here.** Because the pod hosts exactly one agent and the SA the controller
   attaches is already least-privilege read-only, tools and CLIs (`kubectl`, `gcloud`) use the pod's
   **ambient** SA directly — no broker, no shim. They can only ever perform read-only, in-scope
   operations.
6. **Placement:** Platform → hub; Cluster Admin → its cluster; Developer Team → its namespace
   ([05](05-system-architecture.md) §3), via the CR's `scope`/placement. Native in-cluster/cloud reads
   through the ambient SA.
7. **Mutation is read-only-agent → PR → customer CI/CD.** Agents emit **KCC YAML or Terraform HCL**,
   open a PR (Minty-brokered token), a human merges, and the **customer's CI/CD pipeline** applies it
   ([04](04-workflow-model.md), [06](06-api-and-data-contracts.md)). Agents hold **no write creds**.
8. **Cron runs in-pod under the same SA.** Hermes' per-profile cron fires read-only checks; anything it
   wants to change goes through the PR loop. No per-run tokens, no attestation, no separate identity —
   cron reads at the agent's own (read-only, tier-scoped) authority.
9. **Human-request authorization = trusted-human access + read-only ceiling.** The control is _who may
   reach an agent_ — authenticated chat + `AllowedUsers` + per-audience entrypoints; only trusted
   humans get in. v1 does **not** check the requester's own GCP/K8s permissions and does not union them
   with the agent SA — the agent's read-only, tier-scoped identity is the ceiling ([03](03-security-model.md)
   §4a). A human selects the agent by handle / slash command / NL routing through the `@kage` gateway
   ([02](02-agent-personas.md) §2.4); routing is a convenience, not an authz signal, and the gateway
   enforces the target agent's `AllowedUsers` before dispatch. Per-request user-scoped authorization
   (the SAR/IAM check + down-scoping) is deferred (§5).
10. **Coordination is indirect** via the GitOps repo + OKF ([02](02-agent-personas.md) §2.3). No
    co-located multiplexer and no direct agent-to-agent messaging.

## 3. Deliberately out of scope (this is where the simplicity comes from)

None of the following are in v1 — each is additive and lives in the §5 hardening path:

- a **scope broker** / token-exchange service;
- **per-run ephemeral downscoped tokens** (interactive or cron);
- a **co-located multiplexer** (multiple profiles sharing one pod);
- **CLI credential shims** + metadata-server egress lockdown;
- **cron trigger attestation** (external scheduler + signed job manifests);
- **user-scoped authorization** entirely — the per-request `SubjectAccessReview`/IAM check **and** the
  down-scoping of the agent to the requester ([03](03-security-model.md) §4a); v1 relies on
  trusted-human access + the read-only ceiling instead;
- the **cross-object attenuation admission webhook** (child scope ⊆ parent scope, §5);
- the **external authorization gateway** as a separate component ([05](05-system-architecture.md)
  C14);
- **untrusted code execution and its execution sandbox** — v1 agents reason and author PRs; they do not
  run untrusted/model-generated code, so the gVisor execution sandbox (§5.1) is deferred with that
  capability.

Every one of these existed to make **co-location** or **per-request delegation** safe. v1 chooses **one
pod per agent** + **trusted-human access** instead, so they are unnecessary. The controller stays
**thin** on purpose: it reconciles `Agent` → pod, enforces cardinality, and stamps labels — it does
**not** mint RBAC, broker tokens, or authorize requests.

The **ChatOps gateway** that routes human messages to the right agent ([02](02-agent-personas.md)
§2.4, [05](05-system-architecture.md) C15/F5) is **not** the deferred co-located multiplexer above:
it dispatches to the **separate per-tier agent pods**, never co-locates profiles in one pod, and never
has an agent call another agent (coordination stays indirect, [02](02-agent-personas.md) §2.3). It
enforces the existing trusted-human allowlist (`AllowedUsers`) before dispatch and adds **no**
per-request authorization, so it introduces no new trust surface and needs none of the deferred
delegation machinery — it is a v1-compatible convenience layer over the per-audience entrypoints.

## 4. Security considerations

### Held — the load-bearing invariants

**All invariants of the security model ([03](03-security-model.md)) are retained** except per-request
user-scoped authorization, which v1 does not do (see below). Downward attenuation, the default-deny
egress allowlist, and the AI-agent defenses are unchanged; see 03. The guarantees this runtime shape
delivers:

- **No direct mutation** — the only write path is a human-merged PR → the customer's CI/CD; agents
  hold no write RBAC or write tools ([03](03-security-model.md) §7, [04](04-workflow-model.md)).
- **Nothing mints RBAC at runtime** — the controller references pre-created identity; it never grants
  scope. Identity is a reviewed manifest, backstopped at apply time by the attenuation
  `ValidatingAdmissionPolicy` ([03](03-security-model.md) §4).
- **One agent per scope, one least-privilege read-only SA** (1 Platform/project, 1 Cluster-Admin/cluster,
  1 Dev-Team/namespace — enforced by the controller's cardinality webhook) → tier/tenant isolation with
  **no shared-pod blast radius and no cross-tenant in-process leakage**: a Developer Team Agent's pod
  **cannot read another namespace**, a Cluster Admin Agent's **cannot reach another cluster**, and a
  Platform Agent's **cannot reach another project** ([03](03-security-model.md) §3–§4).
- **Trusted-human access** — only authenticated, allowlisted humans can reach an agent
  ([03](03-security-model.md) §4a). This, plus the read-only ceiling, is how the human→agent boundary
  is secured in v1.
- **Hardened pod runtime** — the controller applies a restricted pod-security context by default
  (non-root, seccomp `RuntimeDefault`, no privilege-escalation), following Scion's verified model, plus
  normalized OTel telemetry for attribution. v1 agents are **read-only reasoning + PR authoring** and do
  **not** execute untrusted code, so the hardened context is the whole runtime floor. When agents gain a
  **code-execution** capability, its untrusted/model-generated code must run in the
  `runtimeClassName` **execution sandbox** — a **deferred** capability that arrives _together with_ code
  execution, described in §5.1 (satisfies [03](03-security-model.md) §5's control-loop/sandbox split).

### Traded away — accepted for simplicity

- **No per-request user authorization.** v1 does **not** check the requester's own GCP/K8s permissions
  and does not union/down-scope the agent to them. A trusted human with narrow personal permissions
  can use the agent to read anything within its tier scope. Accepted: **access is limited to trusted
  humans, and the ceiling is read-only** ([03](03-security-model.md) §4a). _(The delegate model that
  closes this is the deferred hardening, §5.)_
- **Standing credentials, not per-run ephemeral.** A compromised pod can use its read-only SA for the
  duration of the compromise, not just for one run.
- **Ambient credentials for CLIs and cron.** Safe here _only because_ pods are single-tenant and the
  SA is least-privilege read-only — this is precisely why co-location is excluded.
- **A custom controller + CRD to own** — an operational/maintenance cost (chosen over leaning on
  Scion's early K8s orchestrator mode, which cannot yet supervise long-lived agent pods). We reuse the
  operator that already exists rather than build new.
- **Higher pod count** (up to ~1 per namespace) — an operational/cost cost, not a security one.

### Residual risks & mitigations

| Risk                                                                                          | Bound / mitigation                                                                                                                                                                                                                                                                                                                            |
| --------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Compromised or injected agent                                                                 | Reads only within its read-only tier scope; can open PRs but **cannot merge** (human gate); short-lived Minty tokens; audit; egress allowlist                                                                                                                                                                                                 |
| A trusted human reads within the agent's tier scope beyond their own rights (confused deputy) | Accepted in v1; bounded by **only granting agent access to trusted humans** + the read-only ceiling; per-request down-scoping is the deferred fix (§5)                                                                                                                                                                                        |
| Compromised controller (the runtime)                                                          | Thin by design — it references identity, never mints RBAC or brokers tokens; runs under its own SA (create/patch **agent-pod Deployments** in `kubeagents-system` and each developer-team agent's placement namespace, **no write on tenant workloads or cloud resources**); its actions are reconciles, not agent mutations, and are audited |
| Cron self-triggered by a compromised pod                                                      | In-scope, read-only only; proposals still human-merged                                                                                                                                                                                                                                                                                        |
| Prompt injection                                                                              | No mutation path (read-only + PR gate); cannot exceed SA scope; worst case is in-scope read exfil (egress-bounded) or a misleading PR (human-reviewed)                                                                                                                                                                                        |

## 5. Future hardening (only if/when needed)

### 5.1 Untrusted code execution & the execution sandbox (deferred)

Agents will eventually **generate and execute untrusted code** (model-written scripts, ad-hoc analysis,
tool code). That capability is **deferred past v1** — v1 agents only reason and author PRs — but when it
lands it must not run in the agent's own pod. This section fixes the intended mechanism now so the CRD's
`runtimeClassName` hook and the [03](03-security-model.md) §5 control-loop/execution-sandbox split have a
concrete, buildable target.

**Chosen mechanism: gVisor, via [GKE Agent Sandbox](https://docs.cloud.google.com/kubernetes-engine/docs/concepts/machine-learning/agent-sandbox).**
Of the three practical isolation runtimes — gVisor (userspace kernel), Kata Containers (lightweight VM),
Firecracker (microVM) — **gVisor is the lightest and the most Google-native**, and it is what we adopt:

- **Lightweight.** gVisor's `Sentry` intercepts syscalls in userspace (no VM boot, no guest kernel):
  millisecond-to-sub-second start and ~50–100Mi overhead per pod, versus Kata's guest kernel + VMM at
  ~130–512Mi and 150–300ms cold start. It needs **no nested virtualization** and existing container
  images run unmodified — a near drop-in via `RuntimeClass`. Trade-off: partial syscall compatibility and
  some overhead on syscall-heavy I/O — acceptable for bounded agent code execution.
- **Google-native / low-lift.** gVisor is a Google project (the same isolation that sandboxes Gemini).
  **GKE Agent Sandbox** is purpose-built for exactly this — safely running untrusted, AI-generated code —
  and is the only native agent sandbox among the major clouds. It is **open source** (a Kubernetes
  SIG Apps subproject), so it is not GKE lock-in and runs on any conformant cluster. It reuses the
  `runtimeClassName` field the `Agent` CRD **already** exposes: `RuntimeClass` `gvisor` (handler `runsc`).
  Enable it on GKE Standard with a `--sandbox type=gvisor` node pool (`cos_containerd` image); on
  Autopilot request it per-pod. Its `SandboxWarmPool` keeps pre-booted pods so a new sandbox is claimable
  in **under a second** (~300 sandboxes/sec), removing the cold-start cost that would otherwise make
  per-run isolation impractical.

**Topology — this is where the control-loop / execution-sandbox split lands.** The agent's
reasoning/control loop stays in its normal pod (allowlisted egress, read-only tier SA). Untrusted code
runs in a **separate, gVisor-sandboxed, air-gapped execution environment** (default-deny
`NetworkPolicy`, no service-account token, non-root, read-only rootfs, dropped capabilities) — claimed
from a warm pool per run and replenished after. The two never share a process, so both exfiltration and
kernel-escape blast radius are bounded.

**Known limit (pair, don't rely on it alone).** gVisor stops container escape and host-kernel exploits;
it does **not** constrain what the code does within the permissions it is granted — prompt-injection
that drives exfiltration through otherwise-legitimate calls is out of its scope, and a documented
metadata-server escape must be closed with `NetworkPolicy`. So the sandbox layers **on top of** the
read-only ceiling, the egress allowlist, and the PR gate — it does not replace them.

**Why deferred.** v1 agents don't execute untrusted code, so there is nothing to sandbox yet. The
sandbox **node pool already exists** in provisioning today (`make gcp-provision-02-gvisor`, `INSTALL.md`),
so the deferred piece is the **capability + its wiring** (`RuntimeClass` `gvisor`, the air-gapped
execution pod, the warm pool), **not** the infrastructure. The capability and its sandbox therefore ship
**together**, as a unit, post-v1 — never code execution first, sandbox later. Until then the v1 floor is
the hardened pod-security context (§4).

### 5.2 Delegation & co-location hardening (deferred)

If pod count (cost) or the best-effort user-down-scoping proves insufficient, layer on the model
explored during design (kept out of v1 for simplicity):

- **Cross-object attenuation webhook** — a validating admission webhook enforcing that a child agent's
  scope is a **strict subset** of its parent's (pure CEL can't express this cross-object). The
  kube-agents controller is its natural host; v1 relies on the review-gate + the in-tree
  `ValidatingAdmissionPolicy` instead ([03](03-security-model.md) §4, [06](06-api-and-data-contracts.md)
  §1.2).
- **co-located profiles** via a Hermes multiplexer (fewer pods), which then requires
- a **scope broker** issuing **per-run ephemeral, downscoped tokens** — interactive runs down-scoped
  to the requesting human; cron runs authorized by an **attested trigger + reviewed job manifest**;
- **CLI credential shims** + metadata-egress lockdown so shell `kubectl`/`gcloud` also go through the
  broker; and
- the **external authorization gateway** ([05](05-system-architecture.md) C14) as the enforcement
  point outside the LLM loop.

None are required for v1; each is additive and can be adopted independently when the cost/benefit
flips.

## 6. Goals & non-goals

### Goals

- The **simplest** runtime that meets the tiered-agent + per-agent-identity + cron + read-only + PR
  requirements.
- Use a **thin kube-agents controller** (the extended `k8s-operator/`) reconciling a single
  tier-discriminated **`Agent` CRD** as the runtime, and **Hermes** as the harness; the `Agent` CR +
  Hermes profile are the persona-packaging format.
- Reuse the hardened, per-pod-identity model verified in **Scion**
  (`serviceAccountName`/`namespace`/`runtimeClassName` + hardened pod security), with a Phase-1 path to
  calling Scion's launch primitive directly — rather than leaning on Scion's early K8s orchestrator
  mode for lifecycle.
- Document the security trade-offs **honestly**, with an explicit upgrade path.

### Non-goals

- Broker / ephemeral-token infrastructure, co-location, or per-request credential enforcement (v1).
- Framework portability beyond the Hermes runtime ([02](02-agent-personas.md) §9).
- Deploying Scion as a standalone per-cluster orchestrator (its K8s runtime is early; the controller
  owns lifecycle in v1).

## 7. Verification

- **One pod per agent, correct identity:** for each `Agent` CR the controller reconciles, assert the
  pod's `spec.serviceAccountName`, `namespace`, `runtimeClassName` (where required), and the hardened
  securityContext.
- **Cardinality:** creating a second `Agent` CR for the same `(tier, scope)` is **rejected** by the
  controller's validating webhook.
- **Read-only ceiling from inside the pod:** exec into an agent pod; `kubectl auth can-i --list` shows
  only `get/list/watch` within its tier scope; every write and every cross-scope read returns **no**.
- **No ambient write creds:** the pod has no kubeconfig / GCP ADC for writes; the metadata server is
  **not** reachable for a broader token (egress test).
- **Controller mints no RBAC:** the controller's own RBAC has no verb granting `roles`/`rolebindings`
  create on tenant scopes; agent KSA/RBAC exist only as pre-created manifests in the repo (grep + audit).
- **Cron under the same SA:** a cron-triggered run reads within the tier scope and proposes changes via
  PR — never a direct write.
- **Trusted-human access (v1):** the entrypoint allowlist is enforced; there is no per-request user
  permission check (deferred, §5).
