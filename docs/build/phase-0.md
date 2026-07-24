# Phase 0 — Foundations (task breakdown)

**Roadmap:** `docs/design/07-implementation-roadmap.md` §2 Phase 0.
**Goal:** repo layout + guardrails + a test cluster exist before behavior changes.

**Phase acceptance (07):**

- A1. Repo tree matches `06` §3.
- A2. The pre-created RBAC overlay + CI are the **only** RBAC path exercised in Phase 0 (controller
  not deployed to the negative-test cluster yet — removing its runtime RBAC-minting is Phase 1).
- A3. A deliberately-bad RBAC PR (agent write verb) is caught by human review **and**, if merged
  anyway, is **rejected at apply time by the `ValidatingAdmissionPolicy`** on the test cluster.
- A4. The **OKF validator script passes** on `knowledge/`.

**Touched Verification suites:** 03 §11 (attenuation admission), 06 §10 (contract/layout), 08 §7
(pre-created identity path). Load-bearing subset active this phase: **03 §11 attenuation**.

**Decision (spec-silent, recorded):** the "customer GitOps repo" is external in production, but for
the build we scaffold a **reference GitOps layout inside this repo** as the template customers fork.
Chosen path: `examples/gitops-repo/`. The Kind/scratch bootstrap lives in `local-dev/` (per AGENTS.md
and 07). Recorded in LEDGER Decisions.

---

## Tasks

| ID    | Task                                             | Implements          | Files (planned)                                                                                                                                                                                        | Acceptance signal                              | Status                                                                           |
| ----- | ------------------------------------------------ | ------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ---------------------------------------------- | -------------------------------------------------------------------------------- |
| P0-T1 | Scaffold GitOps repo layout                      | 06 §3               | `examples/gitops-repo/{clusters/<cluster>/{provisioning,namespaces/<ns>,agents},fleet,knowledge,policy,.github/workflows}` + READMEs                                                                   | A1 (tree matches 06 §3)                        | ✅ done                                                                          |
| P0-T2 | Scaffold OKF base                                | 06 §5               | `examples/gitops-repo/knowledge/{index.md, cluster-blueprint/standard-gke.md}` w/ `type` frontmatter + resolving links                                                                                 | A1, A4                                         | ✅ done                                                                          |
| P0-T3 | OKF validator script                             | 07 P0, 06 §5        | `local-dev/okf-validate.py` (or `scripts/`) — checks every `knowledge/**.md` has valid `type` frontmatter + all md links resolve; exit non-zero on failure                                             | A4 (passes on `knowledge/`)                    | ✅ done                                                                          |
| P0-T4 | Per-tier read-only RBAC render overlay           | 07 P0, 06 §2, 03 §4 | `examples/gitops-repo/policy/rbac-overlay/` — SA + Role/ClusterRole + RoleBinding per tier, stamped `kube-agents/tier` label; get/list/watch only                                                      | A2                                             | ✅ done                                                                          |
| P0-T5 | ValidatingAdmissionPolicy (attenuation backstop) | 03 §4, 03 §11       | `examples/gitops-repo/policy/vap-agent-readonly.yaml` — deny any Role/ClusterRole selected by `kube-agents/tier` whose own `rules` grant a write verb or wrong-scope; CEL scoped to the role's `rules` | A3, 03 §11 attenuation                         | ✅ done                                                                          |
| P0-T6 | Test-cluster bootstrap (Kind + scratch GKE)      | 07 P0               | `local-dev/kind/` (K8s ≥1.30 config + `up.sh`/`down.sh`), `local-dev/gke-scratch/` (create/destroy script)                                                                                             | cluster stands up; VAP test (P0-T5) runs on it | ✅ done                                                                          |
| P0-T7 | Branch protection / review gate config           | 07 P0, 06 §7        | `examples/gitops-repo/CODEOWNERS` + `.github/` ruleset doc requiring human review on `**/agents/**`, `**/namespaces/**`, `**/provisioning/**`, `**/policy/**`                                          | A3 (human-review half)                         | ✅ done (CODEOWNERS + ruleset doc; enabling on the real repo is a customer step) |

## Verification plan for this phase

1. **A1/tree:** compare `examples/gitops-repo/` tree to 06 §3 (script or manual diff).
2. **A4/OKF:** run P0-T3 validator on `knowledge/` → exit 0.
3. **A3/attenuation (03 §11, load-bearing):** on the Kind cluster, `kubectl apply` a Role granting an
   agent SA `create`/`update`/`delete` (and a cluster-scoped binding to a namespace-tier SA) → expect
   **denial** by the VAP. Adversarially confirm the denial is from the policy, not a malformed object.
4. **A2:** confirm no runtime RBAC-minting path is exercised (controller not deployed here); the
   overlay is the only RBAC applied.

## Pre-PR review gate (findings & fixes)

Before opening the Phase 0 PR the harness ran an adversarial verify→review gate (live acceptance
re-run on Kind + parallel security/invariant/completeness reviewers, each finding independently
confirmed). Acceptance A1–A4 stayed green; **5 of 17** findings survived confirmation, and the
in-scope defects were fixed and re-verified before the PR:

| Fix                                                                                                                                                                                                              | File                                      | Was                                                  | Now                                                                                                                                           |
| ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------- | ---------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------- |
| VAP read-only ceiling was a **write-verb deny-list** — `impersonate`/`bind`/`escalate` were admitted (a tier-labeled ClusterRole with `impersonate` → agent could act as `system:masters` = full cluster-admin). | `policy/vap-agent-readonly.yaml`          | deny `create/update/patch/delete/deletecollection/*` | **read-verb ALLOW-LIST**: every rule's verbs ⊆ `{get,list,watch}`; any write **or** escalation verb (and any future non-read verb) is denied. |
| Destructive-test guard used **unanchored substring globs** (`*scratch*`, `*kube-agents-dev*`) — a prod context like `gke_prod_…_kube-agents-dev-prod` slipped through.                                           | `local-dev/tests/negative-attenuation.sh` | `kind-*\|*scratch*\|*kube-agents-dev*`               | anchored `kind-*\|gke-scratch-*` (matches `up.sh` / `create.sh` naming). Verified: 3 prod-lookalike contexts now refused.                     |
| Negative-test cleanup read an **exhausted heredoc stdin** → admitted `good-reader` role leaked.                                                                                                                  | `local-dev/tests/negative-attenuation.sh` | `delete -f -` on empty stdin (silent no-op)          | manifest captured once, re-emitted for both apply and delete; verified `good-reader` is `NotFound` after the run.                             |
| OKF validator false-reported valid CommonMark links carrying a **title** (`[t](p.md "T")`) as broken.                                                                                                            | `local-dev/okf-validate.py`               | title/anchor treated as part of the path             | strips optional title + angle-bracket form before resolving; verified a titled link passes.                                                   |

New coverage: the attenuation test now includes an **`impersonate` ClusterRole → DENY** case that
exercises the allow-list fix (all 4 cases pass on Kind, denials adversarially confirmed from the policy).

## Deferred / recorded (real but out of Phase 0 scope)

Confirmed-real findings that the design defers to a later phase — recorded so the PR is honest about
what the v1 backstop does **not** yet cover:

- **Cross-object binding escape** — a `ClusterRoleBinding` binding a namespace-tier agent SA to a
  pre-existing write/`view` ClusterRole is not caught by the v1 VAP (it is scoped to a Role/ClusterRole's
  own `rules`, and does not evaluate bindings). This is the deferred cross-object **child⊆parent**
  admission check (07). Until then it is held by human review + CODEOWNERS on `/policy` and `/agents`.
  03 §11's "cluster-scoped binding to a namespace-tier SA" case is exercised in the test via a
  labeled wrong-scope ClusterRole proxy, not a binding.
- **Non-tier-labeled RBAC** escapes the VAP by design (the policy governs agent-tier RBAC only via
  `is-agent-rbac`); other RBAC is a separate governance concern handled by review.
- **`aggregationRule` ClusterRoles** (empty `.rules` at create, populated later) are admitted — same
  cross-object/temporal concern as above; deferred with the child⊆parent check.
- OKF reference-style links (`[t][ref]`) and a frontmatter-only file with no trailing newline are not
  validated/handled — documented limitations, not Phase 0 blockers.

## Notes / open items

- Real GitHub branch-protection is a repo _setting_, not code — P0-T7 delivers CODEOWNERS + a
  documented ruleset; enabling it on the actual GitOps repo is a customer/admin step (flag in PR).
- Scratch GKE incurs cost — only P0-T6's GKE path and identity-specific checks use it; Kind covers
  the rest.
