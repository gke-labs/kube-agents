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
- **Buildable (bridging) — docs 05–07.** _How_ it is assembled: system architecture, API & data
  contracts, and a phased implementation roadmap with acceptance criteria. These translate the
  north star into something a builder can execute without guessing.

**Still out of scope:** the actual source code, per-skill low-level specs beyond the contracts in
06, and account-specific values (project IDs, secrets). Detailed feature designs (like
`docs/designs/audit-logging-user-attribution.md`) continue to live in `docs/designs/` and link back
here.

**Build-readiness bar:** a competent agent (or engineer) reading 01→07 should be able to build the
end state without needing an undocumented decision. Every decision is stated declaratively in its home
spec (01–06); 07 is sequencing only. Where the docs intentionally stop short of field-level detail
(exact Go API fields, per-skill logic, account-specific values), the builder grounds on the existing
repo patterns named in §6 and the contracts in 06 — see §8.

---

## 4. Design doc map

| #   | Document                                                           | Covers                                                                                                                                                                                                       | Status     |
| --- | ------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ---------- |
| —   | [README.md](README.md) (this charter)                              | Goal, workflow, map, how to resume                                                                                                                                                                           | **Living** |
| 01  | [01-vision-scope.md](01-vision-scope.md)                           | Project goals, the "replace kubectl/gcloud/console with agents" thesis, in/out of scope, success criteria                                                                                                    | ✅ Agreed  |
| 02  | [02-agent-personas.md](02-agent-personas.md)                       | Agent roster (Platform, Cluster Admin, Developer Team), roles, boundaries, cascading provisioning, read-only agents & indirect coordination                                                                  | ✅ Agreed  |
| 03  | [03-security-model.md](03-security-model.md)                       | Trust boundaries, per-tier identity/least-privilege, downward attenuation, **user-scoped authorization** (delegate, not amplifier), AI-agent threats, security-review suite as control                       | ✅ Agreed  |
| 04  | [04-workflow-model.md](04-workflow-model.md)                       | Propose→review→reconcile loop, autonomy vs. mandatory gates, per-tier approval authority, heartbeat, recovery ladder, failure isolation                                                                      | ✅ Agreed  |
|     | _**Foundational (north-star) above · Buildable (bridging) below**_ |                                                                                                                                                                                                              |            |
| 05  | [05-system-architecture.md](05-system-architecture.md)             | Component inventory (incl. authorization gateway), hub-and-spoke topology, data flows, shared services, networking, NFR/scale targets                                                                        | ✅ Agreed  |
| 06  | [06-api-and-data-contracts.md](06-api-and-data-contracts.md)       | Single tiered `Agent` CRD, identity-minting, user-authorization contract, GitOps repo layout + actuation/IaC conventions (KCC YAML or Terraform via customer CI/CD), OKF schema, session-state keys (mem0 deferred), review-gate contract, MCP tool changes | ✅ Agreed  |
| 07  | [07-implementation-roadmap.md](07-implementation-roadmap.md)       | Phased build (current→end state), per-phase acceptance criteria, definition of done, risks                                                                                                                   | ✅ Agreed  |

**Status legend:** ⬜ Not started · ✍️ Drafting · 👀 In review · ✅ Agreed · ♻️ Needs revisit

---

## 5. Conventions for these docs

- Each design doc opens with **Status**, a **TL;DR**, then numbered sections (mirrors the style of
  `docs/designs/audit-logging-user-attribution.md`).
- Every doc has a **Goals / Non-goals** section. Decisions are stated declaratively in the body — the
  docs are not a Q&A log.
- Cross-link freely: docs reference each other and the code they describe.
- When a doc records a decision that should change runtime behavior, note the target artifact
  (e.g. "update `agents/platform/SOUL.md §1`") so the follow-up is traceable.

---

## 6. Key references (current repo state)

- Platform Agent persona: `agents/platform/SOUL.md`
- Agent config & skills: `agents/platform/config.yaml`, `agents/platform/skills/`
- Governance SOPs: `agents/platform/governance/`
- Operator (CRDs, controllers): `k8s-operator/`
- Security-review skills: `.agents/skills/review-security-k8s-*`
- Existing feature designs: `docs/designs/`
- Glossary: `docs/glossary.md`
- Reference implementation stack (read-only agents; KCC YAML or Terraform HCL applied by the
  customer's CI/CD — unopinionated; OKF; mem0 deferred post-v1):
  [04-workflow-model.md](04-workflow-model.md) §1.1
- Contribution mechanics (Conventional Commits, fork-not-upstream, prettier, PR template): `AGENTS.md`
- Install prerequisites (cert-manager, Workload Identity; the customer's own CI/CD + IaC toolchain):
  `INSTALL.md`

---

## 7. Open questions (charter-level)

_Process/structure questions that affect the whole effort._

- None — all design decisions are captured declaratively in the specs (01–07).

---

## 8. Building from these docs (for an agentic coding harness)

If you are an agent (or engineer) tasked with building kube-agents end-to-end from this design set:

1. **Read in order 01 → 07.** 01–04 give you the intent and invariants; 05 gives you the system to
   assemble; 06 gives you the exact contracts to implement against; 07 gives you the sequence.
2. **Build by phase from [07-implementation-roadmap.md](07-implementation-roadmap.md).** Each phase
   has explicit **acceptance criteria** — do not advance until they pass.
3. **Decisions are already made — don't re-litigate.** Every design decision is stated in its home
   spec (01–06). If you hit something genuinely unspecified, pick the simplest option consistent with
   the invariants (item 4), implement it, and flag it in your PR. Building does not wait on debate.
4. **Honor the invariants** even when they contradict current code (the code is mid-migration):
   agents are **read-only**; **all** mutation flows through the GitOps loop; agents **never call
   each other directly**; each tier is **scope-bounded** (project/cluster/namespace); and every
   change is **reviewed, attributable, and revertible**.
5. **Definition of done** is the product-level acceptance in 07, which makes
   [01-vision-scope.md](01-vision-scope.md) §7 concrete.
6. **Ground new code on existing patterns — don't invent structure.** New personas follow the
   Platform Agent's shape (`agents/platform/`: `SOUL.md` + `config.yaml` + `skills/` + governance
   SOPs); new CRDs follow the `PlatformAgent` Kubebuilder pattern (`k8s-operator/api/v1alpha1/`,
   reusing the shared `AgentSpec`/`HarnessSpec`/`IntegrationSpec`); the review gate reuses the
   `.agents/skills/review-security-k8s-*` suite. 06 gives the contracts; the repo gives the shape.
7. **Prove each phase with tests — they are load-bearing, not extras.** The negative isolation test
   (Phase 3: an agent is _provably unable_ to read another scope or escalate) and the failure-
   isolation chaos tests (Phase 6: no cascade) are acceptance criteria; a phase is not done until
   they pass. These tests are how the security model (03) and failure isolation (04 §6) stop being
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

The design set (01–07) is complete, internally consistent, and build-ready — every decision is stated
in its home spec. Remaining work is logistics: push + PR the docs.

**Commit status:** `docs/design/` is committed on branch `docs/design-end-state-specs`, **not
pushed**. Opening a PR still requires: a fork remote (`AGENTS.md` forbids pushing to upstream),
running `prettier --write docs/design/` (npm registry auth blocked it locally), and the PR template.
