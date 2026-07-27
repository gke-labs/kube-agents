# kube-agents — Design

**Status:** Design complete. The documents in this directory are the authoritative, build-ready
specification of kube-agents' end-state architecture — sufficient for an engineer or an agentic
coding harness to build the product end-to-end.

kube-agents replaces the _human interface_ to Kubernetes and GKE — `kubectl`, `gcloud`, and the
Cloud Console — with a tier of **read-only AI agents** that operate infrastructure by **proposing
changes through GitOps**, never by mutating it directly. Three personas map onto the Kubernetes
containment hierarchy:

- **Platform Agent** — one per project.
- **Cluster Admin Agent** — one per cluster.
- **Developer Team Agent** — one per namespace.

They differ only in _read_ scope; none can write. Every change an agent makes is a **reviewed,
attributable, revertible pull request**, applied by the customer's own CI/CD pipeline. "Full
replacement" means replacing the human _interface_ — never human _approval_.

> **These docs describe the end state, not current code.** Some of it is built today (the Platform
> Agent); some is not (the Cluster Admin and Developer Team agents, the ChatOps router). Where a doc
> leads the implementation it says so and flags the delta; the design is the source of truth the code
> converges toward. [07](07-implementation-roadmap.md) sequences the migration from today to the end
> state.

---

## Core invariants

Load-bearing rules that hold across every persona, phase, and document. A change that violates one is
wrong even if it compiles and passes tests:

1. **Agents are read-only** on all cluster and cloud APIs. The only write path is a reviewed PR.
2. **All mutation flows through GitOps** — agent proposes → human approves → the customer's CI/CD
   applies the change (KCC YAML or Terraform HCL). No direct `kubectl`/`gcloud` writes; no
   break-glass path in v1.
3. **Agents never call each other directly.** They coordinate only through shared state — the GitOps
   repo and the Operational Knowledge Framework (OKF).
4. **Each tier is scope-bounded** (project / cluster / namespace), enforced by a per-agent read-only
   identity, not by convention.
5. **Every change is reviewed, attributable, and revertible.**

The security model (03) makes these enforceable; the roadmap (07) proves them with negative tests.

---

## The design set

Two tiers, meant to be read in order **01 → 08**:

- **Foundational (north star) — 01–04:** _what_ we are building and _why_.
- **Buildable (bridging) — 05–08:** _how_ it is assembled.

| #   | Document                                                       | Covers                                                                                                                                                                                                                                           |
| --- | -------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| 01  | [Vision & scope](01-vision-scope.md)                           | Project goals, the "replace kubectl/gcloud/console with agents" thesis, in/out of scope, success criteria                                                                                                                                        |
| 02  | [Agent personas](02-agent-personas.md)                         | The three-persona roster, roles and boundaries, cascading provisioning, read-only agents & indirect coordination, ChatOps addressing (3-mode routing: slash / handle / NL)                                                                       |
| 03  | [Security model](03-security-model.md)                         | Trust boundaries, per-tier identity & least-privilege, downward attenuation, trusted-human access + the read-only ceiling, AI-agent threats, the security-review suite as a control                                                              |
| 04  | [Workflow model](04-workflow-model.md)                         | The propose → review → reconcile loop, autonomy vs. mandatory gates, per-tier approval authority, push triggers & heartbeat, the recovery ladder, failure isolation                                                                              |
|     | _Foundational (north star) above · Buildable (bridging) below_ |                                                                                                                                                                                                                                                  |
| 05  | [System architecture](05-system-architecture.md)               | Component inventory (incl. the ChatOps gateway & router), hub-and-spoke topology, data flows, shared services, networking, scale/NFR targets                                                                                                     |
| 06  | [API & data contracts](06-api-and-data-contracts.md)           | The per-persona `Agent` CRD, the pre-created read-only identity contract, GitOps repo layout & IaC conventions (KCC or Terraform via customer CI/CD), OKF schema, the ChatOps routing contract, the review-gate contract, MCP tool changes       |
| 07  | [Implementation roadmap](07-implementation-roadmap.md)         | The phased build (current → end state), per-phase acceptance criteria, the verification loop, the definition of done, and risks                                                                                                                  |
| 08  | [Agent runtime & identity](08-agent-runtime-and-identity.md)   | The thin kube-agents controller (the extended `k8s-operator/`) reconciling each `Agent` CR (Hermes harness) into an isolated pod with a per-pod read-only Workload-Identity SA, on Scion's per-pod model; what is deferred as hardening, and why |

Each document opens with a **TL;DR** and carries a **Goals / Non-goals** section and a
**Verification** section of concrete, mostly-runnable checks.

---

## Building from these docs (for an engineer or agentic coding harness)

To build kube-agents end-to-end from this design set:

1. **Read 01 → 08 in order.** 01–04 give the intent and the invariants above; 05 the system to
   assemble; 06 the exact contracts; 07 the build sequence; 08 the runtime and identity model.
2. **Build by phase, verify, iterate.** Follow [07](07-implementation-roadmap.md) §2. After each
   phase, run its **acceptance criteria** _and_ the **Verification** checks of every spec the phase
   touched (02 §10, 03 §11, 04 §9, 05 §8, 06 §10, 08 §7). Do not advance a phase — or open the final
   PR — until its checks pass. The verification loop is defined in
   [07](07-implementation-roadmap.md) §5.
3. **Decisions are already made — don't re-litigate.** Every decision is stated in its home spec
   (01–06 and 08). If you hit something genuinely unspecified, pick the simplest option consistent
   with the invariants, implement it, and flag it in your PR.
4. **Honor the invariants even when they contradict current code** (the code is mid-migration). They
   are listed above and detailed in [03](03-security-model.md).
5. **Ground new code on existing patterns — don't invent structure.** New personas follow the
   Platform Agent's shape (`agents/platform/`: `SOUL.md` + `config.yaml` + `skills/` + governance
   SOPs), packaged as an `Agent` CR running the **Hermes** harness and reconciled by the **kube-agents
   controller** (the extended `k8s-operator/`). Per-agent identity is pre-created
   KSA / RBAC / Workload-Identity manifests the controller **references**, never mints; the review gate
   reuses the `.agents/skills/review-security-k8s-*` suite.
6. **Verification checks are load-bearing, not extras.** The two decisive suites are the **security
   negative tests** (03 §11 — read-only, per-tier scope, attenuation, no break-glass) and the
   **failure-isolation chaos tests** (05 §8). A build is not done until both are green.
7. **Definition of done** is the product-level acceptance in [07](07-implementation-roadmap.md) §3,
   which makes [01](01-vision-scope.md) §7 concrete.
8. **Produce changes the way the repo requires** — see `AGENTS.md` (Conventional Commits, PR
   template, format before commit, stage only targeted files).

**What these docs intentionally leave to the builder:** field-by-field API schemas beyond the
snippets in [06](06-api-and-data-contracts.md), per-skill implementation logic, and account-specific
values (project IDs, secrets). Derive these from the contracts in 06 and the repo patterns below.

---

## Key references (repo)

- **Platform Agent persona:** `agents/platform/` — `SOUL.md`, `config.yaml`, `skills/`, `governance/`
- **kube-agents controller** (the agent runtime; generalized per tier): `k8s-operator/`
- **Agent harness — Hermes:** [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent)
- **Per-pod runtime model — Scion:** [GoogleCloudPlatform/scion](https://github.com/GoogleCloudPlatform/scion)
- **Security-review skills:** `.agents/skills/review-security-k8s-*`
- **Reference implementation stack** (Hermes on the controller; KCC or Terraform via customer CI/CD;
  OKF): [04-workflow-model.md](04-workflow-model.md) §1.1
- **Glossary:** `docs/glossary.md`
- **Detailed feature designs:** `docs/designs/` (e.g. `audit-logging-user-attribution.md`)
- **Contribution mechanics:** `AGENTS.md`
- **Install prerequisites:** `INSTALL.md` — _predates the controller runtime; rewritten in roadmap
  Phase 1 ([07](07-implementation-roadmap.md))._
