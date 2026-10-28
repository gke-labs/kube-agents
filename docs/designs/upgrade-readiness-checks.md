# Upgrade readiness checks for a large GKE fleet

**Status:** requirements. Nothing here is on `main` beyond what the Scope table credits to an
existing audit or skill. The incident checks section records six checks and one extension,
implemented and, as of 2026-10-21, not on `main`; the Verification and Progress sections record
their proof.

## High level summary

A GKE minor upgrade replaces the control plane and rebuilds every node. Either event can stop an
application that did not expect it. The upgrade work gives kube-agents two questions to answer for
every cluster: will the next upgrade succeed, and what did the last upgrade change. Some pieces are
on main today. The Scope table below and each linked document's status line say which. This document
and the retrospective design specify the rest. The agent only reads and reports. A person decides
and upgrades.

**Before the upgrade: the readiness report.** A daily job grades each cluster for which GKE offers a
new version, a Saturday job grades every cluster in scope and registers each finding on the
findings queue, which reminds the user until a decision, and the agent grades any cluster on
request in chat. Today each cluster's grade is blocked, unknown or ready, and each finding names
the object and the setting that blocks. This
document specifies a verdict per cluster family: Go, No-Go or Action required. The Scope table below
says which parts exist on main.

**From the field: the incident checks.** One production fleet's support history supplies the sixteen
upgrade incidents in the Progress table. Six new checks, and two new reads in a seventh, the
surge-capacity check, cover thirteen of them. The other three incidents, in two scenarios, have no
check yet. As of 2026-10-21 the seven checks are implemented and not on main. Four layers of proof
verify a check. They are a replay test, a live read, a planted fixture with its evaluation case, and
the weekly review run end to end. The sections "The incident checks", "Verification" and "Progress"
record all of this.

**After the upgrade: the retrospective.** The retrospective design specifies a job that runs once at
install, at the end of each weekend and soon after each upgrade. Its report says what each upgrade
did, what failed, which catalogue entry the failure matches, and the fix to make before the next
upgrade. It records each failure, and each risk it finds on a cluster, as a guard. The next
readiness report shows the guard. The collector is on main
(`agents/platform/skills/fleet-audit/scripts/upgrade_retrospective.py`); the job that schedules it
is not. See [`upgrade-retrospective.md`](upgrade-retrospective.md).

**What both rest on: the catalogue and the test fleet.** The catalogue lists the ways an upgrade
fails, each from a public incident, provider documentation, a test-fleet fixture or one production
fleet's support history. For each it
names the signal before the upgrade and the signal after it. The test fleet carries the before-state
of each entry that a standing fixture can carry. The catalogue plans one evaluation case per entry,
each grading whether the agent names it, and its status table names the registered cases. See
[`upgrade-failure-catalogue.md`](upgrade-failure-catalogue.md) and
[`upgrade-failure-reproductions.md`](upgrade-failure-reproductions.md).

### The scenarios, as the agent reports them

Each entry's case is to ask the agent one question about the test fleet and grade a
fixed-form answer line. The line names the cluster, the object and the finding, so a check reads it
without a model as judge. The catalogue's status table names the registered cases; the rest are
planned. Entry 5's fixture is a maintenance exclusion that holds the minor upgrade. One answer the
agent gave for it on the test fleet, from a case not yet registered, quoted from the run's record:

```
seeded-a: exclusion none; scope none; ends none; holds the automatic minor upgrade: no
seeded-b: exclusion hold-the-minor-lag; scope NO_MINOR_UPGRADES; ends 2027-01-05; holds the automatic minor upgrade: yes
seeded-c: exclusion none; scope none; ends none; holds the automatic minor upgrade: no
seeded-d: exclusion none; scope none; ends none; holds the automatic minor upgrade: no
```

That case's check graded five objectives on that reply and passed all five. The five objectives: a
line exists, it states the scope and end date, it says the exclusion holds the upgrade, and it
invents no CVE.

### The retrospective, as it reports

The retrospective design specifies a collector. It reads GKE's upgrade operations for the window,
the versions before and after, and the objects that failed after each one. It matches each failure
to a catalogue entry, or marks it unclassified. It writes one block per incident: what happened,
what failed, how to detect and mitigate it next time, and the guard it recorded. One block from a
report the collector produced on a development project that contains the test fleet. The quote
leaves out the version and operation tables and replaces the project name:

```
### 2 — <project>/us-central1-a/seeded-a — `seeded-capacity/Deployment/inference-server`

**What happened.** new (first seen); channel REGULAR; cluster status RUNNING.

**What failed.**

| Symptom | Since | Catalogue entry | Confidence | Evidence |
| --- | --- | --- | --- | --- |
| Unschedulable | first seen | 2. No spare capacity for the displaced pods | high | 3 of 4 pods: Insufficient cpu; e.g. inference-server-778b78fdb8-ld26r; 1 pre-existing since 2026-09-25T17:02:48Z (e.g. inference-server-778b78fdb8-cp2pf) |

**Detect and mitigate next time.**
- **2. No spare capacity for the displaced pods** — For seeded-capacity/Deployment/inference-server: A pool whose upgrade settings let a node go before its replacement exists, with requests near allocatable, the autoscaler at its ceiling or quota exhausted. Read today: the stockout-prevention audit flags regional GPU, TPU and CPU quota near exhaustion and pools near `autoscaling.maxNodeCount`; `maxSurge` and headroom against allocatable are unread. Mitigate before: Keep the pool on maxSurge at least 1 and maxUnavailable 0, with one node of headroom and quota for the extra node. Mitigate after: Add a node or raise the ceiling and the Pending pods schedule.

**Mitigation set up.**
- guard `<project>/us-central1-a/seeded-a#failure#2#seeded-capacity/Deployment/inference-server` failure entry 2 (high), first seen 2026-10-14T19:49:58Z, 1 review(s)
```

That run reviewed the project's seven clusters in 20 seconds and filed 6 errors and 8 warnings. It
recorded 61 guards: 15 for failures and 46 for risks.

## The problem, in one paragraph

Kubernetes ships a new minor version every few months, and GKE clusters have to follow. Before a
team upgrades, somebody has to answer one question: **will anything break?** Answering it today
means opening several tools and checking by hand — is anything still calling an API this version
deletes, are the add-ons compatible, will the pods survive nodes being drained one at a time — and
then repeating all of it for the next group of clusters. A team running hundreds of clusters does
this over and over. This document lists the checks an agent should run on a schedule instead, so
the answer is waiting for them.
The failures those checks are chosen from, each with its pre- and post-upgrade signal, are
catalogued in [`upgrade-failure-catalogue.md`](upgrade-failure-catalogue.md).

Two things to know about the checks. They are **read-only**: they look and report, and never
upgrade anything — that stays a human's decision. And they are **grouped by cluster family**, a
family being a set of clusters built from the same template, because that is the unit a team
actually upgrades.

## Upgrades that went wrong in public

Every check below exists because the thing it looks for has already taken a real organization down.
These are published postmortems, provider incident reports, and — where no company wrote one up —
issue threads where operators reported production breakage. Each entry says which check would have
caught it. Two categories have no incident behind them and are marked as such at the end; nothing
here is a hypothetical dressed as history.

**Reddit, 14 March 2023 — 314 minutes down.** Reddit upgraded a large cluster from Kubernetes 1.23
to 1.24. The network died about two minutes later. Their CNI, Calico, picked its route reflectors —
the nodes every other node peers with — by matching the label `node-role.kubernetes.io/master`.
Kubernetes 1.24 removed that label from running clusters. The selector matched nothing, every node
dropped every route, and the cluster went dark. There is no supported Kubernetes downgrade, so
recovery meant a restore procedure they had never run against production. The postmortem is
explicit about why no scan would have found it: the route-reflector configuration lived in
Calico-specific data "expected to only be managed by their CLI interface (not the standard
Kubernetes API), hand-edited, and uploaded back", and was "thus committed nowhere".
[Postmortem](https://web.archive.org/web/20260826130053/https://www.reddit.com/r/RedditEng/comments/11xx5o0/you_broke_reddit_the_piday_outage/)
(the Wayback copy; Reddit blocks automated fetches of the original).
_Caught by:_ diffing the target release's removed **identifiers** — labels as well as API versions —
against the selectors in use in the live cluster, including add-on configuration that lives outside
the Kubernetes API.

**Jetstack, September 2019 — a fail-closed webhook deadlocked a GKE control plane.** A regional GKE
master upgrade ran past the 20-minute timeout they had set on their Terraform apply. When the second
master came up, kube-apiserver's startup hook tried to write a ConfigMap in `kube-system`. A
ValidatingWebhookConfiguration backed by Open Policy Agent, scoped far more broadly than it needed
to be, intercepted that write. OPA did not answer, the write timed out, the master failed its health
check and crash-looped. The resulting API downtime stopped kubelets reporting node health, so GKE
node auto-repair began destroying and recreating nodes in a loop, taking out every tenant. Their
permanent fix was to scope the webhook to specific namespaces and resources and to give OPA a
liveness probe.
[Postmortem](https://web.archive.org/web/20230607064526/https://www.jetstack.io/blog/gke-webhook-outage/).
_Caught by:_ listing every webhook that fails closed and flagging any whose rules or namespace
selector can match `kube-system` or cluster-scoped objects, or whose backend runs one replica with
no liveness probe.

**loveholidays, March 2019 — a GKE upgrade budgeted at two to two and a half hours did not hold.**
Going from 1.10 to 1.12, a 14-node pool "took over an hour" at about five minutes a node, because
pods used `emptyDir` — which blocks eviction unless annotated `safe-to-evict` — and some workloads
had a `terminationGracePeriodSeconds` of five to ten minutes. The author's own extrapolation is the
point: at 100 nodes, with some taking over fifteen minutes to drain, the window is not recoverable.
One node pool upgrade then hung with no drains happening at all and could not be cancelled from the
console. Separately, GKE had withdrawn the exact patch version they upgraded to five days earlier,
and it was still selectable.
[Write-up](https://deploy.live/blog/the-shipwreck-of-gke-cluster-upgrade/).
_Caught by:_ estimating drain time before the window — count pods with `emptyDir` and no
`safe-to-evict` annotation, and grace-period outliers, then multiply by node count — and re-reading
the target patch version's release notes at run time rather than at planning time.

**Google Cloud, September 2022 — Calico wedged pod teardown on GKE 1.22 and later.** A race condition
in Calico made the CNI fail pod teardown with an authorization error, leaving pods stuck in
Terminating or Pending across 34 locations. Every 1.22 and 1.23 release was affected, and 1.24
before `1.24.4-gke.800`. Google noted that using the cluster autoscaler can increase the chance of
hitting it, since more node churn means more teardowns.
[Incident report](https://status.cloud.google.com/incidents/urNR4xD4gBNsyaZj3W1i).
_Caught by:_ checking installed add-on versions against the provider's known-issues list, at patch
granularity where the fix landed in a patch — here a minor-level check flags 1.22 and 1.23 outright,
but only the patch version distinguishes a safe 1.24 from an unsafe one.

**Google Cloud, July–September 2021 — 59 days of auto-upgrades restarting containers.** Clusters on
the REGULAR channel were automatically moved from 1.19 to 1.20. On any node pool still using Docker
rather than containerd, every container on a node restarted whenever the Docker daemon did. Google
eventually paused automatic upgrades to stop it spreading.
[Incident report](https://status.cloud.google.com/incidents/vFhgfrfzzrx6zQo69SdQ).
_Caught by:_ inventorying the container runtime per node pool and treating "still on Docker" as
blocking. The same check is what catches the 1.24 dockershim removal.

**Datadog, March 2023 (~50 hours, five regions) and Heroku, June 2025 (~24 hours) — the same
failure, twice.** In both cases an automatic operating-system update ran on production hosts that
should not have been taking one, and restarted the host's networking service. On Datadog's Ubuntu
22.04 fleet, systemd v249's `systemd-networkd` "forcibly deleted the routes managed by the Container
Network Interface (CNI) plugin (Cilium)", taking tens of thousands of nodes off the network between
06:00 and 07:00. Heroku's networking service "relied on a legacy script that only applied correct
routing rules on initial boot", so a restart severed outbound connectivity for every dyno on the
host. Neither showed up in staging: on a cold boot the ordering is fine, and the bug exists only
when the package is upgraded under a running machine. Heroku's corrective action was to disable
unattended vendor OS upgrades outright.
[Datadog](https://www.datadoghq.com/blog/2023-03-08-multiregion-infrastructure-connectivity-issue/),
[Heroku](https://www.heroku.com/blog/summary-of-june-10-outage/).
_Caught by:_ asserting that unattended OS package upgrades are disabled on nodes, and testing a node
image by upgrading a running node rather than by booting a fresh one.

**Spinnaker on GKE, September 2023 — a controller polling a removed API froze the upgrade.**
Spinnaker's clouddriver called `policy/v1beta1/podsecuritypolicies` more than once a minute and the
cluster would not move to 1.25; `omitKinds` did not stop the polling
([issue thread](https://github.com/spinnaker/spinnaker/issues/6880)). The mechanism is GKE's, not
the thread's inference: when a deprecation insight is active, GKE states that "automatic upgrade to
the upcoming minor version is paused"
([GKE documentation](https://docs.cloud.google.com/kubernetes-engine/docs/deprecations/viewing-deprecation-insights-and-recommendations)).
A cluster sitting on an old version is often not a scheduling accident.
_Caught by:_ reading the deprecation insights and reporting a paused auto-upgrade as the reason a
cluster is lagging — the check in the GKE deprecation insights section below.

**Helm releases, from Kubernetes 1.25 — deploys blocked by a manifest nobody was running.** After
1.25 removed PodSecurityPolicy, `helm upgrade` fails on any release whose **stored** manifest
contains one. Nothing crashes; the workloads keep running. You cannot deploy that release again,
and removing the PSP from your chart does not help, because the failure is in reading the old
release Secret. The issue's opener reported that `helm mapkubeapis` did not resolve it at the
time, because it only rewrote `apiVersion`/`kind` pairs; from v0.4.0 it also removes the resources
whose kind has no successor ([issue thread](https://github.com/helm/helm/issues/11287)).
_Caught by:_ scanning stored Helm release state and GitOps manifests for removed kinds, not only
live objects. GKE's deprecation insights cannot see this: they are generated from live API-server
traffic over a 30-day observation window, so a manifest that is applied only after the upgrade is
invisible to them.

Two gaps, stated rather than papered over. **Surge and quota** has no public postmortem behind it;
the mechanism is documented by the provider rather than by a victim, so the citation is
[GKE's upgrade quota page](https://cloud.google.com/kubernetes-engine/docs/how-to/node-upgrades-quota).
**Stateful workloads pinned to a zone** has none either. The nearest first-party statement is about
ephemeral node storage rather than a zonal volume — GKE says data on a Local SSD "does not persist
when the Pod or node is deleted, repaired, upgraded, or experiences an unrecoverable error"
([GKE documentation](https://cloud.google.com/kubernetes-engine/docs/concepts/local-ssd)) — which
makes the point that an upgrade is a data event for some workloads, but is not evidence for the
zonal-volume case.

## Scope

Some of this already runs. The table says what, so nobody builds it twice. The SOP or skill named
is the real definition of what that check does and at what threshold; this document does not
repeat it. **The third column is the work.**

| Area                                   | Already on `main`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      | New in this document                                                                                                                                                                                                   |
| -------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Version posture                        | `security-patch-orchestrator` (`agents/platform/governance/security_patch_orchestrator_sop.md`): control plane behind its channel, node-pool skew, fleet spread, no channel, auto-upgrade and auto-repair off, no maintenance window, blocking exclusions, stale image types, notifications not configured, and whether a behind cluster's upgrade would go through cleanly (`upgrade-blocked`, from the readiness grade below). The `fleet-upgrade-verification` skill grades every cluster against one `--target-version` on request. Its `--readiness` carries a zonal control plane as a risk on every member, with the retry advice. Shipped in `--readiness` as per-entry rules: `cluster-status-age` (a cluster not `RUNNING` for more than four hours, with the operation that holds it), `control-plane-upgrade-history` (an upgrade or repair that failed on compute capacity in the last 90 days, against the maintenance window) and `target-patch-valid` (the target and the running patch against the channel's `validVersions`, re-read at run time).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   | A target version and date **per family**, and days remaining against that date. GKE's API exposes no end-of-support date (the SOP records why calendar EOL was dropped), so the release calendar is an operator input. |
| GKE deprecation insights               | Nothing reads the Recommender API; the `gke-upgrades` skill's checklist template tells a human to look at the insights dashboard or grep the API server's deprecated-request metric. `fleet-upgrade-verification --readiness` (on request, and daily under the `upgrade-readiness-watch` cron job when a cluster is below a version its channel offers) reads each cluster's audit log (paged `gcloud logging read`s, the last seven days, writes only) for the `k8s.io/removed-release` and `k8s.io/deprecated` stamps: an operator caller of an API the target removes that wrote in the last 48 hours blocks, a caller of a deprecated API is a risk, each named with its principal, user agent and API (`readiness_rules/removed_api_callers.py`, `deprecated_api_callers.py`).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    | The Recommender insights themselves, the metric cross-check, the count across runs, and the paused-auto-upgrade explanation.                                                                                           |
| Workload compatibility                 | The `fleet-upgrade-verification` skill's `api_deprecation_scan.py` scans every managed GitOps repository's manifests for `apiVersion`s the target version removes, reporting each hit with its replacement at the commit it read; it runs on request, not on a schedule. `--readiness`, on request and under the daily `upgrade-readiness-watch`, also pairs each pool's containerd major (from its nodes) with the major the target's node image ships (`upgrade_shape_tables.py`) and the pods on the runtime socket, flags NetworkPolicies on a legacy-dataplane cluster whose add-on does not enforce them, and grades in-tree `gcePersistentDisk` PersistentVolumes against the PD CSI driver add-on. The same grade reads the pod templates against the target: images on retired registry hosts, which still answer a pull today, are risks; Java and .NET images below the cgroup v2 floor on a pool the target moves to cgroup v2, multi-process containers the group OOM kill reaches (a heuristic), host-network DaemonSets coupled to the node image, and CUDA pins below their toolkit's driver minimum are risks; a CUDA pin whose major's driver floor the target image falls below blocks; a selector on a label whose drop a pool crosses in this upgrade blocks, on an uncarried or deprecated label is a risk. `--readiness` also grades, per member, kubectl clients (audit-log user agents and kubectl images) against the target's one-minor skew window and third-party add-on images against a sourced support table (`readiness_rules/client_addon_skew.py`; GKE-managed add-ons expose no version and are listed, not graded), and namespaces whose Pod Security Admission labels follow `latest` against the per-minor table of changed defaults (`readiness_rules/changed_defaults.py`, tables and sources in `upgrade_shape_tables.py`). Shipped in `--readiness`: the add-on inventory (`kube-system` and `gmp-system` DaemonSets and Deployments by image and tag, per member in the JSON and as a table) and the `known-issues-matrix` rule over it, with the table's freshness check. | The add-on compatibility matrix beyond the sourced rows, and admission-webhook posture — and putting the existing manifest scan on the schedule.                                                                       |
| Drain safety and disruption            | `obtainability-audit` (`agents/platform/governance/obtainability_audit_sop.md`): multi-replica workloads with no PodDisruptionBudget, drain-blocking budgets, single-replica Service-backed Deployments, rigid scheduling, missing spread. Weekly, the upgrade audit's `upgrade-blocked` check runs `fleet-upgrade-verification --readiness --cluster` once per behind cluster, and files the join; daily, the `upgrade-readiness-watch` cron job runs the same grade when a cluster is below a version its channel offers; every Saturday, the `upgrade-readiness-weekly-review` cron job runs it for every cluster in scope and registers each blocker and risk on the findings queue, which reminds until the user decides; on request, the same grade reports drain-blocking PDBs, fail-closed webhooks with an unreachable backend in the upgrade's path or able to refuse the control plane's bootstrap RBAC writes, upgrade-covering maintenance exclusions, node-pool skew, a pool that removes a node before its replacement exists (`maxUnavailable` above 0 on the surge strategy, or an autoscaled blue-green rollout) by placing the pods of each of its nodes onto the nodes their selectors, affinity and tolerations admit, and the surge nodes the pools need: a pool with accelerators that surges with no reservation affinity, and a Pod range with fewer free per-node blocks than the pools' `maxSurge` sum, per member against a chosen target, emitting `blocked`/`unknown`/`ready`. The same grade lists as a risk every Deployment or StatefulSet that keeps state on the node (a StatefulSet's `emptyDir`, a writable `hostPath`, a Local SSD path, a claim on a local StorageClass), which the rebuild loses. Shipped in `--readiness`: `lb-connection-draining` (a backend service on the cluster's pools or NEGs whose draining timeout is above GKE's 600-second deregistration wait) and `orphan-node-objects` (a Node whose instance the project no longer has).                                                                                                                      | Regional quota for the surge node, workloads pinned to a pool being retired, whether anything consumes the cluster's upgrade notifications, and the drain-time estimate.                                               |
| Rollout orchestration and verification | The `fleet-upgrade-verification` skill reports, run over run, which clusters started, completed or stalled against one target version. The `upgrade-retrospective` cron job (`agents/platform/governance/upgrade_retrospective_sop.md`) is the diff after the upgrade: on Sunday evening it reviews every cluster upgraded since its last run, classifies what stayed down, stuck or restarting against the catalogue, and keeps a guard per failure for the next readiness report. The retrospective's gate (`clear`, `risk`, `pending`: the pod and node diff plus Cloud Logging ingestion bytes by container for the seven days before and after), the readiness rule `rollout-sequence-precheck` (the fleet host project's existing RolloutSequences; the platform allows one per host project) and the rollout orchestration SOP (`agents/platform/governance/upgrade_rollout_orchestration_sop.md`: the precheck, one cluster per family first, a seven-day soak, the diff and its gate, then the rest, with a manual path per cluster when the sequence cannot be created).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     | The review of one cluster soon after its upgrade (designed, not built).                                                                                                                                                |

## What a run produces: a verdict, not a list

A list of findings still leaves someone deciding what it means. So each run ends with a verdict for
every cluster family — **`Go`**, **`No-Go`**, or **`Action required`** — in a form a release
pipeline can read and gate on.

Every check below carries a tier, and the tiers are what produce the verdict:

| Tier         | Meaning                                                         | Examples                                                                                                                                                                           |
| ------------ | --------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Blocker**  | The upgrade will fail or cause an outage. Fix before upgrading. | GKE has paused auto-upgrade on the cluster; a PodDisruptionBudget that can never be satisfied, so the node drain hangs forever; no spare quota for the extra node an upgrade needs |
| **Risk**     | The upgrade can proceed, but watch this.                        | An add-on whose version is not supported on the target Kubernetes version                                                                                                          |
| **Advisory** | Worth knowing, not worth blocking.                              | A stale annotation                                                                                                                                                                 |

The findings are the evidence behind the verdict; the verdict is the deliverable.

Closest thing on `main`: `fleet-upgrade-verification --readiness` already emits
`blocked`/`unknown`/`ready` per cluster from drain-blocking PDBs, fail-closed webhooks with an
unreachable backend in the upgrade's path or able to refuse the control plane's bootstrap RBAC
writes (ClusterRoles and ClusterRoleBindings; Roles and RoleBindings in `kube-system` and
`kube-public`), maintenance exclusions, skew and the per-entry rules registered in
`upgrade_readiness.EXTRA_RULES`, each a module of its `readiness_rules/` package and each a
blocker or a risk that the Scope table credits as it lands, then the plan-level
`rollout-sequence-precheck`, which lists the fleet host project's RolloutSequences once per run
and carries an existing one as a risk, since the platform refuses a second. Two
things are missing — the verdict is per cluster, not per family, and it reads a subset of the
checks here, not every one. It is on the schedule three ways: the upgrade audit's `upgrade-blocked`
check runs it weekly for the clusters that are behind and files the join, the daily
`upgrade-readiness-watch` cron job runs it for a cluster below a version its release channel
offers, and the Saturday `upgrade-readiness-weekly-review` job runs it for every cluster in scope
and registers each blocker and risk on the findings queue.

## Version posture

Already served (see Scope). The additions:

- Re-read the target patch version's release notes and known issues **at run time**, not at
  planning time. A provider can withdraw a patch version between the plan and the window, and a
  withdrawn version stays selectable. `target-patch-valid` does the version half: it reads the
  location's server config when the report runs and grades the member's target as a blocker when
  the cluster's channel's `validVersions` does not carry it and the control plane has to move to
  it, or neither that list nor `validNodeVersions` carries it and only pools move (the node list
  serves a pool move alone; a target given
  as `MAJOR.MINOR.PATCH` without its `-gke.` build is in a list when the list carries a build of it,
  as the API reads it), and the running
  control-plane version as a risk when its own channel's list has moved past it, whatever another
  channel or `validMasterVersions` still lists (the exemption the Monday audit's `master-behind`
  check applies, which hides this failure). The action names the newest active patch of the
  minor on the channel.
- A cluster that is not `RUNNING` accepts no change, and only the provider can end an operation
  that has wedged. `cluster-status-age` reads the status and, from the project's operations list,
  the operations on the cluster and on its node pools (a node pool upgrade, resize or repair names
  the pool and holds the cluster as a cluster operation does); the holder is the newest one still
  `RUNNING`. It files a blocker when the holder has run for more than four hours, naming the
  operation, its type and pool, its start and its age; a younger holder is a reconcile in
  progress, noted and not a blocker. A cluster not `RUNNING` with no `RUNNING` operation in the
  project's list is read again (the clusters list is made before any member is graded, the
  operations list at the project's first member, so an operation that ended in between leaves a
  list status the cluster no longer has, and one that started in between is in no list the rule
  saw): a `clusters describe` first, where `RUNNING` now is a note, then an `operations list` for
  the cluster, where a `RUNNING` operation is graded against the threshold; a failed read is
  unknown, and a status still not `RUNNING` with nothing running is a blocker quoting the
  cluster's `statusMessage`, with the newest finished operation as context.
  The action is the `gke-upgrades` skill's Stuck operation procedure: freeze changes and, past a
  few hours, open a support case quoting the operation id, or the status and its message.
- An automated control-plane upgrade, resize or repair needs compute capacity when it fires, and
  the provider does not check it first. `control-plane-upgrade-history` reads the operations list
  for an `UPGRADE_MASTER`, `UPGRADE_NODES` or `REPAIR_CLUSTER` that failed on capacity in the
  last 90 days and files it with the cluster's maintenance window: a risk when the cluster has
  no window or its window opens in the region's busy hours (06:00 to 21:00 in the region's
  standard local time), whose action is a window in an off-peak hour for the region and the
  upgrade retried in it, and a note whose action is the retry in the window when the window
  already opens off-peak; either way the upgrade notifications are consumed so the next failed
  attempt is a message the same day. A lagging signal by nature: it explains the last failure
  and lowers the odds of the next.
- Each family gets a target version and a date, taken from the GKE release calendar by the
  operator, and every run reports the days remaining. The point is to catch a family falling
  behind **its own plan**, which happens long before it falls behind what GKE still supports.

## GKE deprecation insights

Kubernetes removes APIs on a schedule. If a workload is still calling one that the next version
deletes, that workload breaks the moment the control plane upgrades.

GKE already detects this and will tell you: it publishes
[deprecation insights](https://docs.cloud.google.com/kubernetes-engine/docs/deprecations/viewing-deprecation-insights-and-recommendations)
per cluster through the Recommender API (`google.container.DiagnosisInsight`), covering removed API
calls, deprecated authentication methods, old node images and more. Nothing in this repository
reads them today.

There is a second reason to care. **When GKE sees one of these, it stops auto-upgrading that
cluster.** So a cluster quietly sitting on an old version is often not a scheduling accident — it
is GKE refusing to move it, and nobody noticed.

The checks:

- Read the insights for every cluster and report them grouped by family.
- Diff the target release's removed **identifiers** against what the live cluster uses — not just
  `apiVersion`s, but node labels a selector can name. The label a release drops is the same class of
  break as the API it drops, and it is the one a manifest scan misses.
- Say **who has to fix it**. An insight names the API being called; the caller's identity comes
  from the insight detail and the `k8s.io/deprecated` audit-log annotations, and the owning team
  from the namespace. A finding nobody owns does not get fixed.
- Cross-check the API server's own counter (`apiserver_requested_deprecated_apis`) for the APIs the
  **target** version removes — not just the current one, which is what a team usually checks.
- Track the count across runs, so a team sees progress: "12 callers last week, 3 now."
- When a cluster is behind its channel because an insight paused its auto-upgrade, say that is the
  reason rather than reporting an unexplained lag.

## Workload compatibility

Four ways a workload breaks on a new version:

- **Add-ons.** Service mesh, cert-manager, monitoring agents, GitOps controllers, CSI and CNI
  drivers, custom admission webhooks — each supports a range of Kubernetes versions. Inventory what
  each family runs and check it against the target version's support matrix **and against the
  provider's known-issues list for the specific target patch version**. A check that compares minor
  versions alone passes a cluster into a bug the provider has already published and fixed in a
  patch. The managed half is shipped: the readiness report inventories the DaemonSets and
  Deployments in `kube-system` and `gmp-system` per member by image name and tag (the JSON's
  `addons`, and a table after the readiness table), and the `known-issues-matrix` rule compares the
  inventory and the member's control-plane and target versions with
  `fleet-upgrade-verification/scripts/known_issues.json`, a table of the page's issues whose
  identified versions name a minor from 1.32 up (the JSON's source note states the rule and how
  each row's versions are read; a row naming no add-on matches by version alone), each with the
  page's cells, the patch that fixes it and the page anchor; a match through a bounded version set
  is a risk naming the fix, and a match through one with no upper bound (`All versions`, a build or
  minor and later with no fixed build above it) is a note on the member, not a risk, since it would
  stand on every later version until the page changes. A target given without its build is
  graded against the builds of the patch the channel lists: the issue is the target's risk when it
  affects every one, a note naming the affected builds otherwise. The
  table is data with a date: when it is older than thirty days the rule files a risk that the table
  is stale rather than passing a clean matrix, and the newest version the table knows is in the
  note, not graded. The operator add-ons (cert-manager, Istio, the
  GPU operator) and their vendor matrices are still the work.
- **Admission webhooks that fail closed.** A webhook with `failurePolicy: Fail` rejects everything
  it matches while its own backend is down, which is exactly what happens when the node that backend
  runs on is drained. Report the scope as well as the policy: a webhook whose rules or namespace
  selector can match `kube-system` or a cluster-scoped object can block the control plane's own
  writes during an upgrade, not just the pods being moved. Report the backend too — one replica with
  no liveness probe is what turns a slow webhook into a stuck one. `--readiness` already grades the
  case where the backend is unreachable now, on the webhook's rules (the writes a node drain and a
  node join make, listed with their sources as `UPGRADE_PATH_TARGETS` in
  `agents/platform/skills/fleet-upgrade-verification/scripts/upgrade_readiness.py`) and on its
  bootstrap-RBAC reach (a dead webhook whose rules match the ClusterRoles and ClusterRoleBindings a
  new master's `rbac/bootstrap-roles` hook reconciles, which no namespace selector exempts, or the
  Roles and RoleBindings it reconciles in `kube-system` and `kube-public` when the selector admits
  that namespace, `CONTROL_PLANE_CLUSTER_WRITES` and `CONTROL_PLANE_KUBE_SYSTEM_WRITES` in the same file; the ConfigMap write the
  Jetstack outage deadlocked on left the start-up path in Kubernetes 1.17, and the leader-election
  Leases are already on the node path's list); the object selector, match conditions and the
  would-fail-once-drained backend are still unread.
- **Manifests in Git, and release state, not just live clusters.** Stored Helm release manifests
  carry removed kinds even when nothing in the cluster runs them, and the next `helm upgrade` of that
  release fails on reading its own stored state. Nothing breaks until someone deploys, which is why
  a live-traffic check never sees it.
- **Manifests in Git.** A removed field or API version sitting in the
  GitOps repository breaks on the next sync even if nothing in the cluster uses it today: in-tree
  volume plugins, `PodSecurityPolicy`, seccomp annotations, legacy `Ingress` classes,
  `batch/v1beta1 CronJob`. `api_deprecation_scan.py` already covers the `apiVersion`/`kind` part on
  request (see Scope); what is new is the rest and running it on the schedule.
- **Node runtime and image assumptions.** Inventory the container runtime per node pool — a pool
  still on Docker rather than containerd is blocking, and the same check catches the dockershim
  removal. Anything expecting cgroup v1 or a dockershim-era socket mount belongs here too.
  `--readiness` already pairs each pool's containerd major, read from its nodes, with the major the
  target's node image ships (`upgrade_shape_tables.py`, by minor and image family) and names the pods
  mounting the runtime socket when the major changes, and reads each pool's `effectiveCgroupMode`
  against the target for the Java and .NET images below the cgroup v2 floor (the `cgroup-v2-runtime`
  rule); the node's cgroup filesystem and the heap flags are unread.
- **NetworkPolicies no dataplane enforces.** A legacy-dataplane cluster with the network policy
  add-on off and policies in its namespaces enforces nothing today, and all of them take effect at
  the node rebuild that follows enabling the add-on, or on the Dataplane V2 cluster that replaces
  it. `--readiness` already flags the shape, naming the namespaces.
- **In-tree volumes.** A PersistentVolume with an in-tree `gcePersistentDisk` source attaches only
  through CSI migration, so with the PD CSI driver add-on off the pod a drain moves stays Pending.
  `--readiness` already grades every such volume against the add-on, naming the workloads that
  mount it; StorageClasses naming the in-tree provisioner are unread.
- **Unattended OS upgrades on nodes.** A node whose operating system updates itself can lose its
  CNI's routing rules when the package manager restarts networking, and the failure appears on a
  running node rather than a freshly booted one — so a node image that passes a boot test can still
  break in place. Check that automatic OS package upgrades are disabled.

## Drain safety

Upgrading nodes means draining them: evict the pods, delete the node, bring up a new one. Most
upgrade failures are really drain failures. The PodDisruptionBudget, single-replica,
rigid-scheduling and spreading checks already run (see Scope). The additions:

- **How long the drain will actually take.** Count the pods that resist eviction before the window
  is booked, not during it: `emptyDir` volumes without a `safe-to-evict` annotation, and
  `terminationGracePeriodSeconds` outliers. Multiplied by node count, that is the window, and it is
  routinely several times the estimate.
- **No room to add a node.** A pool with `max-surge` of 0, or blue/green needed but not configured,
  or regional quota too tight to fit even one surge node, or a Pod range with no free per-node block
  for the surge node, which then never registers. The upgrade cannot start. `--readiness` already
  grades the pool side: a pool that removes a node before its replacement exists, measured by
  placing each node's pods onto the nodes they may schedule on, blocks when it cannot grow and a pod
  has no node to take it; and the Pod IP side: per Pod range, the free aligned blocks of the pools'
  per-node size against the surge nodes the pools below the target need (the sum of their
  `maxSurge`), fewer blocks than nodes blocking, and a pool with accelerators that surges with no reservation affinity a risk, since the zone may not have the accelerator for the surge node and the upgrade then waits with the old node serving.
  The quota side is unread by the rule; the stockout audit reads regional quota.
- **Workloads pinned to a pool being retired.** A `nodeSelector` or affinity naming a pool that is
  going away leaves those pods nowhere to land.
- **Nobody listening to the upgrade notifications.** GKE publishes `UpgradeEvent` and
  `SecurityBulletinEvent` to Pub/Sub. Checking the topic is configured is not enough — check
  something actually consumes it, or the notifications go nowhere.
- **A load balancer that drains longer than the node's deregistration wait.** Shipped as
  `lb-connection-draining`: the report reads the project's backend services once per project,
  matches each backend to the cluster's node-pool instance groups and to the NEGs its Services
  annotate, and files a blocker per backend service whose `drainingTimeoutSec` is above the 600
  seconds GKE waits per node, with the `gcloud compute backend-services update` command that
  lowers it. The unmanaged `k8s-ig--` groups GKE creates for an instance-group Service of type
  LoadBalancer are not matched; a membership read per group is the change that would.
- **A Node whose virtual machine is gone.** Shipped as `orphan-node-objects`: the report reads the
  project's instances once per project and joins the cluster's Nodes to them on the providerID's
  zone and instance name; a Node with no instance behind it is a blocker with
  `kubectl delete node <name>`, and the note counts nodes against instances on every member so a
  clean cluster reads as graded. The list is read once per project, at its first graded member
  (after that member's own node read, before the later members'), so a member's node read and the
  list are minutes apart at most: a Node created after the list's read time registered in between
  (a surge node, a scale-up; `Ready` `False` for its first seconds), and a `Ready`, uncordoned Node
  the list does not carry left in between; each is a note naming the instants, not a blocker.

## Rollout and verification

Rollout tracking against one target version already runs (see Scope). The additions:

- **A sequence, not a switch.** Upgrade one cluster per family first, let it soak, then the rest —
  and name each family's blocking findings so the order is justified rather than arbitrary.
- **A diff after the upgrade.** Compare each cluster against itself before and after: new
  `CrashLoopBackOff` or `ImagePullBackOff`, webhook latency, pods stuck pending, deprecation
  warnings that were not there before. Without the diff, "the upgrade worked" means "nothing
  obvious caught fire".
  The [upgrade retrospective](upgrade-retrospective.md) does the pod and node part of that diff on
  a schedule (new `CrashLoopBackOff`, `ImagePullBackOff` and Pending pods; not webhook latency or
  deprecation warnings): a review of every new or upgraded cluster at the end of each weekend (a
  review of one cluster soon after its upgrade is designed, not built: the retrospective's
  requirement 1 states the latency), with the failures it finds classified against the catalogue
  and written as guards for the readiness report to read once its reader ships.

  What ships of the sequence and the diff now, on request: the retrospective grades each reviewed
  cluster with a **gate** (`clear`, `risk` or `pending`) from its pod and node diff plus Cloud
  Logging ingestion bytes by container for the seven days before and after the upgrade (a
  container at or above 2.0 times its week before, and at or above 1 GiB in the week after, is a
  `risk`), and lists each family from the `upgrade-family` cluster label with its first-upgraded
  member's gate; the readiness rule `rollout-sequence-precheck` reads the fleet host project's
  existing RolloutSequences, because the platform allows one per host project and a plan that
  creates a second is refused; and the rollout orchestration SOP
  (`agents/platform/governance/upgrade_rollout_orchestration_sop.md`) runs the order: the
  precheck, one cluster per family first, a seven-day soak, the scoped diff and its gate, then
  the rest, with the same order per cluster when the sequence cannot be created. The operator
  creates the sequence and runs every upgrade; the agent writes the plan, the stage
  configuration, the pull requests and the operator steps. The review of one cluster soon after
  its upgrade is still designed, not built: the SOP waits for the soak and runs the scoped diff
  when asked.

## The incident checks

The checks above come from public failures. Seven more come from one production fleet's support
history: sixteen upgrade incidents, read for the Scope table. Thirteen are matched to a check that
fires before the maintenance window; the other three, in two scenarios, have none yet, and the
Progress table names them. Six of the checks are new readiness rules with their own catalogue
entries, 21 to 26; the seventh is two reads added to the `surge-capacity` rule (entry 2), a rule
that is itself implemented and not on `main`. As of 2026-10-21 all seven are implemented and none
is on `main`, whose `EXTRA_RULES` carries one rule; the Scope table records each as it lands, and
the project tracks the delivery in its issue tracker. Each rule is a module of
`scripts/readiness_rules/` in the `fleet-upgrade-verification` skill, registered in `EXTRA_RULES`
and run wherever `--readiness` runs: the skill's fleet run, the daily `upgrade-readiness-watch` and
the weekly `upgrade-blocked` check. The skill's `references/readiness-rules.md`, delivered with the
rules, is the definition of what a rule reads and at what threshold. The table says what each reads
and what it files with the action it names; the incident is why the check exists.

| Check                            | Entry | Reads                                                                                                                                                                                                                                                                                                                        | Files, and the action                                                                                                                                                                                                                                                                                                                                                                                |
| -------------------------------- | ----- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `lb-connection-draining`         | 21    | The project's backend services (`gcloud compute backend-services list`, once per project), matched to the member's node-pool instance groups and to the NEGs its Services annotate                                                                                                                                           | A blocker per backend service whose `connectionDraining.drainingTimeoutSec` is above the 600 seconds GKE waits per node. Action: `gcloud compute backend-services update <name> --global\|--region <region> --connection-draining-timeout=600`                                                                                                                                                       |
| `orphan-node-objects`            | 22    | The project's instances (`gcloud compute instances list`, once per project), joined to the member's Nodes on `spec.providerID`                                                                                                                                                                                               | A blocker per Node with no instance behind it; a Node created after the list's read time, or a `Ready` uncordoned Node the list lacks, is a note. Action: `kubectl delete node <name>`                                                                                                                                                                                                               |
| `control-plane-upgrade-history`  | 23    | The project's operations (`gcloud container operations list`, once per project): `DONE` `UPGRADE_MASTER`, `UPGRADE_NODES` and `REPAIR_CLUSTER` operations of the last 90 days on the member or its pools whose error names capacity; the cluster's maintenance window                                                        | A risk per capacity failure, with the operation's date, its message and the window. Action: set a maintenance window in an off-peak hour for the region and retry the upgrade in it; keep upgrade notifications consumed                                                                                                                                                                             |
| `cluster-status-age`             | 24    | The cluster's `status` and, from the same operations list, the `RUNNING` operations on it or its pools; a cluster not `RUNNING` with nothing running is read again (`clusters describe`, then its own `operations list`)                                                                                                     | A blocker when the holding operation has run for more than four hours, or when nothing runs and the status is still not `RUNNING` (quoting `statusMessage`); a younger holder is a note. Action: freeze changes and, past a few hours, open a support case quoting the operation id, or the status and its message (the `gke-upgrades` skill's "Stuck operation" procedure, delivered with the rule) |
| `target-patch-valid`             | 25    | The location's server config (`gcloud container get-server-config`, once per location): the member's target against its channel's `validVersions` (and `validNodeVersions` for a pool move alone); the running control-plane version against the same list                                                                   | A blocker for a target the channel does not carry; a risk for a running control plane the channel has moved past. Action: target the newest active patch of the minor on the channel, else the next active version it lists                                                                                                                                                                          |
| `known-issues-matrix`            | 26    | The DaemonSets and Deployments in `kube-system` and `gmp-system`, against `known_issues.json` beside the rules (the rows of GKE's known-issues page from 1.32 up) at the running and the target patch; the table's age                                                                                                       | A risk per matched issue, with the add-on, the fix and the page anchor; a match through an open version set is a note; a table older than thirty days is a risk. Action: target the patch the issue names as fixed, or hold; regenerate a stale table                                                                                                                                                |
| `surge-capacity`, two more reads | 2     | For each pool below the target on the surge strategy with `maxSurge` above 0: its accelerators (`config.accelerators`) and reservation affinity (`config.reservationAffinity`); per Pod range (`networkConfig.podIpv4CidrBlock`, else `clusterIpv4Cidr`), the free aligned per-node blocks against the pools' `maxSurge` sum | A blocker for an accelerator pool that surges with no reservation affinity; a blocker when the free blocks are fewer than the surge nodes. Action: hold a reservation for the surge node, or set the in-place strategy (`max-surge` 0, `max-unavailable` 1) on a pool with a spare accelerator node; lower `max-surge` or add a secondary Pod range                                                  |

A finding reaches the operator by the paths the Scope table credits, and by two more, implemented
and not on `main` as of 2026-10-21: the Saturday review, the `upgrade-readiness-weekly-review` job
that grades every cluster and registers each finding on the findings queue, with the brief, the
`upgrade-readiness-weekly-brief` job's chat message that reports them; and the retrospective job
([`upgrade-retrospective.md`](upgrade-retrospective.md)), which runs the collector on a schedule and
records a failure after the upgrade as a guard the next readiness report shows. The collector is on
`main`; its schedule, and the readiness report's reading of its guards, are not.

## Verification

A check is verified at four layers, and the four together are this document's standard for a
check: a check is complete when each layer is on record for it, as a finding, as a clean run, or
as ruled out with the reason.

1. **Replay.** One test per incident with a check, built from recorded inputs shaped like the
   incident, asserting the rule's finding text and its action through the registry's fold
   (`test_incident_replays.py` beside the rules: eleven replays for the six incident rules, two for
   the surge extension).
2. **Live read.** `fleet_upgrade_report.py --readiness` over a development project with nothing
   planted for the check. It proves the reads against the real APIs and shows what a healthy fleet
   reports. On the development project the read found a control-plane upgrade that had failed on
   compute capacity (2026-10-09, on a cluster with no maintenance window) and a control plane whose
   patch the REGULAR channel had dropped, each with the rule's action.
3. **Fixture and evaluation case.** A planted fixture the check finds, and whose own action removes
   it; and the check's evaluation case, red on `main` and green three times on the branch's build
   ([`eval_driven_development.md`](../../.agents/rules/eval_driven_development.md)). For the drain
   check: a backend service draining for 720 seconds on a seeded pool blocks the cluster, the
   finding prints the `gcloud compute backend-services update` command, and after that command the
   rule reports no service above the wait. Where nothing can be planted on a healthy cluster (a
   ghost Node, a capacity failure, a wedged reconcile, a withdrawn patch, a published add-on issue)
   the catalogue entry says why, the replay stands in, and the case grades the rule's note on a
   clean fleet.
4. **End to end.** The Saturday review and the brief, run through the cron path on an integration
   image, with the finding named in chat: seven clusters graded, the findings registered on the
   queue, the brief delivered to the chat channel, and the drain finding in the brief's decide-now
   list with its command in the report.

This round taught two rules of design, each now binding on a check:

- A check declares its own catalogue entry, because the review titles the finding and recommends by
  that entry: four incident rules first declared the nearest existing entry, so the review titled a
  capacity failure "No spare capacity for the displaced pods" with the surge action, and entries 23
  to 26 are the fix.
- An evaluation case grades the check's own text, because a line the agent can compose from raw
  reads proves nothing about the check: `upgrades-readiness-cluster-status-age` and
  `upgrades-readiness-target-patch-valid` passed on `main` without the rule, and the per-object
  `upgrades-fleet-readiness-*` cases graded a line the agent never produced, so every case now
  copies the rule's note or finding as the report prints it.

## Progress

The state of each check at 2026-10-21, one row per incident scenario, with the sixteen incidents by
date; a replayed incident carries the date its replay names. The Scope table says what is on `main`;
this table is a dated record of each check's proof, in five words, not the run-by-run record.
**done**: the layer is on record for the check, and the cell says what it showed where it showed a
finding. **ran**: the layer ran with the check in it and reported no finding of this check, which is
the clean run the standard accepts. **none**: the layer does not apply, and the cell says why.
**pending**: the layer is under way, and the cell says what remains. **open**: not started.

| Scenario                                                                     | Incidents                                      | Check                                                                                            | Replay                                             | Live read                                                                                                                                 | Fixture                                                                                                                                                                                                             | Evaluation case                                                                                                                                                          | End to end                                                                                                                                      |
| ---------------------------------------------------------------------------- | ---------------------------------------------- | ------------------------------------------------------------------------------------------------ | -------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------- |
| The load balancer's connection drain outlasts the node's deregistration wait | 2025-06-26, 2025-07-03, 2026-03-31             | `lb-connection-draining` (21)                                                                    | done, three                                        | ran                                                                                                                                       | done: a backend service draining for 720 seconds on a seeded pool; the rule blocks, and after its command reports no service above the wait                                                                         | pending: `upgrades-readiness-lb-drain-timeout` is green three times on the branch's build; the red against `main` is not yet on record                                   | done: named in the brief's decide-now list, with its command in the report                                                                      |
| A Node object outlives its virtual machine                                   | 2025-12-09                                     | `orphan-node-objects` (22)                                                                       | done                                               | ran                                                                                                                                       | none: the cloud controller deletes a ghost Node within seconds                                                                                                                                                      | pending: `upgrades-readiness-orphan-node` has three greens on the branch's build, re-graded at the final pattern; the red against `main` is not yet on record            | ran                                                                                                                                             |
| A cluster held in a long reconcile                                           | 2025-06-17                                     | `cluster-status-age` (24)                                                                        | done                                               | ran                                                                                                                                       | none: a status other than `RUNNING` needs a real operation in flight or wedged                                                                                                                                      | done: `upgrades-readiness-cluster-status-age`, red on `main`, green three times                                                                                          | ran                                                                                                                                             |
| A control-plane upgrade or resize fails on regional compute capacity         | 2025-06-19, 2026-01-14, 2026-04-15, 2026-10-05 | `control-plane-upgrade-history` (23)                                                             | done, four                                         | done: a real control-plane upgrade that failed on capacity on 2026-10-09                                                                  | none: a capacity failure is the provider's                                                                                                                                                                          | pending: `upgrades-audit-control-plane-capacity`, red on `main`; three passes in five runs over two builds, and three greens of three on one build are not yet on record | done: the finding was in the review, titled by entry 2 until the rule declared entry 23                                                         |
| The target patch is withdrawn before the window                              | 2026-05-05                                     | `target-patch-valid` (25)                                                                        | done                                               | done: a control plane whose patch the REGULAR channel had dropped                                                                         | done: the version laggard, a standing fixture, holds its control plane at a patch the channel has moved past, which is the rule's risk; no withdrawn target can be planted, the channel's list being the provider's | done: `upgrades-readiness-target-patch-valid`, red on `main`, green three times                                                                                          | done: the finding was in the review, titled by entry 10 until the rule declared entry 25                                                        |
| A known issue in an add-on at the target version                             | 2026-01-13                                     | `known-issues-matrix` (26)                                                                       | done                                               | done: two published issues matched on the version laggard                                                                                 | none: which issues the page lists is the provider's                                                                                                                                                                 | pending: `upgrades-readiness-known-issues` has two greens of three; the third green and the red against `main` are not yet on record                                     | ran                                                                                                                                             |
| A GPU pool upgrade stalls on accelerator capacity without a reservation      | 2025-04-28                                     | `surge-capacity`, accelerator read (2)                                                           | done                                               | pending: the seeded fleet has no accelerator pool; the read ran on a GPU cluster's record from the development project                    | open: an accelerator pool costs by the hour on every pool project, and the seeded fleet has no surge-headroom fixture yet                                                                                           | open: `upgrades-readiness-surge-headroom` needs that fixture first                                                                                                       | ran                                                                                                                                             |
| A new pool cannot register nodes on a full Pod range                         | 2025-09                                        | `surge-capacity`, Pod-range read (2)                                                             | done                                               | ran                                                                                                                                       | open: a Pod range is fixed at cluster creation, so the fixture is a small new cluster slot, part of the same surge-headroom fixture                                                                                 | open: the same case, after the fixture                                                                                                                                   | ran                                                                                                                                             |
| Upgrade sequencing                                                           | 2026-09-17, 2026-09-21                         | `rollout-sequence-precheck` (plan-level) and the rollout orchestration SOP                       | done, on the recorded sequence list                | ran: the dev project's standing `seeded-train` sequence, read live on 2026-10-23 (the API's selector grammar and catch-all stage with it) | the standing sequence, a fleet and the GKE Hub role on the host project (dev: in place; pool projects: open)                                                                                                        | `upgrades-readiness-rollout-sequence-precheck` (parked until the pool projects carry the fixture) and `upgrades-rollout-family-held-by-canary-gate`                      | shipped: the precheck before the plan, one cluster per family first, the soak, the gate, the rest, and the manual path per cluster              |
| The log volume rises after the upgrade                                       | 2026-04-21                                     | the retrospective's gate: `upgrade-log-volume-rose` and the per-cluster `clear`/`risk`/`pending` | done, on two recorded weeks of a development fleet | ran: the two weeks read through the credential proxy's Monitoring route on 2026-10-22                                                     | the recorded-weeks report the stack `upgrade-retro-recorded-weeks` seeds in the store                                                                                                                               | `upgrades-retrospective-log-volume-gate`                                                                                                                                 | shipped: the post-upgrade diff as the family gate with log volume; a gate read while its after-week was open is read again on the next full run |

## How it is delivered

These checks are one audit capability on the
[capability delivery vehicle](capability-delivery-vehicle.md), which gives them five properties
that are not built here: it **ships with the agent**; it **runs on a weekly schedule**; it can be
**asked for in chat** at any time, for the whole fleet or one family; its criteria are
**customizable**, held apart from the procedure so an operator can change a threshold by agreeing
the edit with the agent; and it is **self-learning**, refining those criteria from what the
operator says, within limits the operator sets.

What ships of that today is the schedule and the chat half, through the Platform Agent's cron roster
rather than the vehicle: the weekly `upgrade-readiness-weekly-review` job grades every cluster in
scope on Saturday and registers each blocker and risk on the findings queue, which carries it to a
decision (dismiss, snooze, accept) and reminds until one is made; the `upgrade-readiness-weekly-brief`
job that follows composes the week's message from the review and the queue; and the daily
`upgrade-readiness-watch` grades a cluster the day a new version appears for it. The family
verdict ships as the retrospective's gate and families list (`go`, `hold`, `pending`), read by the
rollout orchestration SOP; the customizable criteria and the self-learning stay specified here and
unbuilt.
