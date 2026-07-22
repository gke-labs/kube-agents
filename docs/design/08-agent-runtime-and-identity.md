# Design 08: Agent Runtime & Identity (simple v1)

**Status:** ✅ Agreed — 2026-07-22

**Charter:** [README.md](README.md) · **Depends on:** [02](02-agent-personas.md),
[03](03-security-model.md), [04](04-workflow-model.md), [06](06-api-and-data-contracts.md) ·
**Tier:** Buildable (bridging)

---

## TL;DR

The simplest runtime that satisfies the multi-agent requirements: each persona is a **Hermes
profile** deployed as **its own pod with one read-only, tier-scoped ServiceAccount** (Workload
Identity). Agents are read-only; the only write path is a human-merged PR → the customer's CI/CD. Cron
runs in-pod under that same SA. Human requests are gated by an **in-agent `SubjectAccessReview` + IAM
check** (the simple form of [03](03-security-model.md) §4a). **No scope broker, no co-located
multiplexer, no per-run token exchange, no CLI credential shims** — those are deferred hardening (§5).
This deliberately prioritizes **simplicity over defense-in-depth**; the trade-offs are in §4.

---

## 1. What this doc decides

Runtime packaging, deployment topology, and identity for the personas in
[02](02-agent-personas.md) — i.e. _how each agent actually runs and authenticates_, at the simplest
bar that meets the requirements (tiered agents, per-agent scoped identity, cron, read-only + PR,
user-permission awareness).

## 2. The solution

1. **Persona = Hermes profile.** Each agent is one Hermes profile (`SOUL.md`, `config.yaml`, skills,
   cron, gateway). `--clone-from` templates the per-namespace Developer Team profile. Profiles are the
   **packaging format only** — not a security boundary.
2. **One pod, one profile, one SA.** The operator reconciles each `Agent` CR into a Deployment running
   one profile with **one read-only Kubernetes ServiceAccount + Workload Identity**, scoped to its
   tier (project / cluster / namespace, per [03](03-security-model.md) §3).
3. **Ambient identity is fine here.** Because the pod hosts exactly one profile and its SA is already
   least-privilege read-only, tools and CLIs (`kubectl`, `gcloud`) use the pod's **ambient** SA
   directly — no broker, no shim. They can only ever perform read-only, in-scope operations.
4. **Placement:** Platform → hub; Cluster Admin → its cluster; Developer Team → its namespace
   ([05](05-system-architecture.md) §3). Native in-cluster/cloud reads via the ambient SA.
5. **Mutation is read-only-agent → PR → customer CI/CD.** Agents emit **KCC YAML or Terraform HCL**,
   open a PR (Minty-brokered token), a human merges, and the **customer's CI/CD pipeline** applies it
   ([04](04-workflow-model.md), [06](06-api-and-data-contracts.md)). Agents hold **no write creds**.
6. **Cron runs in-pod under the same SA.** Hermes' per-profile cron fires read-only checks; anything
   it wants to change goes through the PR loop. No per-run tokens, no attestation, no separate
   identity — cron reads at the agent's own (read-only, tier-scoped) authority.
7. **Human-request authorization = trusted-human access + read-only ceiling.** The control is _who may
   reach an agent_ — authenticated chat + `AllowedUsers` + per-audience entrypoints; only trusted
   humans get in. v1 does **not** check the requester's own GCP/K8s permissions and does not union them
   with the agent SA — the agent's read-only, tier-scoped identity is the ceiling ([03](03-security-model.md)
   §4a). Per-request user-scoped authorization (the SAR/IAM check + down-scoping) is deferred (§5).
8. **Coordination is indirect** via the GitOps repo + OKF ([02](02-agent-personas.md) §2.3). No
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
- **One pod per agent + one least-privilege read-only SA** → tier/tenant isolation with **no
  shared-pod blast radius and no cross-tenant in-process leakage** (a Developer Team Agent cannot read
  another namespace; a Cluster Admin Agent cannot reach another cluster) ([03](03-security-model.md)
  §3–§4).
- **Trusted-human access** — only authenticated, allowlisted humans can reach an agent
  ([03](03-security-model.md) §4a). This, plus the read-only ceiling, is how the human→agent boundary
  is secured in v1.

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
- Use Hermes **profiles** as the persona-packaging format.
- Document the security trade-offs **honestly**, with an explicit upgrade path.

### Non-goals

- Broker / ephemeral-token infrastructure, co-location, or per-request credential enforcement (v1).
- Framework portability beyond the Hermes runtime ([02](02-agent-personas.md) §9).
