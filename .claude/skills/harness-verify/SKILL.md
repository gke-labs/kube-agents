---
name: harness-verify
description: Run the kube-agents verification suites for the current build phase — phase acceptance criteria plus the touched specs' Verification sections (02 §10, 03 §11, 04 §9, 05 §8, 06 §10, 08 §7) — on Kind or scratch GKE, and record results in docs/build/LEDGER.md. Use after implementing a task, before advancing a phase, or to check for regressions.
---

# harness-verify — run and log the verification suites

Runs the concrete checks the design set defines and records every result in the ledger's
**Verification log**. Never report "verified" without a command/log/PR as evidence.

## Inputs

- Current phase + task from `docs/build/LEDGER.md`.
- The phase's **Accept** bullets (`docs/design/07-implementation-roadmap.md` §2).
- The **Verification** sections of the specs the task touched. The full map:
  - 01 §8 — product acceptance (Definition of Done cross-check)
  - 02 §10 — persona/ChatOps routing checks
  - 03 §11 — **security negative tests (load-bearing)**
  - 04 §9 — workflow / propose-review-reconcile + push-trigger checks
  - 05 §8 — **failure-isolation chaos tests (load-bearing)**
  - 06 §10 — API/CRD/contract checks
  - 08 §7 — runtime/identity/pod-spec checks

## Targets

- **Kind** (default inner loop): SAR checks, `ValidatingAdmissionPolicy` denial tests, controller
  pod-spec assertions, chaos kills, config greps. K8s ≥1.30.
- **Scratch GKE** (ephemeral): criteria needing real Workload Identity / cloud IAM — e.g. the cloud
  GSA viewer-only assertion, real WI binding on the agent KSA. Tear the cluster down after.
- **Destructive-test guard:** confirm the kube context is Kind or a scratch GKE cluster before any
  delete/kill/bad-RBAC-apply. Otherwise **halt** (see `.claude/harness/invariants.md`).

## The two load-bearing suites (must be green before "done")

**Security negative tests — 03 §11** (mostly _expect failure_):

- **Read-only per tier (SAR):** for each agent SA,
  `kubectl auth can-i create|update|delete <res> --as=<agent-sa>` → **no** for every resource;
  `get|list|watch` → **yes** only within tier scope. Dev Team SA → **no** reads in other namespaces;
  Cluster Admin SA → **no** for other clusters; Platform SA → **no** for other projects.
- **No write tools:** grep the **operator-rendered** config (`renderConfigYAML()` / mounted ConfigMap,
  not just the baked `agents/platform/config.yaml`) — no `create_cluster`, `gke` MCP read-only,
  `apply_manifest`/`delete_cluster_manifest` removed.
- **Attenuation admission:** apply a `Role`/`ClusterRole` granting an agent SA a write verb (or a
  cluster-scoped binding to a namespace-tier SA) → **rejected** by the `ValidatingAdmissionPolicy`.
- **No break-glass:** a direct `kubectl apply` / cloud write with an agent identity → **forbidden**;
  the only successful mutation is a merged PR actuated by CI/CD.
- **Trusted-human access:** unauthenticated / non-`AllowedUsers` request (incl. via ChatOps slash /
  `@handle` / NL) → **refused**; the gateway checks the _target_ agent's allowlist before dispatch.
- **Egress default-deny:** from an agent pod, only allowlisted endpoints reachable; metadata server
  and arbitrary hosts **not**.

**Failure-isolation chaos — 05 §8 / 07 Phase 6:**

- Kill the hub → spoke workloads keep running (agents pause), resume on recovery.
- Kill the controller in a cluster → running agent pods continue, no new reconciles.
- Kill a Cluster Admin Agent → its Developer Team Agents keep running.
- Controller relaunches agent pods after pod kill.

## Procedure

1. Determine the suites in scope (phase Accept + touched specs).
2. For parallel speed, dispatch independent suites to subagents (Agent tool) — or run the optional
   `.claude/harness/verify-phase.workflow.js` workflow (needs explicit workflow opt-in). Each suite
   returns: suite id, target, PASS/FAIL, evidence (command + output snippet / PR link).
3. **Adversarially confirm** any negative test that "passed" — a check that silently no-ops reads as
   green. E.g. confirm the admission policy actually denied (non-zero apply), not that the manifest
   was malformed.
4. Append/update one row per suite in `LEDGER.md` **Verification log**. On any FAIL: do not advance;
   hand back to `harness-run` §5 to fix, or raise a **Blocker** if unresolvable.
