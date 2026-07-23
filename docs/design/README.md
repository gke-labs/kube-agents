# kube-agents Design Charter

**Status:** Living document — started 2026-07-21

**Purpose:** This is the entry point for the design-doc-driven engineering effort on
`kube-agents`. It persists _why_ we are doing this, _how_ we work together (human + coding
agent), the _map_ of design docs and their status, and _how to resume_ in a fresh session.

> **Resuming in a new session?** Point your agent here:
> _"Read `docs/design/README.md` and the linked design docs, then let's continue."_
> That reloads the goal, the workflow, and the current state of every design doc below.

---

## 1. Why this effort exists

`kube-agents` already has working parts — a Platform Agent (`agents/platform/SOUL.md`), ~20
skills, governance SOPs, a Go operator (`k8s-operator/`), and a security-review skill suite
(`.agents/skills/`). What it does **not** yet have is a single, agreed-upon articulation of the
intent behind those parts: the goals, the agent roster and their boundaries, the security/trust
model, and the workflow rules.

This effort produces that articulation as a small set of design documents. The docs serve two
audiences at once:

1. **Humans** — contributors and reviewers who need to understand the "why" before the "how".
2. **Agents** — the coding agents (and the runtime agents themselves) that read these docs as
   durable, authoritative context. Design decisions written here become the source of truth that
   `SOUL.md`, skills, and code should conform to.

The goal is to **define the end-state architecture** — the target we are building toward — and
resolve the open questions, so the project can be engineered collaboratively with agents against a
shared, written understanding. Some of what these docs describe is already built (e.g. the Platform
Agent); some is coming soon (e.g. the Cluster Admin and Developer Team agents). **These docs
describe the intended end state, not a snapshot of current state.** Where the design leads the
implementation, we note it briefly for traceability, but the design is the source of truth that
code converges toward — not the other way around.

---

## 2. How we work together (the agentic engineering loop)

We develop `kube-agents` design-first, in a human-in-the-loop loop with a coding agent:

1. **Frame** — Pick the next design doc (or a section of one) from the map in §4.
2. **Draft** — The agent drafts against the real repo state (reads code/config, never guesses),
   and surfaces genuine decisions rather than silently picking.
3. **Decide** — The human resolves open questions. Decisions land in the doc body as plain
   declarative design; anything genuinely unresolved is flagged inline until decided.
4. **Reconcile** — Where a decision contradicts current code/config, note the gap explicitly.
   Design docs may lead implementation; when they do, the gap is tracked, not hidden.
5. **Persist** — Update this charter's map (§4) with status, and record any durable process
   decisions here.

**Principles**

- **Docs lead, code follows.** When a doc and the code disagree, the doc states the intended end
  state and flags the delta. We do not quietly rewrite docs to match drifted code.
- **Ground every claim in the repo.** Reference real files (`path:line`), real skills, real CRDs.
- **Small, reviewable increments.** One doc (or one section) per pass, PR-sized.
- **Decisions are explicit.** A resolved question names the choice and the reasoning; an open one
  is parked visibly, not buried.

---

## 3. Scope of this effort

The design set has two tiers, and together they must be **sufficient to hand to an agentic coding
harness to build the product end-to-end**:

- **Foundational (north-star) — docs 01–04.** _What_ we are building and _why_: vision, personas,
  security model, workflow model. These define the end state and may lead the current code.
- **Buildable (bridging) — docs 05–08.** _How_ it is assembled: system architecture, API & data
  contracts, a phased implementation roadmap with acceptance criteria, and the agent runtime & identity
  model. These translate the north star into something a builder can execute without guessing.

**Still out of scope:** the actual source code, per-skill low-level specs beyond the contracts in
06, and account-specific values (project IDs, secrets). Detailed feature designs (like
`docs/designs/audit-logging-user-attribution.md`) continue to live in `docs/designs/` and link back
here.

**Build-readiness bar:** a competent agent (or engineer) reading 01→08 should be able to build the
end state without needing an undocumented decision. Every decision is stated declaratively in its home
spec (01–06 and 08); 07 is sequencing only. Where the docs intentionally stop short of field-level detail
(exact Go API fields, per-skill logic, account-specific values), the builder grounds on the existing
repo patterns named in §6 and the contracts in 06 — see §8.

---

## 4. Design doc map

| #   | Document                                                           | Covers                                                                                                                                                                                                       | Status     |
| --- | ------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ---------- |
| —   | [README.md](README.md) (this charter)                              | Goal, workflow, map, how to resume                                                                                                                                                                           | **Living** |
| 01  | [01-vision-scope.md](01-vision-scope.md)                           | Project goals, the "replace kubectl/gcloud/console with agents" thesis, in/out of scope, success criteria                                                                                                    | ✅ Agreed  |
| 02  | [02-agent-personas.md](02-agent-personas.md)                       | Agent roster (Platform, Cluster Admin, Developer Team), roles, boundaries, cascading provisioning, read-only agents & indirect coordination, **ChatOps addressing (3-mode routing: slash / handle / NL)**    | ✅ Agreed  |
| 03  | [03-security-model.md](03-security-model.md)                       | Trust boundaries, per-tier identity/least-privilege, downward attenuation, **trusted-human access + read-only agent ceiling** (user-scoped down-scoping deferred), AI-agent threats, security-review suite as control | ✅ Agreed  |
| 04  | [04-workflow-model.md](04-workflow-model.md)                       | Propose→review→reconcile loop, autonomy vs. mandatory gates, per-tier approval authority, heartbeat, recovery ladder, failure isolation                                                                      | ✅ Agreed  |
|     | _**Foundational (north-star) above · Buildable (bridging) below**_ |                                                                                                                                                                                                              |            |
| 05  | [05-system-architecture.md](05-system-architecture.md)             | Component inventory (incl. authorization gateway, **ChatOps gateway & router**), hub-and-spoke topology, data flows (incl. **chat ingress/routing F5**), shared services, networking, NFR/scale targets       | ✅ Agreed  |
| 06  | [06-api-and-data-contracts.md](06-api-and-data-contracts.md)       | Per-persona **`Agent` CRD** (running Hermes, reconciled by the kube-agents controller), identity contract (pre-created read-only KSA/RBAC/WI the controller references by name), user-authorization contract (deferred), GitOps repo layout + actuation/IaC conventions (KCC YAML or Terraform via customer CI/CD), OKF schema, session-state keys (mem0 deferred), **ChatOps addressing/routing contract**, review-gate contract, MCP tool changes | ✅ Agreed  |
| 07  | [07-implementation-roadmap.md](07-implementation-roadmap.md)       | Phased build (current→end state), per-phase acceptance criteria, **verification loop** (§5), definition of done, risks                                                                                        | ✅ Agreed  |
| 08  | [08-agent-runtime-and-identity.md](08-agent-runtime-and-identity.md) | **Runtime & identity:** a **thin kube-agents controller** (the extended `k8s-operator/`) reconciles each `Agent` CR (Hermes harness) into an isolated pod with a per-pod read-only tier-scoped SA (Workload Identity), on **Scion**'s verified per-pod model; trusted-human access + read-only ceiling; broker/co-location/ephemeral-tokens/user-check/cross-object-webhook deferred as hardening + security trade-offs | ✅ Agreed  |

**Status legend:** ⬜ Not started · ✍️ Drafting · 👀 In review · ✅ Agreed · ♻️ Needs revisit

---

## 5. Conventions for these docs

- Each design doc opens with **Status**, a **TL;DR**, then numbered sections (mirrors the style of
  `docs/designs/audit-logging-user-attribution.md`).
- Every doc has a **Goals / Non-goals** section and a **Verification** section (concrete, runnable
  checks a coding harness uses to validate the implementation). Decisions are stated declaratively in
  the body — the docs are not a Q&A log.
- Cross-link freely: docs reference each other and the code they describe.
- When a doc records a decision that should change runtime behavior, note the target artifact
  (e.g. "update `agents/platform/SOUL.md §1`") so the follow-up is traceable.

---

## 6. Key references (current repo state)

- Platform Agent persona: `agents/platform/SOUL.md`
- Agent config & skills: `agents/platform/config.yaml`, `agents/platform/skills/`
- Governance SOPs: `agents/platform/governance/`
- kube-agents controller (the agent runtime; extend/generalize for tiers): `k8s-operator/`
- Per-pod runtime model (reference): **Scion** — [GoogleCloudPlatform/scion](https://github.com/GoogleCloudPlatform/scion)
- Agent harness: **Hermes** — [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent)
- Security-review skills: `.agents/skills/review-security-k8s-*`
- Existing feature designs: `docs/designs/`
- Glossary: `docs/glossary.md`
- Reference implementation stack (read-only agents = Hermes on the kube-agents controller; KCC YAML or
  Terraform HCL applied by the customer's CI/CD — unopinionated; OKF; mem0 deferred post-v1):
  [04-workflow-model.md](04-workflow-model.md) §1.1
- Contribution mechanics (Conventional Commits, fork-not-upstream, prettier, PR template): `AGENTS.md`
- Install prerequisites (cert-manager for the controller webhook, Workload Identity; the customer's own
  CI/CD + IaC toolchain): `INSTALL.md` — _stale vs. the controller runtime; updated in Phase 1
  ([07](07-implementation-roadmap.md))._

---

## 7. Open questions (charter-level)

_Process/structure questions that affect the whole effort._

- None — all design decisions are captured declaratively in the specs (01–08).

---

## 8. Building from these docs (for an agentic coding harness)

If you are an agent (or engineer) tasked with building kube-agents end-to-end from this design set:

1. **Read in order 01 → 08.** 01–04 give you the intent and invariants; 05 gives you the system to
   assemble; 06 gives you the exact contracts; 07 gives you the sequence; 08 gives you the runtime
   (the kube-agents controller + Hermes, on Scion's model) and identity model.
2. **Build by phase, verify, iterate.** Build from
   [07-implementation-roadmap.md](07-implementation-roadmap.md) §2. After each phase, run its
   **acceptance criteria** _and_ the **Verification** checks of every spec that phase touched (each doc
   has a `## Verification` section: 02 §10, 03 §11, 04 §9, 05 §8, 06 §10, 08 §7). **Do not advance a
   phase — or open the final PR — until all its checks pass;** fix and re-run until green. The
   verification loop is defined in [07](07-implementation-roadmap.md) §5.
3. **Decisions are already made — don't re-litigate.** Every design decision is stated in its home
   spec (01–06 and 08). If you hit something genuinely unspecified, pick the simplest option consistent with
   the invariants (item 4), implement it, and flag it in your PR. Building does not wait on debate.
4. **Honor the invariants** even when they contradict current code (the code is mid-migration):
   agents are **read-only**; **all** mutation flows through the GitOps loop; agents **never call
   each other directly**; each tier is **scope-bounded** (project/cluster/namespace); and every
   change is **reviewed, attributable, and revertible**.
5. **Definition of done** is the product-level acceptance in 07, which makes
   [01-vision-scope.md](01-vision-scope.md) §7 concrete.
6. **Ground new code on existing patterns — don't invent structure.** New personas follow the
   Platform Agent's shape (`agents/platform/`: `SOUL.md` + `config.yaml` + `skills/` + governance
   SOPs), packaged as an **`Agent` CR** running the Hermes harness and reconciled by the **kube-agents
   controller** (the extended `k8s-operator/`; 06 §1, 08); per-agent identity is pre-created KSA/RBAC/WI
   manifests the controller **references** (never mints); the review gate reuses the
   `.agents/skills/review-security-k8s-*` suite. 06 gives the contracts; the repo + `k8s-operator/` give
   the shape.
7. **Prove each phase with the Verification checks — they are load-bearing, not extras.** Each spec's
   `## Verification` section lists concrete, mostly-runnable checks (many are **negative** tests). The
   two load-bearing suites are the **security negative tests** (03 §11 — read-only, per-tier scope,
   attenuation, no-break-glass) and the **failure-isolation chaos tests** (05 §8); a build is not done
   until both are green. This is how the security model (03) and failure isolation (04 §6) stop being
   aspirational.
8. **Produce changes the way the repo requires.** Your output is PRs: follow `AGENTS.md` —
   Conventional Commits, push to a **fork** (never upstream), run `prettier --write` before commit,
   use the PR template, and stage only targeted files (never `git add .`).

**What these docs intentionally leave to you:** field-by-field API schemas beyond the snippets in
[06](06-api-and-data-contracts.md), per-skill implementation logic, and account-specific values
(project IDs, secrets). Derive these from the contracts in 06 and the existing repo patterns in §6 —
the design fixes the decisions and interfaces, not every line of code.

---

## 9. Status

The design set (01–08) is complete, internally consistent, and build-ready — every decision is stated
in its home spec. Remaining work is logistics: push + PR the docs.

**Commit status:** `docs/design/` is committed on branch `docs/design-end-state-specs`, **not
pushed**. Opening a PR still requires: a fork remote (`AGENTS.md` forbids pushing to upstream),
running `prettier --write docs/design/` (npm registry auth blocked it locally), and the PR template.
