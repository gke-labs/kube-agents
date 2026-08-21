# The seeded dirty fleet

Three small standing GKE clusters, one trio per eval project (`kube-agents-evals` and
`kube-agents-evals-2` — see State below), whose defects are planted on
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
creates and 409 against the live fleet. The stack applies **once per eval project**,
and each project keeps its own state: bucket `<project>-tf-state`, prefix
`seeded-fleet`, always. Both eval projects are live today —
`gs://kube-agents-evals-tf-state` and `gs://kube-agents-evals-2-tf-state` — and
project N+1 follows the same convention. The fleet owner creates the bucket once per
project; switching projects means re-initializing against that project's bucket and
naming the project on the apply:

    tofu init -reconfigure \
              -backend-config="bucket=<project>-tf-state" \
              -backend-config="prefix=seeded-fleet"
    tofu apply -var="project_id=<project>"

Local validation without credentials: `tofu init -backend=false && tofu validate`.

Drift is corrected by re-applying this stack on a schedule — a scheduled GitHub
workflow, because the repository's other recurring jobs already live there and the
apply needs nothing Cloud Build has that Actions lacks. The workflow does not exist
yet; creating it is the fleet owner's call. Until it does, a manual `tofu apply` after
any suspected drift is the reconcile.

The reconcile is load-bearing for `seeded-b` in particular: its version lag is held by
a `NO_MINOR_UPGRADES` maintenance exclusion whose window (90 days by default,
`var.exclusion_window_hours`) is re-stamped from now on every apply — the plan always
shows that one in-place update, by design; it is the window rolling forward, not
drift. The window has a hard ceiling the API enforces: an exclusion cannot outlive the
held minor's end of life (observed live: 1.34 capped at 2027-01-25), so as EOL
approaches, applies start failing with exactly that 400 — the built-in warning to
shorten the window variable or plan the re-lag. The exclusion therefore dies in one of
two ways — reconciles lapse for longer than the window, or the minor reaches EOL — and
either way GKE upgrades the master, the defect self-heals, and the upgrade scenario
going red is the detection. A control plane cannot be downgraded, so
the recovery is the same in both cases: replace the cluster and let the derived pin
re-lag it against the then-current default:
`tofu apply -replace=google_container_cluster.seeded_b`. The other standing hazard is
a cleanup sweep — one `orphan-pd-` deletion breaks the cost scenario, and recreating a
deleted fixture restarts its age gate (below).

## Activation timeline

The cost SOP's collectors are age-gated, so the fleet is not fully assertable on the
day it is applied. Recreating a fixture restarts its clock — `creationTimestamp` is
server-set and immutable, so backdating is impossible; do not try.

| Day  | What becomes detectable                                                                                                |
| ---- | ---------------------------------------------------------------------------------------------------------------------- |
| D0   | Everything except the cost fixtures: RBAC over-grant, missing PDB, OOM crashloop, stockout, version lag, drift outlier |
| D+7  | `idle-batch-pool` (the idle-nodepool check refuses pools created under 7 days ago)                                     |
| D+30 | `orphan-pd-*` (the unattached-disk collector filters `creationTimestamp<-P30D` server-side)                            |

The `fleet-cost-idle-pool` scenario must stay dormant until D+30, when both of its
fixtures are visible.

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
rotting — and `seeded-b` is enrolled in the REGULAR channel on purpose: the upgrade
SOP's master-behind check compares a cluster's master minor against its own channel's
default, so a channel-less cluster falls out of that comparison entirely and the lag
would be invisible to the audit it was planted for. The maintenance exclusion above is
what stops channel enrollment from healing the lag.

## Accepted background findings

Each audit of this fleet returns its planted finding **plus** the rows below —
nothing else, once the fleet is at steady state. Two conditions qualify that:
enrolling `seeded-a`/`seeded-c` in REGULAR can surface a transient upgrade 3.1
`master-behind` on them until GKE auto-upgrades their masters in the 03:00
window, and upgrade 3.3 `fleet-spread` is computed over **every** cluster the
audit reads in the project, not just the seeded trio — a `platform-agent-host`
or transient `eval-pr*` cluster running a minor ahead of the channel default
pushes project-wide spread to two and attaches an undeclared `minor` to
`seeded-b`. Neither breaks any scenario (the objectives are `report_contains`,
not exclusivity checks), but both make this table temporarily incomplete. Everything else the baseline used to trip is closed in the stack
itself (Workload Identity and `GKE_METADATA` everywhere, legacy metadata endpoints
disabled, `automountServiceAccountToken: false` and non-root-with-seccomp on all
planted workloads, default-deny NetworkPolicies in the three workload namespaces,
REGULAR channel and a maintenance window on every cluster, a PDB and soft spread
constraints where a non-fixture workload would otherwise trip the reliability
checks), precisely so this table stays short: the fixture's premise is that a
correct audit's findings are known in advance.

| Audit       | Check                                  | Where                       | Why it stays open                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| ----------- | -------------------------------------- | --------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| compliance  | 2.10 `public-control-plane` (critical) | all three clusters          | `seeded-a`/`seeded-b` carry a literal `0.0.0.0/0` authorized-networks block and `seeded-c` none — and `seeded-c`'s missing block IS the drift outlier, so it can never close. Closing a/b needs a named CIDR that still admits every caller with dynamic egress: Prow runners, the platform-agent pods that run the audits, and owner laptops. Until those have stable egress (reserved Cloud NAT IPs per project, or private endpoints plus internal runners), a narrow list would brick the fleet's own auditability. The matching trivy IDs (GCP-0053, GCP-0061) are ignored path-scoped in `.trivyignore.yaml` for the same reason |
| upgrades    | 3.10 `no-notifications` (minor)        | all three clusters          | Closing needs a Pub/Sub topic and notification config per project — real infrastructure for a minor visibility finding on a fleet whose upgrades are themselves fixtures                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| reliability | 3.10 `probes-liveness` (minor)         | the three planted workloads | `checkout-gateway` runs `pause`, which has no shell and listens on nothing, so no honest probe exists; the SOP itself calls a missing liveness probe "frequently the correct choice". Declared for all three rather than closed on two and left asymmetric                                                                                                                                                                                                                                                                                                                                                                             |

The drift and cost audits are fully declared with no background rows: the drift
cohort is confined to the three seeded clusters whose only surviving-severity
divergence is the planted authorized-networks outlier (the other base-critical
facets — private nodes, database encryption — are uniform, and everything
lower-severity is dropped by the ladder at r = 2/3), and the cost audit's only
findings are the two planted, age-gated fixtures (the right-sizing check's
reclaimable-delta floor sits far above these tiny pods). The upgrade audit's
remaining absolute checks are clean by construction: every cluster now has a
channel and a window, the fleet's minor spread is one (below the two-minor
threshold), the `NO_MINOR_UPGRADES` exclusion is the scope its 3.8 explicitly
does not flag, and pools run default auto-upgrade/auto-repair on COS_CONTAINERD.

Implication for the scenarios (`feat/domain-scenarios`): each objective must
assert the planted finding specifically — `debug-binding`, `cluster-admin` —
never "the audit found something", and judged prose should expect the declared
rows above to appear alongside the planted finding in their respective audits.

The chat-routing and incident-triage scenarios need no planted defect; the
silence-on-a-clean-fleet case needs a clean view, which is an open fleet-design
decision recorded with the scenario drafts. The silence case in particular must
tolerate the declared background rows above.

Rough standing cost: about $260 per month — the GKE management fee (three zonal
clusters) is most of it, the five small nodes (20 GB disks) and two 10 GB orphan disks
the rest.
