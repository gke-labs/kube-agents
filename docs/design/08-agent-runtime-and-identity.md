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
7. **Human-request authorization = in-agent check-then-act.** Before acting on a human's behalf, the
   agent runs a `SubjectAccessReview` (K8s) + `testIamPermissions` (GCP) for **that requester** and
   refuses if unauthorized ([03](03-security-model.md) §4a). This is the **in-agent** realization of
   §4a; no separate gateway/broker in v1.
8. **Coordination is indirect** via the GitOps repo + OKF ([02](02-agent-personas.md) §2.3). No
   co-located multiplexer and no direct agent-to-agent messaging.

## 3. Deliberately out of scope (this is where the simplicity comes from)

None of the following are in v1 — each is additive and lives in the §5 hardening path:

- a **scope broker** / token-exchange service;
- **per-run ephemeral downscoped tokens** (interactive or cron);
- a **co-located multiplexer** (multiple profiles sharing one pod);
- **CLI credential shims** + metadata-server egress lockdown;
- **cron trigger attestation** (external scheduler + signed job manifests);
- the **external authorization gateway** as a separate component ([05](05-system-architecture.md)
  C14) — v1 folds its check into the agent (§2.7).

Every one of these existed to make **co-location** safe. v1 chooses **one pod per agent** instead, so
they are unnecessary.

## 4. Security considerations

### Held — the load-bearing invariants (unchanged from 01–07)

- **No direct mutation.** The only write path is a human-merged PR → the customer's CI/CD. Agents
  have no write RBAC and no write tools. _(The single most important property — fully retained.)_
- **Least-privilege, read-only identity per agent**, tier-scoped → tier/tenant isolation holds (a
  Developer Team Agent cannot read another namespace; a Cluster Admin Agent cannot reach another
  cluster).
- **One pod per agent** → no shared-pod blast radius and no cross-tenant in-process leakage.
- **Downward attenuation** via the RBAC template + admission ([03](03-security-model.md) §4) → no
  agent can obtain a broader SA than its tier.
- **Confused-deputy** is mitigated by the in-agent `SubjectAccessReview`/IAM check (§2.7) plus the
  human merge gate on every write.
- **Default-deny egress allowlist** ([03](03-security-model.md) §5) bounds exfiltration.

### Traded away — accepted for simplicity

- **User down-scoping is best-effort, not enforced.** The agent _checks_ the requester's permission,
  then reads under its own (broader, read-only) SA. A buggy or prompt-injected agent could read within
  its own tier scope beyond the specific requester's rights. _(The broker/ephemeral-token design would
  enforce this; v1 does not.)_
- **Standing credentials, not per-run ephemeral.** A compromised pod can use its read-only SA for the
  duration of the compromise, not just for one run.
- **Ambient credentials for CLIs and cron.** Safe here _only because_ pods are single-tenant and the
  SA is least-privilege read-only — this is precisely why co-location is excluded.
- **Higher pod count** (up to ~1 per namespace) — an operational/cost cost, not a security one.

### Residual risks & mitigations

| Risk | Bound / mitigation |
|------|--------------------|
| Compromised or injected agent | Reads only within its read-only tier scope; can open PRs but **cannot merge** (human gate); short-lived Minty tokens; audit; egress allowlist |
| In-agent user check bypassed by a compromised agent | Accepted; the human merge gate is the write backstop; reads stay bounded by the SA's scope |
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
