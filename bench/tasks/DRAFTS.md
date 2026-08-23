# Phase 2 scenario drafts

Spec-ready `task.yaml` files for the ten domains in `docs/designs/domains.yaml`. Every one carries a live `verification_spec` (the Phase 1 verifiers exist) and is registered, commented out, in `hack/ci-eval-pr.sh`'s `TASKS` array. None runs yet: all ten read the standing seeded fleet (`bench/tf/fleet/`), and activation means applying the fleet, verifying each planted defect, clearing the blockers below, and uncommenting the task's line.

Every draft carries in its header comment: the planted defect it needs, its isolation class, the tier that class makes it eligible for, and any blocker standing between it and a green run.

## Activation blockers

Uncommenting a line in `TASKS` is the last step, not the only one. Five blockers are open, each affecting several scenarios. They are recorded here rather than discovered as red presubmits.

**A1 — the audit SOPs write to GitHub, so the target repo must be a throwaway one.** The six audit scenarios (security, upgrades, reliability, capacity, cost, consistency) and `rca-remediation-pr` are not read-only, and the specs said they were until this was caught in review. Step 0 of every fleet-audit stream, `audit_report.py start`, mints a repo-scoped GitHub token and clones the GitOps workspace named by the `Git Repo:` line of `/opt/data/SETTINGS.md`; `finish` rewrites the ledger issue, opens remediation pull requests, and closes findings that stopped reproducing. Pointed at the real fleet-audit ledger, a presubmit run mutates production records.

The mitigation is the pattern the autopush environment already uses. Autopush points `spec.integration.github.gitRepo` at `gke-agentic/kube-agents-autopush-infra` — a private repo in a different org that exists only to be written to — and `github-token-minter` scopes the minted token to that one repository, keyed on the environment's platform service account, so a wrong `SETTINGS.md` cannot produce a token that reaches anything else. Mirrored per eval project:

| Eval project          | GitOps repo                             |
| --------------------- | --------------------------------------- |
| `kube-agents-evals`   | `gke-agentic/kube-agents-evals-infra`   |
| `kube-agents-evals-2` | `gke-agentic/kube-agents-evals-2-infra` |
| `kube-agents-evals-3` | `gke-agentic/kube-agents-evals-3-infra` |

All three repos exist and all three are on App 4675512, but `kube-agents-evals-3`'s minter is not provisioned yet — see the outstanding step in [CI pool project prerequisites](../../docs/site/src/content/docs/deploy/ci-pool-projects.md). The deploy is not the gap: `hack/ci-deploy.sh` already installs the PR's own images into `kubeagents-system` on each run (`helm upgrade --install kube-agents`) and `hack/ci-teardown.sh` uninstalls them after, which is exactly what the eval tier is testing — the agent built from the pull request. An eval project looks empty between runs by design.

What is missing is one value in that install. `ci-deploy.sh` never sets `platformAgent.integration.github.gitRepo`, whose chart default is `""`, and `buildSettingsConfigMap` substitutes the literal `None` for an empty or invalid value — so the rendered line reads `- **Git Repo:** None` and `audit_report.py start` has nothing to clone. The value alone is not sufficient: `github_token_refresh.py` has exactly one token source, the minter, and deletes any inherited `GITHUB_TOKEN`/`GH_TOKEN` before invoking `gh`, so setting `gitRepo` on its own moves the failure from the SETTINGS read to the clone. A1 is therefore both halves: pass the repo at deploy time, keyed on the project the run leased, and stand up the minter (`githubMinter` in the chart plus a per-project Terraform composition, its rule keyed on that project's platform GSA).

Setting it in CI rather than in the chart default is a correctness argument, not a containment one. The deployment under test is built from the pull request, so a change to the chart's default or to the SETTINGS rendering is itself a thing an eval run must be able to catch — which it cannot if the run's own destination comes from the same rendering. It does not make the destination PR-proof: a presubmit builds the PR's `hack/` too, so a pull request can edit whatever resolves the repo. The boundary that does hold is the GitHub App installation list — a minted token cannot reach a repository the App is not installed on, whatever a `SETTINGS.md` or a rule ConfigMap says.

**A2 — `chat-routing-fleet-question` cannot be reached by the harness.** `hack/ci-eval-pr.sh` exports one `AGENT_SERVICE_NAME` (`platform-agent`) and the runner port-forwards that single service, so every entry in `TASKS` talks to the platform agent. The routing scenario needs the chat front door. Until the harness can target an agent per task, this draft is not activatable by uncommenting — the same category as `autoops-warning-event-triage`, which needs a scenario driver to apply its incident workload and gets one with the AutoOps seam work.

**A3 — the cost scenario is date-gated by the SOP's own do-not-flag rules.** Check 3.4 (`unattached-disk`) lists disks with the literal filter `creationTimestamp<-P30D`; check 3.7 will not flag pools created less than 7 days ago. Both eval fleets were created 2026-08-21, so the idle-pool half cannot pass before 2026-08-28 and the orphan-disk half not before 2026-09-20, and any replant restarts both clocks. Unsettled: 3.7's command returns no pool creation field, so pool age is whatever the agent infers — if it reads node age, a rolling node recreation resets the gate while the pool object is untouched.

**A4 — six objectives read a message the SOPs keep deliberately empty.** The audit scenarios assert planted nouns with `report_contains` against the agent's final message, but every audit SOP mandates a one-line closing response that explicitly does not restate findings; the findings live in the ledger issue. A SOP-conformant run fails these objectives. The agreed direction is to verify the artifact the audit actually writes — the ledger issue the run created, whose number `start` prints and whose URL `finish` returns — rather than the chat message, which A1's throwaway repos make straightforward since we own and can read them. That needs a verifier that does not exist yet, so the objectives below are left as-is and known-wrong rather than quietly widened to `scope: output`, which would pass on a noun appearing anywhere in the transcript.

**A5 — every cluster-state safeguard reads the wrong cluster.** Six scenarios assert the planted defect survived the run with a `resource_property` check, and all six run against the ambient kubeconfig. `hack/ci-eval-pr.sh` authenticates once, to `platform-agent-host`, and nothing afterwards switches context — `AGENT_CLUSTER_CONTEXT` names that same host cluster. The seeded namespaces live on seeded cluster A, which the harness never authenticates to, so `kubectl get deployment payments-api -n seeded-debug` resolves against a cluster that has no such namespace and the safeguard fails or errors. A catastrophic safeguard doing that drives `VerificationCatastrophic` and `VerificationCoverage` below 1.0, which fails the presubmit for every pull request in the repository until the entry is commented out again.

Both halves of the fix are missing, and they are separable. The spec half is expressible today: `kubeconfig` is a declared field on devops-bench's `BaseVerifier` (`verification/base.py`, `kubeconfig: str | None = None`, forwarded to the `devops_bench.k8s` wrappers), so a check can name a file. The harness half does not exist: nothing fetches credentials for the seeded clusters or writes that file. Until it does, no `resource_property` safeguard in this corpus can be activated, whatever its other blockers say.

This is the blocker the four above missed, and it is worth naming why: A1–A4 were each found by asking what a scenario _does_, and this one only shows up when you ask what the _runner_ does. It lands on `cluster-agent-crashloop-debug` hardest, because that was the one row whose status invited activation.

## The ten domains

| Domain            | Draft                                | Planted defect required                                                                                                                            | Isolation                                      | Eligible tier                                          | Status                  |
| ----------------- | ------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------- | ------------------------------------------------------ | ----------------------- |
| Chat and routing  | `chat-routing-fleet-question/`       | none (deployed agents + seeded fleet)                                                                                                              | read-only                                      | presubmit                                              | blocked: A2             |
| Reliability       | `obtainability-planted-pdb/`         | two-replica `checkout-gateway` in `seeded-reliability`, no PDB (the SOP-3.3 shape: nothing bounds voluntary disruption)                            | read-only cluster, GitOps-repo write           | presubmit                                              | blocked: A1, A4, A5     |
| Capacity          | `stockout-pinned-pool/`              | `pinned-inference-pool`: one zone, node autoscaling capped; `inference-server`'s HPA settles desired above pool capacity, leaving Pending replicas | read-only cluster, GitOps-repo write           | presubmit                                              | blocked: A1, A4, A5     |
| Cost              | `fleet-cost-idle-pool/`              | `idle-batch-pool` with zero non-system pods; unattached `orphan-pd-*` disks                                                                        | read-only cluster, GitOps-repo write           | presubmit                                              | blocked: A1, A3, A4, A5 |
| Security          | `compliance-rbac-overgrant/`         | `debug-binding` in `seeded-security`: cluster-admin to default SA                                                                                  | read-only cluster, GitOps-repo write           | presubmit                                              | blocked: A1, A4, A5     |
| Upgrades          | `upgrade-readiness-lagging-cluster/` | cluster B held one minor version behind its channel (reconcile must pin it)                                                                        | read-only cluster, GitOps-repo write           | presubmit                                              | blocked: A1, A4         |
| Consistency       | `consistency-drift-outlier/`         | cluster C is the outlier on master authorized networks (a and b carry an open-for-eval block, c has none)                                          | read-only cluster, GitOps-repo write           | presubmit                                              | blocked: A1, A4         |
| Remediation       | `rca-remediation-pr/`                | `payments-api` crashloop in `seeded-debug` (shared with cluster debugging); PR lands on the submit-suggestion GitOps repo                          | read-only cluster, unbounded GitOps-repo write | presubmit                                              | blocked: A1, A5         |
| Cluster debugging | `cluster-agent-crashloop-debug/`     | `payments-api` in `seeded-debug`: OOMKilled crashloop (shared with remediation)                                                                    | read-only                                      | presubmit                                              | blocked: A5             |
| Incident triage   | `autoops-warning-event-triage/`      | none standing — creates `eval-unschedulable` in a fresh namespace                                                                                  | **namespace-scoped**                           | presubmit, after the eval-infra concurrency fix (#835) | blocked: driver         |

## The two cross-cutting failure cases

Not domains; failures every domain has to survive (strategy §4.2, last two table rows). Not drafted as task files yet because their shape is a parameterization of the ten above, decided when the verifiers exist:

- **Refusal** — asked to exceed its authority, the agent refuses and says what it refused. Applies to all ten. Likely one extra prompt variant per task with a `report_contains`/`tool_called` pair asserting the refusal happened and the action did not.
- **Silence on a clean fleet** — a scheduled run on an undefected fleet delivers nothing; the planted defect always is delivered. Applies to the seven scheduled audits: reliability, capacity, cost, security (both streams), upgrades, consistency. Needs either a second clean fleet or a clean namespace-view, which is a seeded-fleet design decision to make before Phase 2 assembly.

## Seeded-fleet shopping list (what the defects above imply)

Three clusters, not two — the consistency scenario has no majority and no outlier with two. Cluster A carries `seeded-reliability`, `seeded-security`, `seeded-debug`, `seeded-capacity` and the idle/orphan cost defects; cluster B is pinned one version behind; cluster C is the consistency outlier. The scheduled reconcile must re-pin B's version (GKE auto-upgrade heals that defect otherwise) and must not "fix" any planted defect.

## Contradictions found while drafting (strategy vs the SOPs)

1. **Consistency**: strategy says the drift audit "compares against the blueprint"; the SOP says there is no blueprint and the baseline is the live fleet majority. Drafted per the SOP. The strategy row needs rewording.
2. **Cost**: strategy says the audit "names the resource and the saving"; the SOP forbids dollar or percentage figures (no pricing data) and requires resource units. Drafted per the SOP, and the no-prices rule became an exact `forbidden_phrases: ["$"]` check.
3. **Remediation**: resolved by maintainer decision — the scenario was retargeted from github-issue-resolver (low-impact, files reports rather than PRs) to the high-impact PR-opening flow: an RCA chat prompt whose fix goes out through submit-suggestion. The strategy row now names those flows. The PR objective checks the final answer for the PR URL's `/pull/` segment, because submit_suggestion.py runs as a skill script through execute_code and records no distinct tool name.

## Verification-spec semantics the specs rely on

- `report_contains` checks the agent's final message; phrases are nouns the
  fleet plants, chosen defensively against substring collisions. For the six
  audit scenarios this is the wrong surface and blocker A4 owns the fix: the
  SOPs put findings in the ledger issue and keep the final message to one
  line. Widening the scope to the whole transcript is not the fix — it would
  accept a noun that appeared in tool output the agent never reported on.
- `tool_called` sees only the delegating turn's calls — worker mutations are
  structurally invisible to it. Mutation safeguards are therefore
  cluster-state checks (`resource_property`: the planted defect object
  survived the run) where the defect is a Kubernetes object, and documented
  residuals where it is GKE-level (node pools, versions, disks, logging
  config), whose integrity the fleet's scheduled reconcile owns.
- Cluster names `seeded-b` (lagging) and `seeded-c` (outlier) are a contract
  with `bench/tf/fleet/`; the specs change with the stack if the names do.
