# The seeded dirty fleet

Three small standing GKE clusters in `kube-agents-evals` whose defects are planted on
purpose: they are the fixtures the Phase 2 presubmit scenarios assert on. The fleet is
read-only for evaluations — every open pull request shares it, and no scenario may
mutate it. Because we planted each defect and chose its name, the scenarios' assertions
can be exact rather than judged.

The clusters carry `managed-by=kube-agents-seeded-fleet`, deliberately distinct from
`managed-by=kube-agents-bench`: the eval orphan sweep in `../modules/cluster/gke`
deletes bench-labeled clusters by age, and the standing fleet must never match it.

## State and reconcile

State is remote (`backend "gcs"`, partial config), because the operating model is
re-apply from any checkout — against local state a fresh checkout would plan full
creates and 409 against the live fleet. The fleet owner creates the bucket once and
every apply names it:

    tofu init -backend-config="bucket=kube-agents-evals-tf-state" \
              -backend-config="prefix=seeded-fleet"

Local validation without credentials: `tofu init -backend=false && tofu validate`.

Drift is corrected by re-applying this stack on a schedule — a scheduled GitHub
workflow, because the repository's other recurring jobs already live there and the
apply needs nothing Cloud Build has that Actions lacks. The workflow does not exist
yet; creating it is the fleet owner's call. Until it does, a manual `tofu apply` after
any suspected drift is the reconcile. Two defects depend on vigilance to stay planted:
any cleanup sweep is one `orphan-pd-` deletion away from breaking the cost scenario,
and `seeded-b`'s version lag is held, not healable — a control plane cannot be
downgraded, so if GKE ever force-upgrades `seeded-b` past its pin (end of support),
the fix is replacing the cluster:
`tofu apply -replace=google_container_cluster.seeded_b`.

## The defects

The scenario ids below are the contract of the in-flight Phase 2 scenario branch
(`feat/domain-scenarios`); the names here are the source of truth its specs assert on.

| Defect                                                                                                                                                                                                                          | Where                                | Asserting scenario                                   |
| ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------ | ---------------------------------------------------- |
| `checkout-gateway`, two replicas, no PDB (the SOP's no-pdb check flags multi-replica only)                                                                                                                                      | `seeded-a` / ns `seeded-reliability` | `obtainability-planted-pdb`                          |
| `debug-binding`, a cluster-scoped ClusterRoleBinding of cluster-admin to the `seeded-security` default SA (the compliance SOP reads ClusterRoleBindings only)                                                                   | `seeded-a`                           | `compliance-rbac-overgrant`                          |
| `payments-api`, deterministic OOM crashloop                                                                                                                                                                                     | `seeded-a` / ns `seeded-debug`       | `cluster-agent-crashloop-debug`, remediation fixture |
| `pinned-inference-pool`: one zone, autoscaler at max, HPA settles at 3 with a standing Pending backlog                                                                                                                          | `seeded-a` / ns `seeded-capacity`    | `stockout-pinned-pool`                               |
| `idle-batch-pool`, zero non-system pods (tainted so it stays that way)                                                                                                                                                          | `seeded-a`                           | `fleet-cost-idle-pool`                               |
| `orphan-pd-1`, `orphan-pd-2`, unattached disks                                                                                                                                                                                  | project, `var.zone`                  | `fleet-cost-idle-pool`                               |
| Control plane one minor behind REGULAR default                                                                                                                                                                                  | `seeded-b`                           | `upgrade-readiness-lagging-cluster`                  |
| Master authorized networks absent, normalized to OFF (peers run it ON with an open block, whose contents the drift SOP never compares); all three clusters carry `environment=seeded` so the drift cohort is exactly this fleet | `seeded-c`                           | `consistency-drift-outlier`                          |

The `environment=seeded` resource label is the cohort confinement, the same class of
fixture-determinism as the pool taints: the drift SOP resolves environment from
`.resourceLabels.environment` before any name inference and keys cohorts on
(mode, environment), keeping unknown-environment clusters in their own cohort. Without
the label, `platform-agent-host` and any transient `eval-pr*` clusters would vote on
this fleet's baseline — a 2/2 authorized-networks split has no majority and no
finding, and churning eval clusters would randomize the audit run to run. The label
value `seeded` is reserved for these three clusters; labeling a fourth cluster with it
changes the vote.

`seeded-b`'s lag is derived at apply time (REGULAR channel default minus one minor,
freshest patch of that minor), so the pin re-computes each reconcile instead of
rotting. Note for the upgrade scenario: `seeded-b` runs with release channel
UNSPECIFIED, so it reports no channel of its own — the audit reads the available
versions (`valid_master_versions`), not a channel default, to see the lag.

The chat-routing and incident-triage scenarios need no planted defect; the
silence-on-a-clean-fleet case needs a clean view, which is an open fleet-design
decision recorded with the scenario drafts.

Rough standing cost: about $260 per month — the GKE management fee (three zonal
clusters) is most of it, the five small nodes (20 GB disks) and two 10 GB orphan disks
the rest.
