# Design 04: Workflow Model

**Status:** ✅ Agreed

**Overview:** [README.md](README.md) · **Depends on:** [01-vision-scope.md](01-vision-scope.md),
[02-agent-personas.md](02-agent-personas.md), [03-security-model.md](03-security-model.md)

---

## TL;DR

Every change in `kube-agents` follows one loop: **an agent proposes a declarative change → a human
approves it (PR merge) → the customer's CI/CD applies it (and the kube-agents controller reconciles agents).** Agents never mutate
infrastructure directly, and **no change reaches a cluster without a human approving the merge —
there is no auto-merge for any tier.** "Autonomy" governs how the agent **proposes**, not whether a
human approves: the agent authors and opens PRs proactively for reversible, in-scope work; for
destructive, irreversible, cross-scope, or high-sensitivity actions it additionally **halts and flags
for the specific tier authority** (§2.2) — regardless of the agent's confidence. Either way, a human
merges.

Proactivity is **push-driven wherever possible**: agents react to **events** — Kubernetes
watches/informers, alerts (Cloud Monitoring/Alertmanager → Pub/Sub), and webhooks — and use **cron**
for genuinely scheduled work; a **heartbeat poll is only the backstop** for drift no trigger caught.
**Push triggers are preferred over polling throughout** (§4) — a poll lags a fast-moving problem and
wastes cycles when nothing changed. Either way, any resulting change still flows through the one loop.
Blockers are handled by a bounded **recovery ladder** before any human escalation. Because each agent
is an independent, controller-reconciled pod, tiers **fail in isolation**, not in cascade.

This doc resolves the deferrals from [02](02-agent-personas.md) (approval authority, failure
isolation) and [03](03-security-model.md) (where the review suite gates, prompt-injection hard
gates).

---

## 1. The core loop: propose → review → reconcile

```
Intent (human chat, event trigger, cron, heartbeat, or escalation)
        │        ← v1: human intent comes only from trusted, allowlisted humans
        ▼           (authenticated entrypoint); no per-request permission check (§2.4, [03] §4a)
  Agent authors a DECLARATIVE change     ← never a direct kubectl/console mutation
   (KCC YAML or Terraform HCL) on a branch  (bounded by the agent's read-only, tier-scoped ceiling)
        │  via `submit-suggestion` (GitHub PR)
        ▼
  REVIEW gate                            ← human approval and/or security-review suite (§3)
        │
        ▼
  CI/CD pipeline actuates merged state → actual  ← customer's pipeline (GitHub Actions /
        │                                            CircleCI / …) applies the artifact (§1.1);
        │                                            agents are READ-ONLY; only the pipeline
        │                                            (+ the controller, which reconciles agent pods) writes
        ▼
  Outcome reported back (human-readable) + audited (trace/session/requester)
```

This is mandated by `SOUL.md §1, §4`: agents are "strictly forbidden from executing direct, manual
cluster mutations." The `submit-suggestion` skill is the reference implementation of the "propose"
step (branch → stage _only_ targeted files → commit → PR); actuation is handled by whatever CI/CD the
customer already runs — the shape is the same.

Why this shape is load-bearing (from [03](03-security-model.md) §7): declarative changes are
**reviewable, attributable, revertible, and constrained** — so the workflow is itself a security
control, not just an operational convenience.

### 1.1 Reference implementation stack (unopinionated)

The loop is mechanism-agnostic. kube-agents provides the intelligence and the reviewed declarative
artifact; **it integrates with the customer's existing CI/CD and IaC rather than mandating a stack**:

| Concern                        | Mechanism                                                                                      | Notes                                                                                                                                      |
| ------------------------------ | ---------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------ |
| Shared source of truth         | **GitOps repository**                                                                          | agents propose PRs here                                                                                                                    |
| Provisioning artifact          | **KCC YAML or Terraform HCL** (per customer requirements)                                      | the agent generates whichever format the customer standardizes on                                                                          |
| Actuation (deploy + reconcile) | **the customer's CI/CD pipeline** (GitHub Actions / CircleCI / Jenkins / …)                    | applies the merged artifact (`kubectl apply`, `terraform apply`, …); kube-agents does not bundle a GitOps engine                           |
| Agent lifecycle                | **kube-agents controller** (`k8s-operator/`, extended)                                         | reconciles each `Agent` CR (Hermes harness) into an isolated pod; sets per-pod SA / namespace / runtimeClass on **Scion**'s verified model |
| Curated shared knowledge       | **OKF** (markdown+frontmatter in git)                                                          | ad-hoc wikis / tribal knowledge                                                                                                            |
| Semantic recall                | **mem0** (Qdrant) — _deferred post-v1_                                                         | —                                                                                                                                          |
| Session / runtime state        | **`session_db.sqlite` + `multiuser_memory`**                                                   | —                                                                                                                                          |
| Cross-agent coordination       | **shared state** (GitOps repo + OKF), reacted to via **event triggers**, heartbeat as backstop | **No direct agent-to-agent calls** — agents stay decoupled by design ([02](02-agent-personas.md) §2.3)                                     |

**Agents are read-only** on every cluster and cloud API; write permission lives only in the
**actuation pipeline** (plus the **kube-agents controller**, whose write is limited to reconciling agent
pods), which act solely on reviewed, merged state. Concretely:

- **Cluster provisioning** = a declarative artifact — a KCC `ContainerCluster` CR **or** a Terraform
  `google_container_cluster` resource — committed to the repo and applied by the pipeline
  (`kubectl apply` / `terraform apply`), **not** a direct `create_cluster` API call.
- **Workload / config deployment** = manifests (or Terraform) in the repo, applied by the pipeline —
  **not** a direct `kubectl` from the agent.

**Deliberately unopinionated:** kube-agents does not mandate or bundle any particular GitOps engine or
IaC controller. It emits the reviewed artifact (KCC YAML or Terraform HCL) and lets the customer's
existing CI/CD actuate it, so it drops into existing infrastructure. (This replaces today's
direct-mutation path, where agents call `create_cluster` / the `gke` MCP server directly.)

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

### 2.4 Who may drive an agent (v1: trusted-human access)

Approval authority (§2.3) governs _who signs off at merge_. The **v1** control on _who may ask an
agent to act at all_ is simple: **access is limited to trusted humans** — authenticated chat + an
explicit `AllowedUsers` allowlist + per-audience entrypoints ([02](02-agent-personas.md)). There is
**no per-request check of the requester's own permissions** and no down-scoping of the agent to them;
the agent is bounded by its **read-only, tier-scoped ceiling**, so no trusted human can drive it to
mutate or read outside its tier ([03](03-security-model.md) §4a). _How_ a trusted human addresses a
specific agent — direct handle, slash command, or NL routing through the `@kage` gateway — is
[02](02-agent-personas.md) §2.4; routing is a convenience, not an authorization signal
([03](03-security-model.md) §4a).

**Deferred hardening.** The delegate model — authorize each request against the requester's own GCP +
K8s permissions (`SubjectAccessReview` + IAM) and down-scope the agent to them, closing the
confused-deputy gap — is deferred to [08](08-agent-runtime-and-identity.md) §5 (contract sketch in
[06](06-api-and-data-contracts.md) §2a). Not in v1.

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

## 4. Proactive operations: push triggers first, polling as backstop

**Core concept — prefer push over poll.** Agents do not only react to chat, and they should not lean on
polling to notice things. Proactivity is driven, in order of preference:

1. **Event triggers (push, reactive)** — react the moment a signal arrives: **Kubernetes
   watches/informers**, **alerts** (Cloud Monitoring / Alertmanager → **Pub/Sub** or HTTP webhook), and
   **GitHub webhooks**. This is the default for anything a signal can represent.
2. **Cron (push, scheduled)** — for genuinely periodic work that no event represents (e.g. a weekly
   compliance audit). A scheduled fire is still a push, not a poll of "did anything change yet?".
3. **Heartbeat (poll, backstop only)** — a periodic sweep that catches drift **no trigger covered** and
   bounds worst-case detection latency. It is the last resort, not the primary mechanism, because a poll
   lags a fast-moving problem and burns cycles when nothing changed.

The Platform Agent already ships **10 governance jobs** (`agents/platform/cron/jobs.json`) mapped to SOPs
in `agents/platform/governance/`; these are the cron (scheduled-push) tier, and reactive concerns should
migrate to event triggers rather than new poll loops:

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

**How triggers are wired.** Event and cron triggers both fire through **Hermes event hooks**. Watches
run against resources the agent's read-only SA can already see (e.g. a crash-looping workload, a
NetworkPolicy or RBAC change); alert and GitHub webhooks arrive over Pub/Sub or HTTP. Crucially, a
trigger changes only _when_ an agent wakes, never _what_ it may do: every path — event, cron, or the
heartbeat backstop — still routes any resulting change through the same propose→review→reconcile loop
(§1), so the agent stays read-only and the change stays a human-merged PR (no auto-merge). Adding a
trigger is therefore a pure latency/efficiency win with no change to the trust model.

**End state — per-tier proactivity (scoped by persona responsibility).** Proactivity — whether fired by
an event trigger, cron, or the heartbeat backstop — exists at every layer, but each tier stewards **only
its own scope**. Fleet-only jobs stay at Platform; cluster/namespace concerns cascade down as scoped
subsets:

| Tier                           | Proactive jobs (scoped to its authority; event-triggered, cron, or heartbeat-swept)                                                                                                                      | Not run here (owned by a higher tier)                                                                                         |
| ------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------- |
| **Platform** (fleet)           | All 10 governance jobs above                                                                                                                                                                             | —                                                                                                                             |
| **Cluster Admin** (cluster)    | Cluster capacity / node health; security patch scan (its cluster); compliance audit (cluster-policy conformance); standardization validator (config vs. blueprint); deploy/drift detection (its cluster) | Policy propagation, lifecycle/deprecation, blueprint sync (authoring), fleet cost, obtainability audit, GitHub issue resolver |
| **Developer Team** (namespace) | Workload health / reliability; workload security posture; cost / right-sizing; drift detection — all **its namespace only**                                                                              | Everything cluster- and fleet-level                                                                                           |

Each tier's proactive work — however triggered — still routes any proposed change through the
propose→review→reconcile loop (§1) with a human merge — it never mutates directly, and never auto-merges.

---

## 5. Autonomous recovery: the recovery ladder

When execution hits a transient blocker (auth, IAM, identity, bootstrap), the agent follows the
bounded **Worker Recovery Ladder** (`SOUL.md §5`) before escalating:

1. Re-run / re-query to capture the exact failure.
2. Inspect identity context (SA annotations, Workload Identity, IAM bindings).
3. Inspect platform recovery mechanisms (the CI/CD pipeline run, cloud APIs, GKE Hub).
4. Apply an allowed self-repair (e.g. token refresh via `scripts/github_token_refresh.py`) — never
   a direct cluster mutation; repairs still route through the declarative workflow.
5. Re-run and resume the original task.
6. Escalate to a human only as last resort.

**Cap:** 5 iterations or ~10 minutes of wall time per distinct blocker. This keeps "loop until
done" from becoming "loop forever," and ensures a real permission boundary escalates promptly.

### 5.1 Reconcile-failure recovery (post-merge)

The ladder above covers blockers during the agent's own execution (**before** a PR merges). A
distinct case is an **actuation failure**: a PR is approved and merged, but the CI/CD pipeline fails
to apply the artifact (or the controller can't reconcile the agent pod). The agent is
read-only and only _observes_ the pipeline run + resource status ([05](05-system-architecture.md)
F2), so recovery is a **corrective-PR loop**, never a direct fix:

1. **Detect** — the proposing agent watches its PR's pipeline run + resource status; the tier
   heartbeat (§4) also catches failures that slip past.
2. **Diagnose** — read the pipeline run logs plus resource/pod **status + events**.
3. **Classify** transient vs. terminal:
   - **Transient** (quota, rate-limit, dependency-not-ready): defer to the pipeline's own
     retry/backoff — wait and re-observe; do **not** act (the agent must not fight a pipeline that is
     already retrying).
   - **Terminal** (invalid config, schema/policy rejection): author a **corrective PR** — a fix, or a
     **revert** of the offending change — through the normal human-merged loop (§1).
4. **Escalate** to a human at the cap.

**Cap:** a few heartbeat cycles (or ~equivalent wall time), mirroring §5's intent. Every correction
is a human-merged PR — never a direct cluster write, never an auto-merge.

---

## 6. Failure isolation across tiers

The parent→child relationship is one of **authority and lifecycle, not runtime dependency**. Each
agent is an independent, controller-reconciled pod with its own identity. Therefore:

| Failure                                  | Effect                                                                                                              | Recovery                                                                                           |
| ---------------------------------------- | ------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------- |
| A **Developer Team Agent** is down       | Only that namespace loses self-service; other namespaces unaffected                                                 | The controller relaunches the pod (Deployment self-heals); Cluster Admin Agent can re-propose it   |
| A **Cluster Admin Agent** is down        | New namespace provisioning in that cluster pauses; existing Developer Team Agents keep running (independent pods)   | The controller relaunches it; Platform Agent detects via heartbeat and re-provisions declaratively |
| The **Platform Agent** is down           | New cluster/fleet operations pause; running Cluster Admin & Developer Team agents keep operating within their scope | The controller relaunches the pod                                                                  |
| The **controller** (a cluster's) is down | No new agent reconciles in that cluster; running agent pods + workloads continue                                    | Controller restart (standard controller recovery)                                                  |

**Design intent:** no cascading failure. Because tiers don't call each other at runtime for their
core function — they're independent controller-reconciled pods bound by reviewed, merged manifests — an
outage at one layer degrades that layer's _new_ work, not the running state of the others.

> **Honest scoping — the hub is a shared-fate dependency for agent _reasoning_.** Inference (C5) and
> the GitHub token broker (Minty, C6) are hub-hosted shared services ([05](05-system-architecture.md)
> §3). If the **hub** is down, spoke **agents cannot reason (no inference) or propose changes (no
> brokered token)** — they pause. What survives is the **already-applied cluster state and running
> workloads** (Kubernetes keeps them running); and because actuation runs on the customer's CI/CD —
> **independent of the kube-agents hub** — an already-merged change can still deploy. So "spoke
> autonomy when the hub is down" means _the cluster keeps running its state_, **not** _the spoke agents
> keep operating_. True agent autonomy under hub loss would require regional/per-spoke inference —
> deliberately out of scope for v1 (a cost trade-off, see [05](05-system-architecture.md) §6).

---

## 7. End-to-end change lifecycle (worked example)

_Cluster admin asks their agent: "give team-payments a namespace with standard isolation."_

1. **Intent** — request arrives via the Cluster Admin Agent's chat entrypoint (authenticated user).
2. **Propose** — the agent authors declarative manifests (Namespace, RBAC, default-deny
   NetworkPolicy, ResourceQuota) and, if the team wants an agent, a **developer-team `Agent` CR + its
   read-only identity manifests** — on a branch via `submit-suggestion`.
3. **Review** — security-review suite runs (§3); because this creates a namespace + a lower-tier
   agent, it hits mandatory gates (§2.2): a **human cluster administrator approves** (§2.3).
4. **Actuate** — on merge, the **CI/CD pipeline** applies the namespace + the overlay-rendered
   **namespace-scoped read-only KSA/RBAC** (no runtime component mints it), and the **controller
   reconciles** the developer-team agent pod bound to that read-only SA. The attenuation ceiling is
   enforced by the in-tree `ValidatingAdmissionPolicy` (the cross-object webhook is deferred,
   [03](03-security-model.md) §4).
5. **Report & audit** — the agent reports outcome in human-readable form; trace/session/requester
   are recorded ([03](03-security-model.md) §5, `docs/designs/audit-logging-user-attribution.md`).

Every step is declarative, reviewed, attributable, and revertible.

---

## 8. Goals & non-goals

### Goals

- One universal change loop (propose → review → reconcile) for all agents and all tiers.
- Clear, unconditional gates for consequential actions; autonomy for safe, in-scope work.
- Human approval authority anchored at the tier that owns the blast radius.
- Proactive, **push-driven** stewardship at every layer (event triggers first, cron for scheduled work,
  heartbeat poll only as backstop), all changes via the loop.
- Bounded autonomous recovery; failure isolation without cascade — agents stay **decoupled**,
  coordinating only through shared state, never via direct agent-to-agent calls
  ([02](02-agent-personas.md) §2.3).

### Non-goals

- Prescribing one CI/CD or IaC tool — the loop is mechanism-agnostic and integrates with the
  customer's existing pipeline (GitHub Actions, CircleCI, Jenkins, Argo, Flux, …) and artifact format
  (KCC YAML or Terraform HCL).
- Redefining identity/RBAC internals (that is [03](03-security-model.md)).
- Specifying chat/UX details of approval prompts.

## 9. Verification

- **Only write path is a merged PR:** a direct cluster/cloud mutation with an agent identity **fails**;
  the same change via PR → merge → CI/CD **succeeds** and is attributed (trace/session/requester + PR
  URL).
- **No auto-merge:** no tier can merge its own PR; every merge requires a human (branch protection).
- **Mandatory gates:** a destructive / cross-scope / project-level change cannot merge without the
  tier's human owner approving (simulate; expect a required review).
- **Heartbeat via the loop:** a cron/heartbeat that wants a change opens a PR — never a direct mutation
  (assert no direct-write audit events from agent identities).
- **Reconcile-failure recovery:** inject a failing apply after merge → the agent opens a corrective /
  revert PR (§5.1), not a direct fix.
