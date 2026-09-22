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
release Secret. Operators in the thread report that `helm mapkubeapis` does not fully resolve it
either — it rewrites `apiVersion`/`kind` pairs rather than removing a kind the new version does not
serve ([issue thread](https://github.com/helm/helm/issues/11287)).
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

- Re-read the target patch version's release notes and known issues **at run time**, not at
  planning time. A provider can withdraw a patch version between the plan and the window, and a
  withdrawn version stays selectable.
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
  patch.
- **Admission webhooks that fail closed.** A webhook with `failurePolicy: Fail` rejects everything
  it matches while its own backend is down, which is exactly what happens when the node that backend
  runs on is drained. Report the scope as well as the policy: a webhook whose rules or namespace
  selector can match `kube-system` or a cluster-scoped object can block the control plane's own
  writes during an upgrade, not just the pods being moved. Report the backend too — one replica with
  no liveness probe is what turns a slow webhook into a stuck one.
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
