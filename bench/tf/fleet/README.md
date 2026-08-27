# The seeded dirty fleet

Three small standing GKE clusters, one trio per eval project — the stack is applied once
per project `gitops_repo_for_project()` in `hack/ci-deploy.sh` maps (see State below) —
whose defects are planted on purpose: they are the fixtures the Phase 2 presubmit
scenarios assert on.
Boskos leases a project at random, so **a project without this stack applied is a
project where every fleet check reports `status: "error"`**. The fleet is
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
`seeded-fleet`, always. Whether a given project's apply is complete is not recorded here,
because a list of project names goes stale silently: `scripts/verify_ci_pool_project.py`
runs `hack/fleet-kubeconfigs.sh` against the project and requires all seven fixture roles.
Project N+1 follows the same convention. The fleet owner creates the bucket once per project; switching projects means
re-initializing against that project's bucket and naming the project on the apply:

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

The reconcile is load-bearing for `seeded-b` in particular, and it does two distinct
things there. First, it **carries the control plane forward**: `min_master_version` is
not a creation-time floor — the field is neither `ForceNew` nor ignored, and the
provider answers a raised value with an operator-initiated `clusters.update` carrying
`desiredMasterVersion` (it upgrades only when the recorded master is lower, never
down). That is what keeps the pin from rotting: a new patch inside the held minor moves
the master onto it, and the day the REGULAR default rolls a minor, the derived pin
recomputes and the next apply walks the master to the new default-minus-one, so the lag
stays exactly one minor without anyone touching the file. `seeded-b`'s node pool is
driven from the same derived pin and deliberately **not** `ignore_changes`'d, so it
moves with the master; freezing it would leave the pool a minor (or, between minor
rolls, a patch) behind the control plane, which is upgrade SOP 3.2 `pool-skew` and a
finding this fleet never declared.

Second, it rolls the exclusion. The lag is held between reconciles by a
`NO_MINOR_UPGRADES` maintenance exclusion whose window (90 days by default,
`var.exclusion_window_hours`) is re-stamped from now on every apply — the plan always
shows that one in-place update, by design; it is the window rolling forward, not
drift. The exclusion gates GKE's _automatic_ upgrades only; manually initiated upgrades
(including the provider's) begin immediately and ignore maintenance policy, which is
why the two mechanisms do not fight. The window has a hard ceiling the API enforces: an
exclusion cannot outlive the held minor's end of life (observed live: 1.34 capped at
2027-01-25), so as EOL approaches, applies start failing with exactly that 400 — the
built-in warning to shorten the window variable or plan the re-lag. The exclusion therefore dies in one of
two ways — reconciles lapse for longer than the window, or the minor reaches EOL — and
either way GKE upgrades the master, the defect self-heals, and the upgrade scenario
going red is the detection. The pin cannot pull it back — the provider upgrades only
when the recorded master is below the configured value, and a control plane cannot be
downgraded — so the recovery is the same in both cases: replace the cluster and let the
derived pin re-lag it against the then-current default:
`tofu apply -replace=google_container_cluster.seeded_b`. That replacement costs the
drift scenario a day — see the cluster-replacement note under Activation timeline below.
The other standing hazard is a cleanup sweep — one `orphan-pd-` deletion breaks the cost
scenario, and recreating a deleted fixture restarts its age gate (below).

## Activation timeline

The cost SOP's collectors and the drift SOP's cohort rules are age-gated, so the fleet
is not fully assertable on the day it is applied. Recreating a fixture restarts its
clock — `creationTimestamp` is server-set and immutable, so backdating is impossible;
do not try.

| Day  | What becomes detectable                                                                                                                                                                                                                                                                                                             |
| ---- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| D0   | RBAC over-grant, missing PDB, OOM crashloop, stockout, version lag                                                                                                                                                                                                                                                                  |
| D+1  | The drift outlier. The drift SOP excludes a cluster whose `createTime` is under 24 hours old "from every cohort" (§1), so on apply day the `(standard, seeded)` cohort has zero members, and §2.4's floor ("a cohort of fewer than **3** clusters produces no findings, ever") would floor it out even if only one cluster were new |
| D+7  | `idle-batch-pool` (the idle-nodepool check refuses pools created under 7 days ago)                                                                                                                                                                                                                                                  |
| D+30 | `orphan-pd-*` (the unattached-disk collector filters `creationTimestamp<-P30D` server-side)                                                                                                                                                                                                                                         |

So `consistency-drift-outlier` must stay dormant until D+1, and `fleet-cost-idle-pool`
until D+30, when both of its fixtures are visible.

**Those two drift gates also govern cluster replacement.** Replacing any one of the
three — the documented recovery for `seeded-b`'s lag, and anything else that forces a
cluster — makes it new for 24 hours, which takes the comparable cohort from three
clusters to two and drops it under the §2.4 floor. The drift audit then emits nothing
for the **whole** fleet, not just the replaced cluster, so `consistency-drift-outlier`
goes red on every open pull request for a day. It is a clean, self-clearing outage
rather than a wrong answer (each member records the floor in its `limitations`, and no
finding is invented), but it is a day of red: schedule a replacement when the drift
scenario can be quiet, or expect and announce the gap.

## The defects

The scenario ids below are the contract of the in-flight Phase 2 scenario branch
(`feat/domain-scenarios`); the names here are the source of truth its specs assert on.

| Defect                                                                                                                                                                                                                          | Where                                | Fixture role         | Asserting scenario                                                                                                                                                                   |
| ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------ | -------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `checkout-gateway`, two replicas, no PDB (the SOP's no-pdb check flags multi-replica only)                                                                                                                                      | `seeded-a` / ns `seeded-reliability` | `no-pdb-workload`    | `obtainability-planted-pdb`, `cluster-agent-healthy-workload-no-finding`                                                                                                             |
| `debug-binding`, a cluster-scoped ClusterRoleBinding of cluster-admin to the `seeded-security` default SA (the compliance SOP reads ClusterRoleBindings only)                                                                   | `seeded-a`                           | `rbac-overgrant`     | `compliance-rbac-overgrant`                                                                                                                                                          |
| `payments-api`, deterministic OOM crashloop                                                                                                                                                                                     | `seeded-a` / ns `seeded-debug`       | `crashloop-workload` | `cluster-agent-crashloop-debug`, `cluster-agent-crashloop-fix-request`, `cluster-agent-crashloop-misleading-symptom`, `cluster-agent-crashloop-evidence-chain`, `rca-remediation-pr` |
| `pinned-inference-pool`: one zone, autoscaler pinned at one node, HPA wants more replicas than the pool can place, leaving a standing Pending backlog (no figure: the count is a load calculation and moves between projects)   | `seeded-a` / ns `seeded-capacity`    | `hpa-saturated`      | `stockout-pinned-pool`, `cluster-agent-pending-replicas-capped-pool`                                                                                                                 |
| `idle-batch-pool`, zero non-system pods (tainted so it stays that way)                                                                                                                                                          | `seeded-a`                           | `idle-nodepool`      | `fleet-cost-idle-pool`                                                                                                                                                               |
| `orphan-pd-1`, `orphan-pd-2`, unattached disks                                                                                                                                                                                  | project, `var.zone`                  | — (GCE-level)        | `fleet-cost-idle-pool`                                                                                                                                                               |
| Control plane one minor behind REGULAR default                                                                                                                                                                                  | `seeded-b`                           | `version-laggard`    | `upgrade-readiness-lagging-cluster`                                                                                                                                                  |
| Master authorized networks absent, normalized to OFF (peers run it ON with an open block, whose contents the drift SOP never compares); all three clusters carry `environment=seeded` so the drift cohort is exactly this fleet | `seeded-c`                           | `drift-outlier`      | `consistency-drift-outlier`                                                                                                                                                          |

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

## Addressing a fixture by role

The `Where` column above is documentation. **No scenario may name a cluster or a
project**, because every eval project carries its own trio of seeded clusters and the
pool of eval projects is meant to grow: a check that says `seeded-a` in
`kube-agents-evals` is a check that cannot run in `kube-agents-evals-2`. A scenario
names the **role** a fixture plays and the runner resolves it inside whichever project
the run leased.

`fixtures.json` in this directory is the catalog, and the only place role and cluster
meet. Each role gives a `cluster_slot` (`a`, `b` or `c`) and the namespace the fixture
lives in, if any. It deliberately carries **no** cluster name, prefix or location:
those are this Terraform's business, and a catalog that repeated them would agree with
reality only for as long as nobody applied the stack with a non-default
`-var cluster_prefix` or into another region — a drift that would surface as failing
checks rather than as an error in the runner. Planting a new defect means adding a role
here in the same change; a `bench/tests/test_fleet_verifier.py` test fails when a
`task.yaml` names a role the catalog lacks, or reads a namespace through a role the
catalog puts elsewhere.

The chain, end to end:

1. `hack/fleet-kubeconfigs.sh` **discovers** the trio in `$FLEET_PROJECT_ID` (defaulting
   to `PROJECT_ID`, the project the run leased) by filtering on the labels this stack
   applies — `environment=seeded` and `managed-by=kube-agents-seeded-fleet`, which
   nothing else in an eval project carries, not `platform-agent-host` and not the
   per-run `eval-pr-*` clusters. Each discovered name's trailing `-<slot>` segment says
   which slot it is. Two labelled clusters in one project whose names end in the same
   `-<slot>` make that slot ambiguous, and it is dropped rather than resolved by
   listing order. It then calls `gcloud container clusters get-credentials` once per
   slot and copies the result to `$BENCH_FLEET_KUBECONFIG_DIR/<role>.kubeconfig` for
   every role on that slot, writing only inside that directory and never touching the
   ambient kubeconfig. `hack/ci-eval-pr.sh` sources it after the host-cluster auth.
2. Before copying, it **reads every object in the role's `probes` list** on that
   cluster — `deployment/payments-api`, `clusterrolebinding/debug-binding`,
   `node?cloud.google.com/gke-nodepool=idle-batch-pool` — skips the role unless all are
   present, and writes the ones it saw to `<role>.confirmed`. A labelled cluster is not
   a planted fixture: an apply that created the clusters and stopped before the
   Kubernetes provider ran leaves a trio that answers every API call and holds none of
   the objects. Confirming presence here, before the agent runs, is what entitles the
   verifier to read an object that is gone at check time as a fixture the run destroyed
   rather than one that was never planted. It probes the **objects** and not merely the
   namespace because four of the seven roles are cluster-scoped and have no namespace:
   a namespace-only gate published them unconditionally, and `compliance-rbac-overgrant`
   then reported a catastrophic `fail` against an agent that had touched nothing.
   Adding a fixture therefore means adding both its role and its probes; every subject
   a `task.yaml` asserts on must appear in that list, which
   `bench/tests/test_fleet_verifier.py` enforces in both directions.
3. A check in a `task.yaml` uses the `fleet_resource_property` verifier and names
   `fixture_role: crashloop-workload`.
4. `kube_agents_bench.fleet.kubeconfig_for_role` turns the role into that path, and the
   verifier binds it to the check's `kubeconfig`.

A role that will not resolve — the stack was never applied in the leased project, its
apply stopped before planting that fixture, the runner never ran, or that cluster was
unreachable — is `status: "error"` naming the role and the project. It never falls back
to the ambient kubeconfig, which points at the agent's host cluster and carries no
fixture; that fallback was activation blocker A5 in `bench/tasks/DRAFTS.md`. See
[Addressing a seeded-fleet fixture by role](../../CUSTOM-TASKS.md#addressing-a-seeded-fleet-fixture-by-role)
for the spec side, including how the verifier keeps "the fixture is gone" (a fail)
apart from "the cluster was unreachable" (an error).

## The second consumer: the presubmit's log-fixture subject

`hack/ci-eval-pr.sh` §3b is the one consumer of this fleet outside the role catalog's
chain, and `fixtures.json`'s description names it as the exception. On every presubmit
in a fleet-carrying project it discovers **slot c** by the same two labels, verifies
its `default` namespace is empty, runs `get-credentials` against it, and hands its
name to the gpu-stress-test stack, which then creates no per-run cluster: the task's
synthetic `hypercomputer-agent`/`hpa-controller` Cloud Logging entries name the slot-c
cluster in their resource labels, on every run. The cluster itself is not mutated —
the entries are project-level and the stack's teardown removes only its fixture
resource — but two consequences are standing state this README must own: the agent
under test is pointed at slot c by name for the length of the eval, and any future
scenario that reads slot c's Cloud Logging history (a `consistency-drift-outlier`
investigation, say) will find those fixture entries attributed to it. Slot c carries
the fleet's only defect that is invisible to a log-analysis task, which is why it is
the only slot the presubmit may reuse; when it is absent or mid-maintenance, the
presubmit provisions its own cluster rather than borrowing slot a or b.

## A read-only credential for evaluations

An eval run reads this fleet to check its fixtures survived. It has no business being
able to change them, and the safeguards are worth less if the credential that checks
them could also have caused what it is checking for.

**This is not true today, and nothing in this change makes it true.** Measured, not
assumed: `prowjob-default-sa@kube-agents-prow.iam.gserviceaccount.com` — the identity
every presubmit runs as — holds `roles/container.admin`, `roles/container.developer`,
`roles/storage.admin`, `roles/resourcemanager.projectIamAdmin` and
`roles/iam.serviceAccountAdmin` in all three eval projects, and
`kubectl auth can-i delete deployments -n seeded-debug` answers yes. There are zero
ClusterRoleBindings or RoleBindings on these clusters naming any `*.gserviceaccount.com`
subject; authorization comes entirely from the GKE IAM webhook, so there is nothing to
narrow in-cluster either. A read-only identity does not exist to hand the harness yet.

What this change adds is the **seam**, so that closing the gap is a configuration
change rather than another code change. The stack provisions
`seeded-fleet-reader@<project>.iam.gserviceaccount.com` with `roles/container.viewer`
on the project and nothing else, and grants impersonation to the identities named in
`var.fleet_reader_token_creators` (a list of IAM members, empty by default) via
`roles/iam.serviceAccountTokenCreator` on that account alone. Set `FLEET_READONLY_SA` to
the account's email and `hack/fleet-kubeconfigs.sh` mints a token for it and writes each
kubeconfig with that token as its only credential. Unset — the state today — the script
warns loudly on every run and the kubeconfigs carry the runner's own identity.

Closing it needs three things, in order, none of them done here: apply this stack in
each eval project; add the Prow identity to `fleet_reader_token_creators` and re-apply;
export `FLEET_READONLY_SA=seeded-fleet-reader@<project>.iam.gserviceaccount.com` in the
Prow job. Then the property is checkable rather than asserted:

    gcloud auth print-access-token \
      --impersonate-service-account="seeded-fleet-reader@<project>.iam.gserviceaccount.com" \
      | xargs -I{} kubectl --token={} auth can-i delete deployments -n seeded-debug
    # must print: no

Three things about this are worth stating rather than assuming:

- **Impersonation must be a minted token, not a flag on `get-credentials`.**
  `gke-gcloud-auth-plugin` has no impersonation option, so the exec credential a
  `get-credentials --impersonate-service-account` writes still resolves to the caller's
  own identity at `kubectl` time. Only replacing the user entry with a minted access
  token actually binds it.
- **`roles/container.viewer` was verified, not assumed.** Its permission set contains no
  `container.secrets.*` and no create/update/delete/patch verb; the single non-get/list
  entry is `container.tokenReviews.create`. The bound worth remembering is lifetime: a
  minted access token lives one hour, which bounds a run, not a fleet. The script mints
  once, at the start; a run longer than the lifetime sees its fleet checks start
  erroring, which is loud and correct but is a real operational limit.
- **Never fold gcloud's stderr into the token.** On the _success_ path
  `gcloud auth print-access-token --impersonate-service-account=...` prints
  `WARNING: This command is using service account impersonation...` to stderr. Capturing
  it with `2>&1` yields a two-line blob that `kubectl config set-credentials` accepts
  without complaint, after which every API call 401s while the script reports success —
  a silent break of exactly the path this section recommends. The script captures stderr
  separately and rejects anything that is not a bare token.

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
threshold — which is also why `seeded-b`'s master is re-pinned forward rather
than frozen: a frozen master would fall two minors behind at the next REGULAR
roll and trip 3.3 `fleet-spread`), 3.2 `pool-skew` is clean because
`seeded-b`'s pool is driven from the same derived pin as its control plane
(any skew between them is the transient mid-reconcile lag 3.2 explicitly does
not flag), the `NO_MINOR_UPGRADES` exclusion is the scope its 3.8 explicitly
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
