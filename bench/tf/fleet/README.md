# The seeded dirty fleet

Three small standing GKE clusters in `kube-agents-evals` whose defects are planted on
purpose: they are the fixtures the Phase 2 presubmit scenarios assert on. The fleet is
read-only for evaluations — every open pull request shares it, and no scenario may
mutate it. Because we planted each defect and chose its name, the scenarios' assertions
can be exact rather than judged.

The clusters carry `managed-by=kube-agents-seeded-fleet`, deliberately distinct from
`managed-by=kube-agents-bench`: the eval orphan sweep in `../modules/cluster/gke`
deletes bench-labeled clusters by age, and the standing fleet must never match it.

Drift is corrected by re-applying this stack on a schedule — a scheduled GitHub
workflow, because the repository's other recurring jobs already live there and the
apply needs nothing Cloud Build has that Actions lacks. The workflow does not exist
yet; creating it is the fleet owner's call. Until it does, a manual `tofu apply` after
any suspected drift is the reconcile. Two defects depend on the reconcile to stay
planted: GKE auto-upgrade heals `seeded-b`'s version lag, and any cleanup sweep is one
`orphan-pd-` deletion away from breaking the cost scenario.

| Defect                                                             | Where                                | Asserting scenario                                               |
| ------------------------------------------------------------------ | ------------------------------------ | ---------------------------------------------------------------- |
| `checkout-gateway`, single replica, no PDB                         | `seeded-a` / ns `seeded-reliability` | `obtainability-planted-pdb`                                      |
| `debug-binding`, cluster-admin to default SA                       | `seeded-a` / ns `seeded-security`    | `compliance-rbac-overgrant`                                      |
| `payments-api`, deterministic OOM crashloop                        | `seeded-a` / ns `seeded-debug`       | `cluster-agent-crashloop-debug`, `issue-resolver-triage` fixture |
| `pinned-inference-pool`, one zone, autoscaler at max, HPA wants 10 | `seeded-a` / ns `seeded-capacity`    | `stockout-pinned-pool`                                           |
| `idle-batch-pool`, zero non-system pods                            | `seeded-a`                           | `fleet-cost-idle-pool`                                           |
| `orphan-pd-1`, `orphan-pd-2`, unattached disks                     | project, zone `us-central1-a`        | `fleet-cost-idle-pool`                                           |
| Control plane one minor behind REGULAR default                     | `seeded-b`                           | `upgrade-readiness-lagging-cluster`                              |
| Workload logging off (peers have it on)                            | `seeded-c`                           | `consistency-drift-outlier`                                      |

`seeded-b`'s lag is derived at apply time (REGULAR channel default minus one minor,
freshest patch of that minor), so the pin re-computes each reconcile instead of
rotting. The chat-routing and incident-triage scenarios need no planted defect; the
silence-on-a-clean-fleet case needs a clean view, which is an open fleet-design
decision recorded in `../../tasks/DRAFTS.md`.
