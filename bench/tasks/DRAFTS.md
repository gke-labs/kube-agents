# Phase 2 scenario drafts

Spec-ready `task.yaml` files for the ten domains in `testing-strategy.md` §4.2. Every one carries a live `verification_spec` (the Phase 1 verifiers exist) and is registered, commented out, in `hack/ci-eval-pr.sh`'s `TASKS` array. None runs yet: all ten read the standing seeded fleet (`bench/tf/fleet/`), and activation means applying the fleet, verifying each planted defect, and uncommenting the task's line.

Every draft carries in its header comment: the planted defect it needs, its isolation class per plan §5.1, and the tier that class makes it eligible for.

## The ten domains

| Domain            | Draft                                | Planted defect required                                                                                                   | Isolation                            | Eligible tier              | Status     |
| ----------------- | ------------------------------------ | ------------------------------------------------------------------------------------------------------------------------- | ------------------------------------ | -------------------------- | ---------- |
| Chat and routing  | `chat-routing-fleet-question/`       | none (deployed agents + seeded fleet)                                                                                     | read-only                            | presubmit                  | spec-ready |
| Reliability       | `obtainability-planted-pdb/`         | single-replica `checkout-gateway` in `seeded-reliability`, no PDB                                                         | read-only                            | presubmit                  | spec-ready |
| Capacity          | `stockout-pinned-pool/`              | `pinned-inference-pool`: one zone, autoscaler at max, HPA wants more                                                      | read-only                            | presubmit                  | spec-ready |
| Cost              | `fleet-cost-idle-pool/`              | `idle-batch-pool` with zero non-system pods; unattached `orphan-pd-*` disks                                               | read-only                            | presubmit                  | spec-ready |
| Security          | `compliance-rbac-overgrant/`         | `debug-binding` in `seeded-security`: cluster-admin to default SA                                                         | read-only                            | presubmit                  | spec-ready |
| Upgrades          | `upgrade-readiness-lagging-cluster/` | cluster B held one minor version behind its channel (reconcile must pin it)                                               | read-only                            | presubmit                  | spec-ready |
| Consistency       | `consistency-drift-outlier/`         | cluster C differs from its two peers on one facet (e.g. logging config)                                                   | read-only                            | presubmit                  | spec-ready |
| Remediation       | `rca-remediation-pr/`                | `payments-api` crashloop in `seeded-debug` (shared with cluster debugging); PR lands on the submit-suggestion GitOps repo | read-only cluster, GitOps-repo write | presubmit                  | spec-ready |
| Cluster debugging | `cluster-agent-crashloop-debug/`     | `payments-api` in `seeded-debug`: OOMKilled crashloop (shared with remediation)                                           | read-only                            | presubmit                  | spec-ready |
| Incident triage   | `autoops-warning-event-triage/`      | none standing — creates `eval-unschedulable` in a fresh namespace                                                         | **namespace-scoped**                 | presubmit, after plan §5.0 | spec-ready |

## The two cross-cutting failure cases

Not domains; failures every domain has to survive (strategy §4.2, last two table rows). Not drafted as task files yet because their shape is a parameterization of the ten above, decided when the verifiers exist:

- **Refusal** — asked to exceed its authority, the agent refuses and says what it refused. Applies to all ten. Likely one extra prompt variant per task with a `report_contains`/`tool_called` pair asserting the refusal happened and the action did not.
- **Silence on a clean fleet** — a scheduled run on an undefected fleet delivers nothing; the planted defect always is delivered. Applies to the seven scheduled audits: reliability, capacity, cost, security (both streams), upgrades, consistency. Needs either a second clean fleet or a clean namespace-view, which is a seeded-fleet design decision to make before Phase 2 assembly.

## Seeded-fleet shopping list (what the defects above imply)

Three clusters, not two — the consistency scenario has no majority and no outlier with two. Cluster A carries `seeded-reliability`, `seeded-security`, `seeded-debug` and the idle/orphan cost defects; cluster B is pinned one version behind; cluster C is the consistency outlier. The scheduled reconcile must re-pin B's version (GKE auto-upgrade heals that defect otherwise) and must not "fix" any planted defect.

## Contradictions found while drafting (strategy vs the SOPs)

1. **Consistency**: strategy says the drift audit "compares against the blueprint"; the SOP says there is no blueprint and the baseline is the live fleet majority. Drafted per the SOP. The strategy row needs rewording.
2. **Cost**: strategy says the audit "names the resource and the saving"; the SOP forbids dollar or percentage figures (no pricing data) and requires resource units. Drafted per the SOP, and the no-prices rule became an exact `forbidden_phrases: ["$"]` check.
3. **Remediation**: resolved by maintainer decision — the scenario was retargeted from github-issue-resolver (low-impact, files reports rather than PRs) to the high-impact PR-opening flow: an RCA chat prompt whose fix goes out through submit-suggestion. The strategy row now names those flows. The PR objective checks the final answer for the PR URL's `/pull/` segment, because submit_suggestion.py runs as a skill script through execute_code and records no distinct tool name.

## Verification-spec semantics the specs rely on

- `report_contains` checks the agent's final message; phrases are nouns the
  fleet plants, chosen defensively against substring collisions.
- `tool_called` sees only the delegating turn's calls — worker mutations are
  structurally invisible to it. Mutation safeguards are therefore
  cluster-state checks (`resource_property`: the planted defect object
  survived the run) where the defect is a Kubernetes object, and documented
  residuals where it is GKE-level (node pools, versions, disks, logging
  config), whose integrity the fleet's scheduled reconcile owns.
- Cluster names `seeded-b` (lagging) and `seeded-c` (outlier) are a contract
  with `bench/tf/fleet/`; the specs change with the stack if the names do.
