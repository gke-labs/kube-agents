# Design 08: Agent Runtime & Identity (simple v1)

**Status:** ✅ Agreed — 2026-07-22

**Charter:** [README.md](README.md) · **Depends on:** [02](02-agent-personas.md),
[03](03-security-model.md), [04](04-workflow-model.md), [06](06-api-and-data-contracts.md) ·
**Tier:** Buildable (bridging)

---

## TL;DR

The simplest runtime that satisfies the multi-agent requirements: each persona is a **Hermes** harness
launched by **[Scion](https://github.com/GoogleCloudPlatform/scion)** (Google's multi-agent
orchestrator) as **its own isolated pod with one read-only, tier-scoped ServiceAccount** (Workload
Identity). **Scion owns pod lifecycle, isolation, the hardened pod-security context, the optional
sandbox, and telemetry, and attaches the SA by name; we own creating that read-only KSA + RBAC + WI
binding** (via reviewed PR → CI). No custom Kubebuilder operator or CRD is needed — Scion supersedes
it. Agents are read-only; the only write path is a human-merged PR → the customer's CI/CD. Cron runs
in-pod under that same SA. The human→agent boundary is secured by **trusted-human access + the
read-only ceiling** — v1 does **not** check the requester's own permissions ([03](03-security-model.md)
§4a). No scope broker, no co-located multiplexer, no per-run token exchange, no CLI credential shims —
deferred hardening (§5). This deliberately prioritizes **simplicity over defense-in-depth**; trade-offs
in §4.

---

## 1. What this doc decides

Runtime packaging, deployment topology, and identity for the personas in
[02](02-agent-personas.md) — i.e. _how each agent actually runs and authenticates_, at the simplest
bar that meets the requirements (tiered agents, per-agent scoped identity, cron, read-only + PR,
user-permission awareness).

## 2. The solution

1. **Persona = a Scion agent template running the Hermes harness.** Each persona is a per-persona
   **Scion agent template** that launches the **Hermes** harness with that persona's profile
   (`SOUL.md`, skills, cron) and carries the pod's identity/placement fields. `--clone-from` (Hermes)
   templates the per-namespace Developer Team persona. The template + the identity manifests (item 3)
   are the **agent definition** — there is no custom CRD.
2. **Scion launches one isolated pod per agent.** [Scion](https://github.com/GoogleCloudPlatform/scion)
   runs each agent in its own pod, setting from the template: `kubernetes.serviceAccountName` (a
   pre-created read-only, tier-scoped KSA — Scion's field is built _for Workload Identity_),
   `kubernetes.namespace` (placement), an optional `kubernetes.runtimeClassName` (sandbox), and a
   hardened pod-security context (non-root, seccomp `RuntimeDefault`, no privilege-escalation) by
   default. **Scion supersedes the custom Kubebuilder operator** for agent lifecycle.
3. **We own the identity; Scion attaches it.** The per-agent KSA + read-only RBAC + Workload-Identity
   binding are pre-created via reviewed PR → CI ([06](06-api-and-data-contracts.md) §2), scoped to the
   tier (project / cluster / namespace, [03](03-security-model.md) §3). Scion references the KSA by
   name, so the pod runs as our **read-only, tier-scoped identity — the ceiling**.
4. **Ambient identity is fine here.** Because the pod hosts exactly one agent and the SA Scion attaches
   is already least-privilege read-only, tools and CLIs (`kubectl`, `gcloud`) use the pod's **ambient**
   SA directly — no broker, no shim. They can only ever perform read-only, in-scope operations.
5. **Placement:** Platform → hub; Cluster Admin → its cluster; Developer Team → its namespace
   ([05](05-system-architecture.md) §3), via the template's `kubernetes.namespace`. Native
   in-cluster/cloud reads through the ambient SA.
6. **Mutation is read-only-agent → PR → customer CI/CD.** Agents emit **KCC YAML or Terraform HCL**,
   open a PR (Minty-brokered token), a human merges, and the **customer's CI/CD pipeline** applies it
   ([04](04-workflow-model.md), [06](06-api-and-data-contracts.md)). Agents hold **no write creds**.
7. **Cron runs in-pod under the same SA.** Hermes' per-profile cron (Scion ticks it) fires read-only
   checks; anything it wants to change goes through the PR loop. No per-run tokens, no attestation, no
   separate identity — cron reads at the agent's own (read-only, tier-scoped) authority.
8. **Human-request authorization = trusted-human access + read-only ceiling.** The control is _who may
   reach an agent_ — authenticated chat + `AllowedUsers` + per-audience entrypoints; only trusted
   humans get in. v1 does **not** check the requester's own GCP/K8s permissions and does not union them
   with the agent SA — the agent's read-only, tier-scoped identity is the ceiling ([03](03-security-model.md)
   §4a). Per-request user-scoped authorization (the SAR/IAM check + down-scoping) is deferred (§5).
9. **Coordination is indirect** via the GitOps repo + OKF ([02](02-agent-personas.md) §2.3). No
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
- the **external authorization gateway** as a separate component ([05](05-system-architecture.md)
  C14).

Every one of these existed to make **co-location** safe. v1 chooses **one pod per agent** instead, so
they are unnecessary.

## 4. Security considerations

### Held — the load-bearing invariants

**All invariants of the security model ([03](03-security-model.md)) are retained** except per-request
user-scoped authorization, which v1 does not do (see below). Downward attenuation, the default-deny
egress allowlist, and the AI-agent defenses are unchanged; see 03. The guarantees this runtime shape
delivers:

- **No direct mutation** — the only write path is a human-merged PR → the customer's CI/CD; agents
  hold no write RBAC or write tools ([03](03-security-model.md) §7, [04](04-workflow-model.md)).
- **One agent per scope, one least-privilege read-only SA** (1 Platform/project, 1 Cluster-Admin/cluster,
  1 Dev-Team/namespace) → tier/tenant isolation with **no shared-pod blast radius and no cross-tenant
  in-process leakage**: a Developer Team Agent's pod **cannot read another namespace**, a Cluster Admin
  Agent's **cannot reach another cluster**, and a Platform Agent's **cannot reach another project**
  ([03](03-security-model.md) §3–§4).
- **Trusted-human access** — only authenticated, allowlisted humans can reach an agent
  ([03](03-security-model.md) §4a). This, plus the read-only ceiling, is how the human→agent boundary
  is secured in v1.
- **Hardened pod runtime (Scion-provided)** — Scion applies a restricted pod-security context by
  default (non-root, seccomp `RuntimeDefault`, no privilege-escalation) and can place untrusted/agent
  code in a **VM-isolated `runtimeClassName` sandbox** (satisfies [03](03-security-model.md) §5's
  control-loop/sandbox split), plus normalized OTel telemetry for attribution.

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
- **Higher pod count** (up to ~1 per namespace) — an operational/cost cost, not a security one.

### Residual risks & mitigations

| Risk | Bound / mitigation |
|------|--------------------|
| Compromised or injected agent | Reads only within its read-only tier scope; can open PRs but **cannot merge** (human gate); short-lived Minty tokens; audit; egress allowlist |
| A trusted human reads within the agent's tier scope beyond their own rights (confused deputy) | Accepted in v1; bounded by **only granting agent access to trusted humans** + the read-only ceiling; per-request down-scoping is the deferred fix (§5) |
| Cron self-triggered by a compromised pod | In-scope, read-only only; proposals still human-merged |
| Prompt injection | No mutation path (read-only + PR gate); cannot exceed SA scope; worst case is in-scope read exfil (egress-bounded) or a misleading PR (human-reviewed) |

## 5. Future hardening (only if/when needed)

If pod count (cost) or the best-effort user-down-scoping proves insufficient, layer on the model
explored during design (kept out of v1 for simplicity):

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
- Use **Scion** as the reference agent orchestrator/runtime and **Hermes** as the harness; the Scion
  agent template + Hermes profile are the persona-packaging format.
- Reuse Scion's per-pod `serviceAccountName`/`namespace`/`runtimeClassName` + hardened pod security
  rather than building a custom operator.
- Document the security trade-offs **honestly**, with an explicit upgrade path.

### Non-goals

- Broker / ephemeral-token infrastructure, co-location, or per-request credential enforcement (v1).
- Framework portability beyond the Hermes runtime ([02](02-agent-personas.md) §9).
