# Upgrade readiness checks for a large GKE fleet

**Status:** requirements. Nothing here is built beyond what the Scope table credits to an existing
audit or skill.

## The problem, in one paragraph

Kubernetes ships a new minor version every few months, and GKE clusters have to follow. Before a
team upgrades, somebody has to answer one question: **will anything break?** Answering it today
means opening several tools and checking by hand — is anything still calling an API this version
deletes, are the add-ons compatible, will the pods survive nodes being drained one at a time — and
then repeating all of it for the next group of clusters. A team running hundreds of clusters does
this over and over. This document lists the checks an agent should run on a schedule instead, so
the answer is waiting for them.

Two things to know about the checks. They are **read-only**: they look and report, and never
upgrade anything — that stays a human's decision. And they are **grouped by cluster family**, a
family being a set of clusters built from the same template, because that is the unit a team
actually upgrades.

## Upgrades that went wrong in public

Every check below exists because the thing it looks for has already taken a real company down.
These are published postmortems and provider incident reports, not hypotheticals. Each one names
the check that would have caught it.

**Reddit, 14 March 2023 — 314 minutes down.** Reddit upgraded a large cluster from Kubernetes 1.23
to 1.24. The network died about two minutes later. Their CNI, Calico, picked its route reflectors —
the nodes every other node peers with — by matching the label `node-role.kubernetes.io/master`.
Kubernetes 1.24 removed that label from running clusters. The selector matched nothing, every node
dropped every route, and the cluster went dark. There is no supported Kubernetes downgrade, so
recovery meant a restore procedure that had never been run against production. The selector lived
only in Calico's own datastore, hand-edited and committed nowhere, so scanning the GitOps
repository would not have found it either.
[Postmortem](https://web.archive.org/web/20260826130053/https://www.reddit.com/r/RedditEng/comments/11xx5o0/you_broke_reddit_the_piday_outage/)
(the Wayback copy; Reddit blocks automated fetches of the original).
_Caught by:_ diffing the target release's removed **identifiers** — labels as well as API versions —
against the selectors actually in use in the live cluster, including add-on configuration.

**Jetstack, September 2019 — a fail-closed webhook deadlocked a GKE control plane.** A regional GKE
master upgrade hung past its 20-minute timeout. When the second master came up, kube-apiserver's
startup hook tried to write a ConfigMap in `kube-system`. A ValidatingWebhookConfiguration backed by
Open Policy Agent, scoped cluster-wide and set to `failurePolicy: Fail`, intercepted that write. OPA
did not answer, the write timed out, the master failed its health check and crash-looped. The
resulting API downtime stopped kubelets reporting node health, so GKE node auto-repair began
destroying and recreating every node in a loop, taking out every tenant.
[Postmortem](https://web.archive.org/web/20230607064526/https://www.jetstack.io/blog/gke-webhook-outage/).
_Caught by:_ listing every webhook with `failurePolicy: Fail`, and flagging any whose rules or
namespace selector can match `kube-system` or cluster-scoped objects, or whose backend has one
replica and no liveness probe.

**loveholidays, March 2019 — a 2-hour GKE upgrade window ran 7 hours.** Going from 1.10 to 1.12,
individual node drains took 15 minutes or more: pods used `emptyDir`, which blocks eviction unless
annotated `safe-to-evict`, and some workloads had a `terminationGracePeriodSeconds` of several
minutes. One node pool upgrade hung with no drains happening at all and could not be cancelled from
the console. Separately, GKE had withdrawn the exact patch version they were upgrading to five days
earlier, and nobody re-read the release notes on the day.
[Write-up](https://deploy.live/blog/the-shipwreck-of-gke-cluster-upgrade/).
_Caught by:_ estimating drain time before the window — count pods with `emptyDir` and no
`safe-to-evict` annotation, and grace-period outliers, then multiply by node count — and re-checking
the target patch version against the release notes at run time rather than at planning time.

**Google Cloud, September 2022 — Calico wedged every drain on GKE 1.22 and later.** A race condition
in Calico made the CNI fail pod teardown with an authorization error, leaving pods stuck in
Terminating or Pending across 35+ regions. Every 1.22 and 1.23 release was affected, and 1.24 up to
`1.24.4-gke.800`. Clusters running the autoscaler were hit hardest, because more node churn meant
more teardowns. [Incident report](https://status.cloud.google.com/incidents/urNR4xD4gBNsyaZj3W1i).
_Caught by:_ checking installed add-on versions against the provider's known-issues list for the
**specific target patch version**, not just the minor. A check comparing minors would have waved
this cluster straight into the bug.

**Google Cloud, July–September 2021 — 59 days of auto-upgrades restarting containers.** Clusters on
the REGULAR channel were automatically moved from 1.19 to 1.20. On any node pool still using Docker
rather than containerd, every container on a node restarted whenever the Docker daemon did. Google
eventually paused automatic upgrades to stop it spreading.
[Incident report](https://status.cloud.google.com/incidents/vFhgfrfzzrx6zQo69SdQ).
_Caught by:_ inventorying the container runtime per node pool and treating "still on Docker" as
blocking. The same check is what catches the 1.24 dockershim removal.

**Datadog, March 2023 (~50 hours, five regions) and Heroku, June 2025 (~24 hours) — the same
failure, twice.** In both cases an unattended OS package upgrade ran on production Kubernetes nodes
and restarted the node's networking service. Datadog's systemd version defaulted to deleting routing
rules it had not created, which included the ones Cilium installed for pod networking; Heroku's
networking config was applied by a script that only ran at first boot. Nodes fell off the network en
masse — Datadog lost roughly 60% of its compute. Neither showed up in staging, because on a cold
boot the ordering is fine; the bug only exists when the package is upgraded under a running node.
[Datadog](https://www.datadoghq.com/blog/2023-03-08-multiregion-infrastructure-connectivity-issue/),
[Heroku](https://www.heroku.com/blog/summary-of-june-10-outage/).
_Caught by:_ asserting that unattended OS upgrades are disabled on nodes, and testing node images by
upgrading a running node rather than booting a fresh one.

**Spinnaker on GKE, September 2023 — a controller polling a removed API froze the upgrade.**
Spinnaker's clouddriver called `policy/v1beta1/podsecuritypolicies` more than once a minute. GKE's
deprecation insights saw the traffic and **paused the cluster's automatic upgrade to 1.25**. The
cluster was not behind by accident; GKE was refusing to move it.
[Issue thread](https://github.com/spinnaker/spinnaker/issues/6880).
_Caught by:_ reading the deprecation insights and reporting a paused auto-upgrade as the reason a
cluster is lagging — the check in the GKE deprecation insights section below.

**Helm releases, from Kubernetes 1.25 — deploys blocked by a manifest nobody was running.** After
1.25 removed PodSecurityPolicy, `helm upgrade` fails on any release whose **stored** manifest
contains one. Nothing crashes; the workloads keep running. You simply cannot deploy anything until
you rewrite the stored release with `helm mapkubeapis`. Removing the PSP from your chart does not
help, because the failure is in reading the old release Secret.
[Issue thread](https://github.com/helm/helm/issues/11287).
_Caught by:_ scanning stored Helm release state and GitOps manifests for removed kinds, not only
live objects. GKE's deprecation insights cannot see this: they are generated from live API-server
traffic over a 30-day window, so an unused manifest that gets applied after the upgrade is invisible
to them
([GKE documentation](https://docs.cloud.google.com/kubernetes-engine/docs/deprecations/viewing-deprecation-insights-and-recommendations)).

Two gaps worth stating. **Surge and quota** has no public postmortem behind it — the mechanism is
documented by the providers rather than by victims, so the citation for it is
[GKE's own upgrade quota page](https://cloud.google.com/kubernetes-engine/docs/how-to/node-upgrades-quota)
rather than an incident. **Zonal volumes** is similar: the strongest artefact is Google stating that
a cluster upgrade deletes the underlying instances and therefore all data on Local SSD, and
recommending that node pools holding persistent data not be auto-upgraded at all
([GKE documentation](https://cloud.google.com/kubernetes-engine/docs/concepts/local-ssd)).

## Scope

Some of this already runs. The table says what, so nobody builds it twice. The SOP or skill named
is the real definition of what that check does and at what threshold; this document does not
repeat it. **The third column is the work.**

| Area                                   | Already on `main`                                                                                                                                                                                                                                                                                                                                                                                                                                                | New in this document                                                                                                                                                                                                   |
| -------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Version posture                        | `security-patch-orchestrator` (`agents/platform/governance/security_patch_orchestrator_sop.md`): control plane behind its channel, node-pool skew, fleet spread, no channel, auto-upgrade and auto-repair off, no maintenance window, blocking exclusions, stale image types, notifications not configured. The `fleet-upgrade-verification` skill grades every cluster against one `--target-version` on request.                                               | A target version and date **per family**, and days remaining against that date. GKE's API exposes no end-of-support date (the SOP records why calendar EOL was dropped), so the release calendar is an operator input. |
| GKE deprecation insights               | Nothing scheduled. The `gke-upgrades` skill's checklist template tells a human to look at the insights dashboard or grep the API server's deprecated-request metric; no job reads the Recommender API.                                                                                                                                                                                                                                                           | Everything in the section.                                                                                                                                                                                             |
| Workload compatibility                 | The `fleet-upgrade-verification` skill's `api_deprecation_scan.py` scans every managed GitOps repository's manifests for `apiVersion`s the target version removes, reporting each hit with its replacement at the commit it read; it runs on request, not on a schedule.                                                                                                                                                                                         | The add-on compatibility matrix, admission-webhook posture, and image assumptions (cgroup v1, dockershim-era mounts) — and putting the existing manifest scan on the schedule.                                         |
| Drain safety and disruption            | `obtainability-audit` (`agents/platform/governance/obtainability_audit_sop.md`): multi-replica workloads with no PodDisruptionBudget, drain-blocking budgets, single-replica Service-backed Deployments, rigid scheduling, missing spread. On request, `fleet-upgrade-verification --readiness` grades drain-blocking PDBs, upgrade-covering maintenance exclusions and node-pool skew per member against a chosen target, emitting `blocked`/`unknown`/`ready`. | Surge and blue/green capacity per pool, workloads pinned to a pool being retired, whether anything consumes the cluster's upgrade notifications.                                                                       |
| Rollout orchestration and verification | The `fleet-upgrade-verification` skill reports, run over run, which clusters started, completed or stalled against one target version.                                                                                                                                                                                                                                                                                                                           | Canary sequencing per family with each family's blocking findings, and the post-upgrade diff.                                                                                                                          |

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
`blocked`/`unknown`/`ready` per cluster from drain-blocking PDBs, maintenance exclusions and skew.
Three things are missing — the verdict is per cluster, not per family; it reads three inputs rather
than every check here; and it runs only when asked, not on the schedule.

## Version posture

Already served (see Scope). One addition:

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
  each family runs and check it against the target version's support matrix.
- **Admission webhooks that block drains.** A webhook with `failurePolicy: Fail`, no
  `timeoutSeconds`, or cluster-wide scope will stall the upgrade when its own backend restarts
  mid-drain — the webhook rejects everything while it is down, including the pods the upgrade is
  trying to move.
- **Manifests in Git, not just live clusters.** A removed field or API version sitting in the
  GitOps repository breaks on the next sync even if nothing in the cluster uses it today: in-tree
  volume plugins, `PodSecurityPolicy`, seccomp annotations, legacy `Ingress` classes,
  `batch/v1beta1 CronJob`. `api_deprecation_scan.py` already covers the `apiVersion`/`kind` part on
  request (see Scope); what is new is the rest and running it on the schedule.
- **Images with old assumptions.** Anything expecting cgroup v1 or a dockershim-era socket mount.

## Drain safety

Upgrading nodes means draining them: evict the pods, delete the node, bring up a new one. Most
upgrade failures are really drain failures. The PodDisruptionBudget, single-replica,
rigid-scheduling and spreading checks already run (see Scope). The additions:

- **No room to add a node.** A pool with `max-surge` of 0, or blue/green needed but not configured,
  or regional quota too tight to fit even one surge node. The upgrade cannot start.
- **Workloads pinned to a pool being retired.** A `nodeSelector` or affinity naming a pool that is
  going away leaves those pods nowhere to land.
- **Nobody listening to the upgrade notifications.** GKE publishes `UpgradeEvent` and
  `SecurityBulletinEvent` to Pub/Sub. Checking the topic is configured is not enough — check
  something actually consumes it, or the notifications go nowhere.

## Rollout and verification

Rollout tracking against one target version already runs (see Scope). The additions:

- **A sequence, not a switch.** Upgrade one cluster per family first, let it soak, then the rest —
  and name each family's blocking findings so the order is justified rather than arbitrary.
- **A diff after the upgrade.** Compare each cluster against itself before and after: new
  `CrashLoopBackOff` or `ImagePullBackOff`, webhook latency, pods stuck pending, deprecation
  warnings that were not there before. Without the diff, "the upgrade worked" means "nothing
  obvious caught fire".

## How it is delivered

These checks are one audit capability on the
[capability delivery vehicle](capability-delivery-vehicle.md), which gives them five properties
that are not built here: it **ships with the agent**; it **runs on a weekly schedule**; it can be
**asked for in chat** at any time, for the whole fleet or one family; its criteria are
**customizable**, held apart from the procedure so an operator can change a threshold by agreeing
the edit with the agent; and it is **self-learning**, refining those criteria from what the
operator says, within limits the operator sets.
