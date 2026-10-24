# The seeded dirty fleet

Four small standing GKE clusters per eval project — the stack is applied once
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
runs `hack/fleet-kubeconfigs.sh` against the project and requires every fixture role the catalogue declares,
then runs `hack/fleet-fixture-state.py` and requires each of them to be in its **designed
state** — the `state` assertions beside each role in `fixtures.json` (the crashloop has a
recorded OOMKilled termination, `checkout-gateway` has two Ready replicas and no PDB in its
namespace, `seeded-b`'s master is still one minor behind its channel with the exclusion
window ahead of now). Presence alone passed on 2026-09-07 while every slot-a fixture sat
Pending (#1278); the state pass is what would have failed that project. A role on a cluster
it could not reach usually reports as unchecked rather than absent; the warnings that
override that default are in `docs/ci-pool-projects.md` §6.
Project N+1 follows the same convention. The fleet owner ([`docs/ownership.md`](../../../docs/ownership.md) says who) creates the bucket once per project; switching projects means
re-initializing against that project's bucket and naming the project on the apply:

    tofu init -reconfigure \
              -backend-config="bucket=<project>-tf-state" \
              -backend-config="prefix=seeded-fleet"
    tofu apply -var="project_id=<project>" -target=google_container_cluster.seeded_a
    tofu apply -var="project_id=<project>"

The first apply creates `seeded-a` alone, because the scaled-zero gateway fixture reads the Gateway
API CRDs that GKE installs with it; on a project that already has it, it changes nothing.

Local validation without credentials: `tofu init -backend=false && tofu validate`.

Drift is corrected by re-applying this stack with `hack/fleet_reconcile.py`, the fleet's only
writer: from `main`, under a Boskos lease per project, creates and in-place updates only, plus
the deletes and replaces `reconcile-allow.json` beside this file declares (one entry per
address, or per resource to cover every instance of a `count` or `for_each` one, with its
reason, reviewed in the pull request that needs it and removed by its follow-up; the no-surge
pool's replace on a minor roll and the scaled-zero gateway's status writer are the two standing ones). Its two Prow
entries in `oss-test-infra` run `main` only: a postsubmit on every merge that changes the stack's files
here, and a daily pass at 08:30 UTC (`docs/ci-pool-projects.md` §6.2, which says which files, and states
the rule for a fixture pull request and for proving a branch on one leased project). A hand
run goes through the script too (`--project <id>`), never `tofu apply` from a branch, and
never from a laptop while a run holds the project. The three hand recoveries (`seeded_b`'s
cluster, a targeted destroy; `inventory_api`, a replace; and `-replace=terraform_data.scaled_zero_status`,
which rewrites the scaled-zero gateway's status after an out-of-band edit the fixture-state scan reports)
are the exception: the script passes neither `-target` nor `-replace`, so they run by hand
from a `main` checkout, under a lease on the project. A recovery that re-creates `seeded_a`, and a project's first apply, run as two applies: `-target=google_container_cluster.seeded_a` first, then the whole stack, because the scaled-zero gateway fixture's `kubernetes_manifest` resources read the Gateway API CRDs at plan time and GKE installs them only with the cluster (`scripts/provision_ci_pool_project.sh` does this). A `default-pool-headroom` drift is the one no re-apply or hand recovery clears: it means seeded-a's default pool is within the 100m margin of full, and the repair is a larger `machine_type` or `node_count` on `seeded_a_default` in `main.tf`, merged, which the postsubmit then rolls project by project. The script's `init` runs with `-lockfile=readonly`, so the
providers are the ones `.terraform.lock.hcl` pins; to move them, change `versions.tf` if the major
changes and run `tofu providers lock -platform=linux_amd64 -platform=darwin_arm64 -platform=darwin_amd64`
here, and commit the result. A resource type that needs a project role the reconciler does not hold
brings the role in the same pull request and the grant before the merge (`docs/ci-pool-projects.md`
§3). Detecting the drift is a separate job, and
it is `hack/fleet-fixture-state.py`'s: the pool verifier runs it against one project
when asked, and the CI health bot's hourly scan runs it against every pool project and
reports a repeated drift the way it reports a lost build node
([`docs/ci-health.md`](../../../docs/ci-health.md), "The seeded-fleet scan"). The
presubmit does not run it and does not act on a drift: an eval
run's verdict is about the pull request, and skipping or excusing cases on the fleet's
account is deliberately not part of evals v1. The script's `--wait` exists for a
fixture that has just been rescheduled (the crashloop needs its first restart before
OOMKilled evidence exists, observed about 40 minutes behind the node repair on one
project in the #1278 retest sweep); a role still drifted at the deadline gets a
`<role>.drift` file beside its kubeconfig whose lines name the assertion and what was
observed, so the operator knows whether it is a `tofu apply` or a node.

The reconcile is load-bearing for `seeded-b` in particular, and it does two distinct
things there. First, it **carries the control plane forward**: `min_master_version` is
not a creation-time floor — the field is neither `ForceNew` nor ignored, and the
provider answers a raised value with an operator-initiated `clusters.update` carrying
`desiredMasterVersion` (it upgrades only when the recorded master is lower, never
down). That is what keeps the pin from rotting: a new patch inside the held minor moves
the master onto it, and the day the REGULAR default rolls a minor, the derived pin
recomputes and the next apply walks the master to the new default-minus-one, so the lag
stays one minor without anyone touching the file, a hold excepted. The pin is drawn from
REGULAR's own version list by `seeded_b_pin.py` (the stack's `external` data source),
never from the provider's all-channel list: GKE lets a cluster running a patch its
channel does not carry upgrade only within that minor, so a pin from a newer channel
turns the next minor roll into a refused apply on every project. When `seeded-b` is
found on such a patch, the script holds the pin instead, at the newest REGULAR patch of
that minor above the running one if there is one, else at the running version, and the
apply after that step does the minor bump, one minor per apply (GKE's control-plane
rule), so a master the hold left two minors behind reaches the target over two applies.
While a hold lasts the lag is two minors, not one: `hack/fleet-fixture-state.py` reads
`version-laggard` as drifted until REGULAR ships a patch above the running one, and if
REGULAR drops the minor first no pin can recover, so the cluster is upgraded by hand off
the channel or destroyed and re-created (the recovery below). `seeded-b`'s node pool is
driven from the same derived pin and deliberately **not** `ignore_changes`'d, so it
moves with the master; freezing it would leave the pool a minor (or, between minor
rolls, a patch) behind the control plane, which is upgrade SOP 3.2 `pool-skew` and a
finding this fleet never declared.

`seeded-b`'s `no-surge-pool` is never updated in place: its `version` is in
`lifecycle.ignore_changes`, and it is replaced when the pin's minor moves
(`replace_triggered_by` on a `terraform_data` that holds the minor). An in-place version
change drains its only node, `pinned-batch-runner`'s budget holds that drain for up to an
hour, and the provider's node-pool update timeout is thirty minutes, so tracking the pin
would run the reconcile into that timeout in every project on every patch roll.
Deleting a pool is different: GKE does not respect PodDisruptionBudgets on deletion unless
the pool opts in, so the replace takes minutes, the pool stays level with the control plane's
minor, and upgrade SOP 3.2 `pool-skew` stays clean. GKE applies patches within the held minor
on its own, and the readiness roles assert the pool's surge settings, not its version.
`hack/fleet_reconcile.py` applies that replacement because `reconcile-allow.json` lists the address, and refuses every undeclared one,
so a failed apply against `seeded-b` is a real error to read.

Second, it rolls the exclusion. The lag is held between reconciles by a
`NO_MINOR_UPGRADES` maintenance exclusion whose window (90 days by default,
`var.exclusion_window_hours`) is re-stamped from now on every apply — the plan always
shows that one in-place update, by design; it is the window rolling forward, not
drift. The exclusion gates GKE's _automatic_ upgrades only; manually initiated upgrades
(including the provider's) begin immediately and ignore maintenance policy. What gates
those is GKE's channel rule above, which is why the pin stays inside REGULAR's own
list. The window has a hard ceiling the API enforces: an
exclusion cannot outlive the held minor's end of life (observed live: 1.34 capped at
2027-01-25), so as EOL approaches, applies start failing with exactly that 400 — the
built-in warning to shorten the window variable or plan the re-lag. The exclusion therefore dies in one of
two ways — reconciles lapse for longer than the window, or the minor reaches EOL — and
either way GKE upgrades the master, the defect self-heals, and the upgrade scenario
going red is the detection. The pin cannot pull it back — the provider upgrades only
when the recorded master is below the configured value, and a control plane cannot be
downgraded — so the recovery is the same in both cases: destroy the cluster under a lease
(`tofu destroy -target=google_container_cluster.seeded_b`) and let the next apply, the
daily's or your own, re-create it at the lag target. Not a `-replace` in one apply:
`seeded_b_pin.py` reads the running cluster at plan time, so a replace plans the new
cluster at the version the old one is on and re-lags nothing. That re-creation costs the
drift scenario a day — see the cluster-replacement note under Activation timeline below.
The other standing hazard is a cleanup sweep — one `orphan-pd-` deletion breaks the cost
scenario, and recreating a deleted fixture restarts its age gate (below).

A third fixture that requires specific recovery is `stalled-controller` (`inventory-api` on
`seeded-a`). The defect is a missing ConfigMap (`inventory-flags`) resolved at container
start. If the ConfigMap is created during an in-cluster investigation and then deleted, the
running container continues to run and the Deployment keeps `Progressing=True` with reason
`NewReplicaSetAvailable` without spec drift. A spec-only re-apply plans no changes
(`PLAN_NO_CHANGES`). To restore the fixture, hand repair requires deleting the pod
(`kubectl delete pod -n seeded-stall -l app=inventory-api`), and if the Deployment's condition
reason remains `NewReplicaSetAvailable`, replacing the Deployment
(`tofu apply -replace=kubernetes_deployment_v1.inventory_api` or deleting the Deployment so apply
recreates it) rather than `kubectl rollout restart` (which writes
`kubectl.kubernetes.io/restartedAt` into the template and trips the
`the-rollout-was-not-restarted` safeguard).

The eleven upgrade-failure shapes on `seeded-a` (`node-local-state` to `cuda-pinned-gpu-job` in the
table below) need a fourth kind of recovery, which is not a repair. Each asserts a before-state (the
pod Running, the DaemonSet ready, the claim Bound) that a later node image, minor or registry
retirement is designed to end: `arch-pinned-worker` goes Pending at the first node rebuild after a
kubelet stops setting `beta.kubernetes.io/arch`, `legacy-registry-pull` goes `ImagePullBackOff` once
the `k8s.gcr.io` redirect is retired, and the two DaemonSets fail their `hostPath` type checks the day
an image moves the socket or `/etc/cni/net.d`. The condition ends in one GKE roll, so it ends on
every pool project within the same maintenance windows, and the signal is the hourly fixture-state
scan's `fixture_drift` condition on that role across projects (`docs/ci-health.md`): the catalogue's
after-state arriving, not a broken fleet, and nothing to put back by hand. The exit is one pull
request that retires the role: remove it from `fixtures.json` and `docs/designs/fleet-fixtures.yaml`,
delete its resources from `defects-a.tf` with a `reconcile-allow.json` entry per address (the
reconciler refuses a delete it does not list), record the after-state against the entry in
`docs/designs/upgrade-failure-reproductions.md` and move its catalogue row back to _designed_, and
park the cases that name the role in the same change: remove their lines from `hack/eval/nightly-cases.txt` and recount its budget paragraph, add their names to `PARKED_AFTER_THE_MOVE` in `scripts/test_eval_rosters.py`, and add each to `FIXTURE_NOT_READY` in `scripts/validate_bench_cases.py` with the retirement issue.

## Activation timeline

The cost SOP's collectors and the drift SOP's cohort rules are age-gated, so the fleet
is not fully assertable on the day it is applied. Recreating a fixture restarts its
clock — `creationTimestamp` is server-set and immutable, so backdating is impossible;
do not try.

| Day  | What becomes detectable                                                                                                                                                                                                                                                                                                                                                                                                                               |
| ---- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| D0   | RBAC over-grant, missing PDB, OOM crashloop, stockout, version lag, deprecated-API caller (its first-run Job completes inside the apply, so the audit trail starts on apply day), the four upgrade-readiness drain defects, the eleven upgrade-failure shapes (ten in `seeded-shapes`, one in `seeded-defaults`), the three zonal-skew fixtures, the `startup-fail` VM (its script fails on every boot, from the first), the world-open firewall rule |
| D+1  | The drift outlier. The drift SOP excludes a cluster whose `createTime` is under 24 hours old "from every cohort" (§1), so on apply day the `(standard, seeded)` cohort has zero members, and §2.4's floor ("a cohort of fewer than **3** clusters produces no findings, ever") would floor it out with any two of the four still new                                                                                                                  |
| D+7  | `idle-batch-pool` (the idle-nodepool check refuses pools created under 7 days ago)                                                                                                                                                                                                                                                                                                                                                                    |
| D+30 | `orphan-pd-*` (the unattached-disk collector filters `creationTimestamp<-P30D` server-side)                                                                                                                                                                                                                                                                                                                                                           |

So `consistency-drift-outlier` must stay dormant until D+1, and `fleet-cost-idle-pool`
until D+30, when both of its fixtures are visible.

**Those two drift gates also govern cluster replacement.** Replacing a cluster — the
documented recovery for `seeded-b`'s lag, and anything else that forces one — makes it
new for 24 hours, so it sits out the comparable cohort for a day. Four clusters leave
three, still at the §2.4 floor: replacing `seeded-a`, `seeded-b` or `seeded-d` leaves
`seeded-c` outvoted 2/3 and the finding stands. Replacing `seeded-c` removes the outlier
itself, and on a project whose fleet predates `seeded-d` any replacement drops the cohort
to two, under the floor; either way the drift audit has no finding for the **whole**
fleet, so `consistency-drift-outlier` goes red on every open pull request for a day. It
is a clean, self-clearing outage rather than a wrong answer (under the floor each member
records it in its `limitations`, and no finding is invented), but it is a day of red:
schedule such a replacement when the drift scenario can be quiet, or expect and announce
the gap.

## The defects

The scenario ids below are the contract of the in-flight Phase 2 scenario branch
(`feat/domain-scenarios`); the names here are the source of truth its specs assert on.

| Defect                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           | Where                                | Fixture role                    | Asserting scenario                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------ | ------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `checkout-gateway`, two replicas, no PDB (the SOP's no-pdb check flags multi-replica only)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       | `seeded-a` / ns `seeded-reliability` | `no-pdb-workload`               | `obtainability-planted-pdb`, `cluster-agent-healthy-workload-no-finding`, `cluster-agent-stalled-controller-healthy-silence`, `upgrades-network-policy-enforcement-stated` (slot presence and its namespace's policy), `upgrades-retrospective-network-policy-enforcement`, `upgrades-retrospective-log-volume-gate` and `upgrades-rollout-family-held-by-canary-gate` (slot presence only) (the same)                                                                                                                                                        |
| `notification-relay`, two replicas, no PDB, in a namespace with no NetworkPolicy, both declared on purpose by `knowledge/notification-relay-no-pdb.md` in the pool project's GitOps repository; provisioning seeds that note into a pool project's repository, and a dev project needs the same note (`GITOPS_INTENT_NOTE_CONTENT` in `scripts/provision_ci_pool_project.sh`; the path and its `declares` items are what the audit and the cases read) in whichever GitOps repository its install reads (a second such workload, so the declaration silences none of the checkout-gateway cases) | `seeded-a` / ns `seeded-intent`      | `declared-no-pdb-workload`      | `obtainability-declared-intent-no-finding`, `compliance-declared-intent-no-finding`                                                                                                                                                                                                                                                                                                                                                                                                                                                                           |
| `token-reader` and `token-sidecar`, one replica each on the `default` ServiceAccount with the token mounted; the same note declares `token-reader` on purpose and not `token-sidecar`, so the compliance audit keeps the sibling's fix off the shared ServiceAccount                                                                                                                                                                                                                                                                                                                             | `seeded-a` / ns `seeded-token`       | `declared-token-workload`       | `compliance-declared-token-shields-siblings`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| `burst-ingest`, two pause replicas requesting 10m / 128Mi each and using almost none of the memory; the same note declares the headroom on purpose, so the waste audit lists it under Declared intent instead of proposing a resize                                                                                                                                                                                                                                                                                                                                                              | `seeded-a` / ns `seeded-headroom`    | `declared-overrequest-workload` | `cost-declared-intent-no-finding`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| `ledger-writer`, two replicas with requests equal to limits (10m / 32Mi), so Guaranteed QoS; the container burns its 10m CPU limit, so no waste-audit check names it                                                                                                                                                                                                                                                                                                                                                                                                                             | `seeded-a` / ns `seeded-qos`         | `guaranteed-qos-workload`       | `cost-rightsizing-names-dependencies`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| `debug-binding`, a cluster-scoped ClusterRoleBinding of cluster-admin to the `seeded-security` default SA (the compliance SOP reads ClusterRoleBindings only)                                                                                                                                                                                                                                                                                                                                                                                                                                    | `seeded-a`                           | `rbac-overgrant`                | `compliance-rbac-overgrant`, `security-overgrant-probe`, `security-overgrant-remediation-proposal`                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| `payments-api`, deterministic OOM crashloop                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      | `seeded-a` / ns `seeded-debug`       | `crashloop-workload`            | `cluster-agent-crashloop-debug`, `cluster-agent-crashloop-fix-request`, `cluster-agent-crashloop-misleading-symptom`, `cluster-agent-crashloop-evidence-chain`, `rca-remediation-pr`                                                                                                                                                                                                                                                                                                                                                                          |
| `inventory-api`, dangling-reference ConfigMap, ProgressDeadlineExceeded                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          | `seeded-a` / ns `seeded-stall`       | `stalled-controller`            | `cluster-agent-stalled-controller-diagnosis`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| `pinned-inference-pool`: one zone, autoscaler pinned at one node, HPA wants more replicas than the pool can place, leaving a standing Pending backlog (no figure: the count is a load calculation and moves between projects)                                                                                                                                                                                                                                                                                                                                                                    | `seeded-a` / ns `seeded-capacity`    | `hpa-saturated`                 | `stockout-pinned-pool`, `cluster-agent-pending-replicas-capped-pool`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| `idle-batch-pool`, zero non-system pods (tainted so it stays that way)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           | `seeded-a`                           | `idle-nodepool`                 | `fleet-cost-idle-pool`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        |
| seeded-a's default pool itself, two e2-standard-2 nodes with CPU unrequested for the next planted fixture; the one role that reads capacity rather than a planted object, so its drift means the pool is within ten fixtures of full and a re-apply changes nothing                                                                                                                                                                                                                                                                                                                              | `seeded-a`                           | `default-pool-headroom`         | none: the scan's guard on the room the slot-a fixtures schedule into, not a case's fixture                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| `orphan-pd-1`, `orphan-pd-2`, unattached disks                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   | project, `var.zone`                  | — (GCE-level)                   | `fleet-cost-idle-pool`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        |
| `world-open-2379-<prefix>` opening tcp:2379 to the internet, and `world-open-<prefix>`, the one VM carrying its tag, with an external IP; nothing listens on 2379, and `world-open-deny-remote-<prefix>` blocks tcp:22 and tcp:3389                                                                                                                                                                                                                                                                                                                                                              | project, `var.zone`                  | — (GCE-level)                   | `networking-world-open-ingress`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| `startup-fail-<prefix>`, a standalone e2-micro whose startup script exits 1 on every boot                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        | project, `var.zone`                  | — (GCE-level)                   | `gce-startup-script-failure`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| Control plane one minor behind REGULAR default, held by the `hold-the-minor-lag` NO_MINOR_UPGRADES exclusion, whose end time every apply re-stamps                                                                                                                                                                                                                                                                                                                                                                                                                                               | `seeded-b`                           | `version-laggard`               | `upgrade-readiness-lagging-cluster`, `upgrades-lagging-master-probe`, `upgrades-fleet-version-table`, `upgrades-fleet-rollout-stall`, `upgrades-fleet-readiness-exclusion`, `upgrades-master-behind-offered-elsewhere`, `upgrades-audit-reports-upgrade-blocked`, `upgrades-fleet-readiness-failclosed-webhook`, `upgrades-exclusion-end-date-named`, `upgrades-removed-api-callers-distinguished` (slot presence only), `upgrades-network-policy-enforcement-stated` (slot presence only), `upgrades-zonal-control-plane-outage-warned` (slot presence only) |
| Master authorized networks absent, normalized to OFF (peers run it ON with an open block, whose contents the drift SOP never compares); every cluster in this stack carries `environment=seeded` so the drift cohort is exactly this fleet; seeded-d joins it as a fourth voter, which moves the agreement ratio from 2/3 to 3/4 and leaves the severity unchanged — main.tf's `seeded_d` block works the arithmetic                                                                                                                                                                             | `seeded-c`                           | `drift-outlier`                 | `consistency-drift-outlier`, `upgrades-removed-api-callers-distinguished` (slot presence only), `upgrades-network-policy-enforcement-stated` (slot presence only), `upgrades-zonal-control-plane-outage-warned` (slot presence only)                                                                                                                                                                                                                                                                                                                          |
| `legacy-endpoints-writer` CronJob patches Endpoints v1 (`legacy-endpoints-lane`) every ten minutes: deprecated and still served, so every write is audit-stamped `k8s.io/deprecated=true`, with no `k8s.io/removed-release` and no Recommender insight                                                                                                                                                                                                                                                                                                                                           | `seeded-a` / ns `seeded-deprecation` | `deprecated-api-caller`         | `upgrades-removed-api-callers-distinguished`, `upgrades-readiness-deprecated-api-callers`                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| `no-surge-pool`, `maxSurge 0` with `maxUnavailable 1`, so an upgrade takes its only node away rather than adding a replacement first (tainted so only the pinned workload lands there)                                                                                                                                                                                                                                                                                                                                                                                                           | `seeded-b`                           | `readiness-surge-blocked`       | `upgrades-no-surge-pool-recreate-warned`, `upgrades-readiness-surge-capacity`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 |
| `pinned-batch-runner`, one replica with a nodeSelector onto that pool, so a drain evicts it into Pending until the node is recreated (requests 250m / 256Mi, above the cost audit's 15% idle floor, so the pool is not an `idle-nodepool` finding)                                                                                                                                                                                                                                                                                                                                               | `seeded-b`                           | `readiness-pinned-workload`     | `upgrades-audit-reports-upgrade-blocked`, `upgrades-drain-blocking-budget-named`, `upgrades-no-surge-pool-recreate-warned`, `upgrades-readiness-surge-capacity`                                                                                                                                                                                                                                                                                                                                                                                               |
| `seeded-fail-closed-gate`, a ValidatingWebhookConfiguration with `failurePolicy: Fail` and the API's maximum 30-second timeout, pointing at a Service that does not exist                                                                                                                                                                                                                                                                                                                                                                                                                        | `seeded-b`                           | `readiness-failclosed-webhook`  | `upgrades-fleet-readiness-failclosed-webhook`, `upgrades-retrospective-failclosed-webhook`                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| `pinned-batch-runner` PodDisruptionBudget with `maxUnavailable: 0`, so its pod can never be evicted voluntarily and every drain that touches it blocks                                                                                                                                                                                                                                                                                                                                                                                                                                           | `seeded-b`                           | `readiness-drain-blocked`       | `upgrades-fleet-readiness-exclusion`, `upgrades-audit-reports-upgrade-blocked`, `upgrades-fleet-readiness-failclosed-webhook`, `upgrades-drain-blocking-budget-named`                                                                                                                                                                                                                                                                                                                                                                                         |
| `drain-timeout-<prefix>-b`, a global external TCP backend service on the default pool's instance group with a 3600-second connection draining timeout, six times the 600 seconds GKE waits per node during a pool upgrade, behind a TCP health check and no forwarding rule, so it routes nothing and costs nothing                                                                                                                                                                                                                                                                              | `seeded-b`                           | `readiness-lb-drain-timeout`    | `upgrades-readiness-lb-drain-timeout` (held in `FIXTURE_NOT_READY` until the pool projects are re-applied)                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| `zone-pinned-api`, two replicas with a `ScheduleAnyway` zonal spread and a required single-zone nodeAffinity, so the constraint reads as protection and never spreads                                                                                                                                                                                                                                                                                                                                                                                                                            | `seeded-d`                           | `zonal-skew-scheduling`         | `upgrades-zonal-skew-replicas-warned`, `upgrades-removed-api-callers-distinguished` (slot presence only), `upgrades-network-policy-enforcement-stated` (slot presence only), `upgrades-zonal-control-plane-outage-warned` (slot presence only)                                                                                                                                                                                                                                                                                                                |
| `zone-bound-store`, two StatefulSet replicas on `seeded-zonal-pd`, a class whose `allowedTopologies` names only the first zone, so both claims bind there and neither pod can change zone without leaving its data (a `maxUnavailable: 1` budget, as `zone-pinned-api` carries)                                                                                                                                                                                                                                                                                                                  | `seeded-d`                           | `zonal-skew-volume`             | none yet                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| `capacity-starved-worker`, four replicas at 500m against seeded-d's two unequal nodes: the first zone's e2-small, which `first-zone-sponge` (six 100m replicas pinned to that pool) keeps below one worker's worth of free CPU, and the second zone's e2-standard-2, which fits one to three workers and never four, so every worker that runs is in the second zone and the surplus is Pending for CPU on any placement of GKE's own pods                                                                                                                                                       | `seeded-d`                           | `zonal-skew-capacity`           | none yet                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| `cache-on-emptydir`, one replica whose queue directory is an `emptyDir`: state on the node that a rebuild erases                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 | `seeded-a` / ns `seeded-shapes`      | `node-local-state`              | `upgrades-node-local-state-named`, `upgrades-fleet-readiness-node-local-state`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| `arch-pinned-worker`, a `nodeSelector` on `beta.kubernetes.io/arch`, deprecated since 1.14 and still set: schedules today, never again once a node image drops the label                                                                                                                                                                                                                                                                                                                                                                                                                         | `seeded-a` / ns `seeded-shapes`      | `deprecated-label-selector`     | `upgrades-deprecated-node-label-selector-named`, `upgrades-fleet-readiness-deprecated-node-label`                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| `node-runtime-probe`, a DaemonSet mounting the containerd socket from the host, read-only: the agent shape a containerd major change or a CRI API removal breaks                                                                                                                                                                                                                                                                                                                                                                                                                                 | `seeded-a` / ns `seeded-shapes`      | `containerd-socket-agent`       | `upgrades-containerd-socket-agent-named`, `upgrades-readiness-containerd-socket`                                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| `intree-pd`, a PersistentVolume on the in-tree `gcePersistentDisk` plugin over the fleet's own disk, bound by the `intree-pd` claim and mounted by the `intree-pd-reader` Deployment, which is what makes the attach happen: it goes only through CSI migration                                                                                                                                                                                                                                                                                                                                  | `seeded-a` / ns `seeded-shapes`      | `intree-pd-volume`              | `upgrades-intree-volume-named`, `upgrades-readiness-intree-volume`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| `legacy-registry-pull`, `k8s.gcr.io/pause:3.9` with `imagePullPolicy: Always`: a retired registry hostname served by redirect, pulled again at every node rebuild                                                                                                                                                                                                                                                                                                                                                                                                                                | `seeded-a` / ns `seeded-shapes`      | `retired-registry-image`        | `upgrades-retired-registry-image-named`, `upgrades-fleet-readiness-retired-registry-image`                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| `seeded-defaults`, a namespace with Pod Security Admission pinned to `enforce: baseline` at `enforce-version: latest`, so its rule set changes with every minor; `baseline-edge-worker` is admitted by today's (entry 8)                                                                                                                                                                                                                                                                                                                                                                         | `seeded-a` / ns `seeded-defaults`    | `moving-admission-default`      | `upgrades-pod-security-latest-pinned-named`, `upgrades-readiness-changed-defaults`                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| `stale-kubectl-client`, a CronJob running `kubectl` v1.29.0 against the control plane every ten minutes: a client outside the supported skew that still succeeds (entry 10)                                                                                                                                                                                                                                                                                                                                                                                                                      | `seeded-a` / ns `seeded-shapes`      | `stale-client-skew`             | `upgrades-stale-client-skew-named`, `upgrades-readiness-client-addon-skew`, `upgrades-retrospective-stale-client-skew`                                                                                                                                                                                                                                                                                                                                                                                                                                        |
| `cgroup-blind-jvm`, the eclipse-temurin 8u302 image under a memory limit: a JDK 8 from before cgroup v2 support, with a sleep as the container process (entry 14)                                                                                                                                                                                                                                                                                                                                                                                                                                | `seeded-a` / ns `seeded-shapes`      | `cgroup-blind-runtime`          | `upgrades-cgroup-blind-runtime-named`, `upgrades-fleet-readiness-cgroup-blind-runtime`, `upgrades-retrospective-cgroup-blind-runtime`                                                                                                                                                                                                                                                                                                                                                                                                                         |
| `multi-process-worker`, a shell supervisor forking two workers under one 32Mi limit: the shape a cgroup v2 node OOM-kills whole (entry 15)                                                                                                                                                                                                                                                                                                                                                                                                                                                       | `seeded-a` / ns `seeded-shapes`      | `multi-process-container`       | `upgrades-multi-process-oom-group-named`, `upgrades-fleet-readiness-multi-process-container`, `upgrades-retrospective-multi-process-container`                                                                                                                                                                                                                                                                                                                                                                                                                |
| `cni-shaped-agent`, a host-network DaemonSet mounting `/etc/cni/net.d` read-only: a network plugin's coupling to the node image (entry 17)                                                                                                                                                                                                                                                                                                                                                                                                                                                       | `seeded-a` / ns `seeded-shapes`      | `node-image-coupled-agent`      | `upgrades-node-image-coupled-agent-named`, `upgrades-fleet-readiness-node-image-coupled-agent`                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| `cuda-pinned-trainer`, a suspended CronJob requesting one GPU on an `nvidia-l4` pool with a CUDA 12.2 image: the driver dependency in a manifest that never runs (entry 18)                                                                                                                                                                                                                                                                                                                                                                                                                      | `seeded-a` / ns `seeded-shapes`      | `cuda-pinned-gpu-job`           | `upgrades-cuda-pinned-gpu-job-named`, `upgrades-fleet-readiness-cuda-pinned-gpu-job`, `upgrades-retrospective-cuda-pinned-gpu-job`                                                                                                                                                                                                                                                                                                                                                                                                                            |
| `storefront` and the Gateway's proxy Deployment at replicas 0, Gateway `storefront-edge` Programmed=False (NoResources) with status written by `gateway_status.py`: a switched-off environment, not a stall                                                                                                                                                                                                                                                                                                                                                                                      | `seeded-a` / ns `seeded-scaled-zero` | `scaled-zero-gateway`           | `cluster-agent-scaled-zero-gateway-silence` (parked until the role is planted)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |

The `environment=seeded` resource label is the cohort confinement, the same class of
fixture-determinism as the pool taints: the drift SOP resolves environment from
`.resourceLabels.environment` before any name inference and keys cohorts on
(mode, environment), keeping unknown-environment clusters in their own cohort. Without
the label, `platform-agent-host` and any transient `eval-pr*` clusters would vote on
this fleet's baseline — a 2/2 authorized-networks split has no majority and no
finding, and churning eval clusters would randomize the audit run to run. The label
value `seeded` is reserved for the clusters in this stack; labeling any other cluster
with it changes the vote.

The `upgrade-family` resource label is the rollout-family membership the upgrade rollout
reads: the retrospective's family gate groups clusters by `resourceLabels.upgrade-family`,
upgrades one member of a family first and holds the rest on that member's post-upgrade
diff. `seeded-a`, `seeded-c` and `seeded-d` carry `upgrade-family=seeded-regular`;
`seeded-b`, held a minor behind, carries `upgrade-family=seeded-held`, a family of its own,
so the regular family's rollout never waits on the lag. The key is on every cluster in
this stack because the drift audit's label-keys facet compares label keys across the
cohort: a key on three clusters and not the fourth would be a drift finding.

`seeded-b`'s lag is derived at apply time (the newest patch REGULAR itself offers one
minor behind its default, read by `seeded_b_pin.py`), so the pin re-computes each
reconcile instead of rotting — and `seeded-b` is enrolled in the REGULAR channel on purpose: the upgrade
SOP's master-behind check compares a cluster's master minor against its own channel's
default, so a channel-less cluster falls out of that comparison entirely and the lag
would be invisible to the audit it was planted for. The maintenance exclusion above is
what stops channel enrollment from healing the lag.

## Addressing a fixture by role

The `Where` column above is documentation. **No scenario may name a cluster or a
project**, because every eval project carries its own set of seeded clusters and the
pool of eval projects is meant to grow: a check that says `seeded-a` in
`kube-agents-evals` is a check that cannot run in `kube-agents-evals-2`. A scenario
names the **role** a fixture plays and the runner resolves it inside whichever project
the run leased.

`fixtures.json` in this directory is the catalog, and the only place role and cluster
meet. Each role gives a `cluster_slot` (`a` to `d`) and the namespace the fixture
lives in, if any. It deliberately carries **no** cluster name, prefix or location:
those are this Terraform's business, and a catalog that repeated them would agree with
reality only for as long as nobody applied the stack with a non-default
`-var cluster_prefix` or into another region — a drift that would surface as failing
checks rather than as an error in the runner. Planting a new defect means adding a role
here in the same change; a `bench/tests/test_fleet_verifier.py` test fails when a
`task.yaml` names a role the catalog lacks, or reads a namespace through a role the
catalog puts elsewhere.

The chain, end to end:

1. `hack/fleet-kubeconfigs.sh` **discovers** the seeded clusters in `$FLEET_PROJECT_ID` (defaulting
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
   Kubernetes provider ran leaves clusters that answer every API call and hold none of
   the objects. Confirming presence here, before the agent runs, is what entitles the
   verifier to read an object that is gone at check time as a fixture the run destroyed
   rather than one that was never planted. It probes the **objects** and not merely the
   namespace because several roles are cluster-scoped and have no namespace:
   a namespace-only gate published them unconditionally, and `compliance-rbac-overgrant`
   then reported a catastrophic `fail` against an agent that had touched nothing.
   Adding a fixture therefore means adding both its role and its probes; every subject
   a `task.yaml` asserts on must appear in that list, which
   `bench/tests/test_fleet_verifier.py` enforces in both directions. It also means
   adding the role's `state` assertions — the observable shape the cases depend on,
   in the small path-and-operator language `fixtures.json`'s `state_syntax` describes
   — which `hack/fleet-fixture-state.py` evaluates after the presence gate. For slots
   b and c the subject is the cluster itself, read back through `clusters describe`
   on the name the runner recorded in `.fleet-context`; the script discovers nothing
   on its own.
3. A check in a `task.yaml` uses the `fleet_resource_property` verifier and names
   `fixture_role: crashloop-workload`.
4. `kube_agents_bench.fleet.kubeconfig_for_role` turns the role into that path, and the
   verifier binds it to the check's `kubeconfig`.

A role that will not resolve — its apply stopped before planting that fixture, the runner
never ran, or that cluster was unreachable — is `status: "error"` naming the role and the
project. (A project the stack was never applied to has no reader account either, so a run
stops at the credential gate before any check.) It never falls back
to the ambient kubeconfig, which points at the agent's host cluster and carries no
fixture; that fallback was activation blocker A5 in `bench/tasks/DRAFTS.md`. See
[Addressing a seeded-fleet fixture by role](../../CUSTOM-TASKS.md#addressing-a-seeded-fleet-fixture-by-role)
for the spec side, including how the verifier keeps "the fixture is gone" (a fail)
apart from "the cluster was unreachable" (an error).

## The presubmit's one consumer outside the role catalog

`hack/ci-eval-pr.sh` addresses this fleet directly in one place, §3b, the log-fixture
subject, which `fixtures.json`'s description names as the sanctioned exception to its
rule. It mutates nothing in-cluster, and nothing in the job repairs a drifted fleet:
the hourly scan above detects one, and the scheduled reconcile ("State and reconcile")
corrects it.

On every presubmit in a fleet-carrying project §3b discovers **slot c** by the same
two labels and the trailing `-<slot>` name segment, verifies
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

Applying this stack is what makes it true. It provisions
`seeded-fleet-reader@<project>.iam.gserviceaccount.com` with `roles/container.viewer` on
the project and nothing else, and grants `roles/iam.serviceAccountTokenCreator` on that
account to the members in `var.fleet_reader_token_creators` — which defaults to
`prowjob-default-sa@kube-agents-prow.iam.gserviceaccount.com`, the identity every
presubmit runs as, to `eval-baseline-recorder@kube-agents-prow.iam.gserviceaccount.com`,
the nightly periodic's, and to `eval-dashboard-publisher@kube-agents-prow.iam.gserviceaccount.com`,
the CI health bot, whose hourly fixture-state scan reads every pool project's fleet as
the reader; on the project itself it holds only the pool-state scan's roles, below
([`docs/ci-health.md`](../../../docs/ci-health.md), "The seeded-fleet scan").
`hack/ci-eval-pr.sh` exports `FLEET_READONLY_SA` pointing at the
account, and `hack/fleet-kubeconfigs.sh` writes each kubeconfig with an `exec:` credential
naming `hack/fleet-reader-credential.sh`, which mints a token as that account whenever
`kubectl` asks for one.

Without the token-creator grant the script writes nothing and exits 3, and a run that
leases the project stops at that step.

On a fleet only you use, leave `FLEET_READONLY_SA` unset and set
`FLEET_ALLOW_RUNNER_CREDENTIAL=1` to read it on your own credential. The alternative
was reading the fleet as the runner's own identity, which holds `roles/container.admin` among the thirteen project roles
`scripts/provision_ci_pool_project.sh` grants at onboarding (`PROW_RUNNER_ROLES` in
`scripts/verify_ci_pool_project.py` is the list) — measured, not assumed:
`kubectl auth can-i delete deployments -n seeded-debug` answers yes. There are zero
ClusterRoleBindings or RoleBindings on these clusters naming any `*.gserviceaccount.com`
subject; authorization comes entirely from the GKE IAM webhook, so there is nothing to
narrow in-cluster either.

The default landed after the pool was provisioned; a project applied before it and never
re-applied lacks the binding — `scripts/verify_ci_pool_project.py` fails such a project, and
re-applying this stack against it is the repair. Where the grant is in place the property
is checkable rather than asserted:

    gcloud auth print-access-token \
      --impersonate-service-account="seeded-fleet-reader@<project>.iam.gserviceaccount.com" \
      | xargs -I{} kubectl --token={} auth can-i delete deployments -n seeded-debug
    # must print: no

The stack also grants `var.pool_state_readers` (default: the bot) the roles in
`local.pool_state_reader_roles` for its hourly pool-state scan
([`docs/ci-health.md`](../../../docs/ci-health.md), "The pool-state scan"); a project
applied before that default scans as "not checked".

Three things about this are worth stating rather than assuming:

- **Impersonation must be a minted token, not a flag on `get-credentials`.**
  `gke-gcloud-auth-plugin` has no impersonation option, so the exec credential a
  `get-credentials --impersonate-service-account` writes still resolves to the caller's
  own identity at `kubectl` time. Only a token minted by impersonation actually binds it.
- **The token cannot be baked into the kubeconfig.** It lives one hour; these files are
  written before the image build and read hours later (recorded whole-job times in
  `hack/ci-eval-pr.sh`: 180 to 222 minutes). An expired credential makes every fleet check
  report `status: "error"`, which reds the presubmit. Hence the `exec:` block, minting
  against the clock of the check. `hack/fleet-reader-credential.sh` caches on disk because
  `kubectl`'s own exec cache is per-process and every check is a separate invocation.
- **`roles/container.viewer` was verified, not assumed.** Its permission set contains no
  `container.secrets.*` and no create/update/delete/patch verb; the single non-get/list
  entry is `container.tokenReviews.create`.
- **Never fold gcloud's stderr into the token.** On the _success_ path
  `gcloud auth print-access-token --impersonate-service-account=...` prints
  `WARNING: This command is using service account impersonation...` to stderr. Capturing
  it with `2>&1` yields a two-line blob that `kubectl config set-credentials` accepts
  without complaint, after which every API call 401s while the script reports success —
  a silent break of exactly the path this section recommends. The gate captures stderr
  separately and rejects anything that is not a bare token; it is the only mint the
  runner makes, since the binding is per account and `fleet-reader-credential.sh` mints
  its own at check time.

## Accepted background findings

Each audit of this fleet returns its planted finding **plus** the rows below —
nothing else, once the fleet is at steady state. Two conditions qualify that:
enrolling `seeded-a`/`seeded-c`/`seeded-d` in REGULAR can surface a transient upgrade 3.1
`master-behind` on them until GKE auto-upgrades their masters in the 03:00
window, and upgrade 3.3 `fleet-spread` is computed over **every** cluster the
audit reads, in every project the credential can list, not just the seeded
clusters — a `platform-agent-host` or transient `eval-pr*` cluster running a minor
ahead of the channel default pushes fleet-wide spread to two and attaches an undeclared `minor` to
`seeded-b`. Neither breaks any scenario (the objectives are `report_contains`,
not exclusivity checks), but both make this table temporarily incomplete. Everything else the baseline used to trip is closed in the stack
itself (Workload Identity and `GKE_METADATA` everywhere, legacy metadata endpoints
disabled, non-root-with-seccomp on all planted workloads and
`automountServiceAccountToken: false` on every one that uses no API (the
deprecated-API writer runs on its own ServiceAccount under a namespaced Role, which
is not the default-SA shape SOP 2.7 flags; `seeded-token`'s two workloads mount it on purpose, one declared and one the finding the compliance declared-token case grades; the stale kubectl
client runs on its own ServiceAccount with no Role at all, which compliance 2.14 flags and the
table below accepts), default-deny NetworkPolicies in every
network-less workload namespace but `seeded-intent`, whose missing policy is the declared posture its note covers, and an API-server-only egress policy in
`seeded-deprecation`,
REGULAR channel and a maintenance window on every cluster, a PDB and soft spread
constraints where a non-fixture workload would otherwise trip the reliability
checks), precisely so this table stays short: the fixture's premise is that a
correct audit's findings are known in advance.

| Audit         | Check                                  | Where                                                  | Why it stays open                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| ------------- | -------------------------------------- | ------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| compliance    | 2.10 `public-control-plane` (critical) | all four clusters                                      | `seeded-a`, `seeded-b` and `seeded-d` carry a literal `0.0.0.0/0` authorized-networks block and `seeded-c` none — and `seeded-c`'s missing block IS the drift outlier, so it can never close. Closing a, b or d needs a named CIDR that admits every caller with dynamic egress: Prow runners, the platform-agent pods that run the audits, and owner laptops. Until those have stable egress (reserved Cloud NAT IPs per project, or private endpoints plus internal runners), a narrow list would brick the fleet's own auditability. The matching trivy IDs (GCP-0053, GCP-0061) are ignored path-scoped in `.trivyignore.yaml` for the same reason |
| upgrades      | 3.10 `no-notifications` (minor)        | all four clusters                                      | Closing needs a Pub/Sub topic and notification config per project — real infrastructure for a minor visibility finding on a fleet whose upgrades are themselves fixtures. `seeded-c`'s is declared on purpose by the same `knowledge/notification-relay-no-pdb.md` note and lists under Declared intent; the other three stay findings                                                                                                                                                                                                                                                                                                                 |
| reliability   | 3.10 `probes-liveness` (minor)         | every planted workload                                 | `checkout-gateway` runs `pause`, which has no shell and listens on nothing, so no honest probe exists; the SOP itself calls a missing liveness probe "frequently the correct choice". Declared for all of them rather than closed on some and left asymmetric. The pinned slot-b workload and the three slot-d workloads run `pause` too and are declared on the same grounds                                                                                                                                                                                                                                                                          |
| cost          | 3.9 `terminal-pods` (minor)            | `seeded-deprecation/legacy-endpoints-writer-first-run` | the first-run Job carries no `ttlSecondsAfterFinished` on purpose: the provider's Read has no TTL handling, so a Job the cluster has expired is re-created on every reconcile. From day 7 the check sees a Complete Job with no TTL and no CronJob owner. CronJob-owned Jobs keep their 900-second TTL and never appear                                                                                                                                                                                                                                                                                                                                |
| obtainability | 3.5 `no-hpa` (minor)                   | `capacity-starved-worker`                              | Four replicas with no HorizontalPodAutoscaler. An HPA would move the replica count the fixture depends on — the fixture IS that not every replica can be placed — so this is declared rather than closed                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| obtainability | 3.5 `no-hpa` (minor)                   | `first-zone-sponge`                                    | Six replicas with no HorizontalPodAutoscaler, most of them Pending by design: the sponge exists to hold the first zone's node below one capacity worker's worth of free CPU, and an autoscaler would resize the very thing it pins. It carries a `maxUnavailable: 100%` budget so that it is neither the fleet's second unprotected multi-replica workload (3.3) nor a drain blocker.                                                                                                                                                                                                                                                                  |
| compliance    | 2.3 `hostpath-mount` (critical)        | `seeded-shapes/node-runtime-probe`                     | The read-only containerd socket mount is the planted dependency (role `containerd-socket-agent`, catalogue entry 13); closing it would delete the defect. `kind: manual`, so nothing promotes it                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| compliance    | 2.2 `host-namespace` (major)           | `seeded-shapes/cni-shaped-agent`                       | Host networking is half of the planted coupling (role `node-image-coupled-agent`, catalogue entry 17); a pod network would heal the fixture                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| compliance    | 2.3 `hostpath-mount` (major)           | `seeded-shapes/cni-shaped-agent`                       | The read-only CNI configuration mount is the other half of that coupling; closing it would delete the fixture                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| compliance    | 2.14 `unbound-sa-automount` (major)    | `seeded-shapes/stale-kubectl-client`                   | The token is what `kubectl` authenticates with and the absence of a grant is the point (role `stale-client-skew`, catalogue entry 10): `/version` is served to any authenticated caller, so a Role would add nothing but a suppression                                                                                                                                                                                                                                                                                                                                                                                                                 |
| obtainability | 3.7 `rigid-scheduling` (major)         | `zone-pinned-api`                                      | The required single-zone nodeAffinity is the fixture: it is what makes `ScheduleAnyway` produce a skew with an attributable cause. Closing it would delete the defect                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |

The fleet plants no removed-API caller, and cannot: no Kubernetes minor after 1.32
removes a served API version through 1.37 (the upstream
[Deprecated API Migration Guide](https://kubernetes.io/docs/reference/using-api/deprecation-guide/)
lists nothing after 1.32, and `Endpoints` is deprecated since 1.33 as a GA API, which the
deprecation policy forbids removing within the major version), so there is no API a
permanent fixture could call that a fleet master would refuse. What the `deprecated-api-caller` role yields
instead is the Admin Activity audit annotation `k8s.io/deprecated=true` on each of its
writes, with no `k8s.io/removed-release` label (core/v1 declares no removal) and no
Recommender `DEPRECATION_*` insight, which GKE raises for removed APIs only. A case that
reads the trail keys on the principal
(`system:serviceaccount:seeded-deprecation:legacy-endpoints-writer`) plus `methodName`
(`io.k8s.core.v1.endpoints.patch`), never on the label alone: `kube-system`'s
endpoint-controller writes every Service's Endpoints and is stamped the same way.

The drift audit is fully declared with no background rows, and the cost audit has one
(the table above, 3.9 `terminal-pods`): the drift
cohort is confined to the four seeded clusters whose only surviving-severity
divergence is the planted authorized-networks outlier (the other base-critical
facets — private nodes, database encryption — are uniform, and everything
lower-severity is dropped by the ladder at r = 3/4, as it was at 2/3 before slot `d`), and the cost audit's only
findings are the two planted, age-gated fixtures (the right-sizing check's
reclaimable-delta floor sits far above these tiny pods). The upgrade audit's
remaining absolute checks are clean by construction: every cluster now has a
channel and a window, the fleet's minor spread is one (below the two-minor
threshold — which is also why `seeded-b`'s master is re-pinned forward rather
than frozen: a frozen master would fall two minors behind at the next REGULAR
roll and trip 3.3 `fleet-spread`; a hold is that frozen master, so while one
lasts and the other clusters follow the default, 3.3 fires on `seeded-b`, a
finding the background table does not carry and the hold's end clears), 3.2 `pool-skew` is clean because
`seeded-b`'s pool is driven from the same derived pin as its control plane
(any skew between them is the transient mid-reconcile lag 3.2 explicitly does
not flag), the `NO_MINOR_UPGRADES` exclusion is the scope its 3.8 explicitly
does not flag, and pools run default auto-upgrade/auto-repair on COS_CONTAINERD.
One upgrade-audit finding is planted by design: 3.11 `upgrade-blocked` on `seeded-b`, joined
from the `pinned-batch-runner` budget above, the `drain-timeout-<prefix>-b` backend service's
`lb-connection-draining` block once the stack is re-applied with it, and the version lag; the
defects table names the case that asserts on it.

Implication for the scenarios (`feat/domain-scenarios`): each objective must
assert the planted finding specifically — `debug-binding`, `cluster-admin` —
never "the audit found something", and judged prose should expect the declared
rows above to appear alongside the planted finding in their respective audits.

The chat-routing and incident-triage scenarios need no defect planted in the
fleet — incident triage plants its own, an OOM-killed workload applied to the
host cluster by `bench/tf/prebuilt/autoops-incident`, because a per-run cluster
joins `k8s-event-watcher`'s watch set too late to be watched inside the run
(the watcher does fan in over the Cluster Agent profile clusters; that stack's
header has the timing argument); the
silence-on-a-clean-fleet case needs a clean view, which is an open fleet-design
decision recorded with the scenario drafts. The silence case in particular must
tolerate the declared background rows above.

Rough standing cost: about $340 per month — the GKE management fee (three zonal
clusters) is most of it, the six nodes (20 GB disks), two 10 GB orphan disks and the
`startup-fail` e2-micro the rest. The no-surge pool on `seeded-b` adds a seventh node, a fifth e2-small, and
seeded-d adds a fourth management fee, an e2-small and an e2-standard-2, so a project carrying
the whole stack costs roughly $490 per month — the fee, not the nodes, is the larger
part of the increase. No separate rollout stands between a merge and that cost: a new
cluster or pool plans as a create, which `hack/fleet_reconcile.py` applies, so the
postsubmit on that merge creates both in every pool project within hours
([`docs/ci-pool-projects.md`](../../../docs/ci-pool-projects.md) §6.2). Four of the first six nodes are e2-small; `seeded-a`'s default pool is two e2-standard-2s
(roughly $49 per month more than the two e2-mediums it ran on), because GKE's system pods
claim 700-860m of an e2-medium's 940m allocatable CPU and the planted fixtures' pods went
Pending, first on a single node and then, as fixtures accrued, on the busier of two — the
comment on `seeded_a_default` in `main.tf` has the numbers.
