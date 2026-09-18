# Upgrade readiness checks for a large GKE fleet

**Status:** requirements. Nothing in this document is built beyond what the Scope table credits to
an existing audit or skill.

## Scope

Scheduled audits and one skill already cover part of this ground. The table maps each area below to
what runs it today; the SOP or skill named is canonical for exactly what it checks and at what
threshold, and this document does not restate those checks; each job's cadence is in the
[cron jobs reference](../site/src/content/docs/reference/cron-jobs.md). What a builder picks up from
here is the third column.

| Area                                   | Already on `main`                                                                                                                                                                                                                                                                                                                                                                                                                                                | New in this document                                                                                                                                                                                                   |
| -------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Version posture                        | `security-patch-orchestrator` (`agents/platform/governance/security_patch_orchestrator_sop.md`): control plane behind its channel, node-pool skew, fleet spread, no channel, auto-upgrade and auto-repair off, no maintenance window, blocking exclusions, stale image types, notifications not configured. The `fleet-upgrade-verification` skill grades every cluster against one `--target-version` on request.                                               | A target version and date **per family**, and days remaining against that date. GKE's API exposes no end-of-support date (the SOP records why calendar EOL was dropped), so the release calendar is an operator input. |
| GKE deprecation insights               | Nothing scheduled. The `gke-upgrades` skill's checklist template tells a human to look at the insights dashboard or grep the API server's deprecated-request metric; no job reads the Recommender API.                                                                                                                                                                                                                                                           | Everything in the section.                                                                                                                                                                                             |
| Workload compatibility                 | The `fleet-upgrade-verification` skill's `api_deprecation_scan.py` scans every managed GitOps repository's manifests for `apiVersion`s the target version removes, reporting each hit with its replacement at the commit it read; it runs on request, not on a schedule.                                                                                                                                                                                         | The add-on compatibility matrix, admission-webhook posture, and image assumptions (cgroup v1, dockershim-era mounts) — and putting the existing manifest scan on the schedule.                                         |
| Drain safety and disruption            | `obtainability-audit` (`agents/platform/governance/obtainability_audit_sop.md`): multi-replica workloads with no PodDisruptionBudget, drain-blocking budgets, single-replica Service-backed Deployments, rigid scheduling, missing spread. On request, `fleet-upgrade-verification --readiness` grades drain-blocking PDBs, upgrade-covering maintenance exclusions and node-pool skew per member against a chosen target, emitting `blocked`/`unknown`/`ready`. | Surge and blue/green capacity per pool, workloads pinned to a pool being retired, whether anything consumes the cluster's upgrade notifications.                                                                       |
| Rollout orchestration and verification | The `fleet-upgrade-verification` skill reports, run over run, which clusters started, completed or stalled against one target version.                                                                                                                                                                                                                                                                                                                           | Canary sequencing per family with each family's blocking findings, and the post-upgrade diff.                                                                                                                          |

## Purpose

A platform team running GKE at scale — millions of cores, dozens of cluster families (fleets of
clusters built to one template), and multi-tenant clusters holding thousands of namespaces each —
assesses readiness for every Kubernetes minor-version bump by hand. Several tools are stitched
together to find deprecated API callers, check add-on compatibility, and confirm workloads will
survive a node drain, and the exercise is repeated per cluster family.

This document lists the checks such an operator would realistically ask the Platform Agent to run on
a schedule ahead of an upgrade, as candidate criteria for a scheduled audit. Each check is
read-only: it inspects the fleet and reports a finding, with an owner and a run-over-run delta,
and never upgrades a cluster.

## How it is delivered

These checks are delivered as one audit capability on the
[capability delivery vehicle](capability-delivery-vehicle.md), which gives them five properties,
none of them built here: it ships **pre-defined** with the agent; it runs **scheduled** as a weekly
job; it is **triggerable** from chat at any time, for the whole fleet or for one family ahead of
its upgrade window; they are **customizable**, in that the criteria below are held apart from the procedure so an
operator can revise them by describing the change to the agent and agreeing the edit; and they are
**self-learning**, in that the agent refines those criteria from what it learns in those
conversations, within limits the operator sets. This document lists only the checks; the delivery
requirements are a separate design.

## The verdict

The output is a decision, not an inventory: an operator's job here is deciding whether an upgrade
can proceed, and a list of unranked findings leaves that triage manual. Every check declares a
tier — **Blocker** (the upgrade cannot proceed: an active auto-upgrade pause, an unsatisfiable
PodDisruptionBudget, a surge the quota cannot fit), **Risk** (proceed with attention: an add-on
lagging the target version's support), or **Advisory** — and each run emits a machine-readable
verdict per cluster family: `Go`, `No-Go`, or `Action required`, naming the blocking findings. A
platform lead can wire that verdict into a release pipeline as a gate; the finding list is the
evidence behind it, not the deliverable.

`fleet-upgrade-verification --readiness` is the nearest thing on `main`: per member, against a
chosen target, it already emits `blocked`/`unknown`/`ready` from drain-blocking PDBs, maintenance
exclusions and skew. What this document adds is the aggregation a release gate needs — a verdict
per **cluster family** rather than per member, every check here tiered into it rather than that
script's three inputs, and the verdict produced by the scheduled audit rather than only on
request.

## Version posture

Served today by `security-patch-orchestrator` and, on request, `fleet-upgrade-verification` (see
Scope). The addition:

- A target version and date per cluster family, supplied by the operator from the GKE release
  calendar, and the days remaining per cluster against its family's date — so a family falling
  behind its own plan is reported before it falls behind the channel.

## GKE deprecation insights

GKE publishes per-cluster
[deprecation insights](https://docs.cloud.google.com/kubernetes-engine/docs/deprecations/viewing-deprecation-insights-and-recommendations)
through the Recommender API (`google.container.DiagnosisInsight`): calls to APIs removed in the
next minor, deprecated authentication methods, old node images, and similar. GKE pauses
auto-upgrade on a cluster with an unresolved insight, so a cluster silently pinned to an old
version is a finding in its own right.

- Read insights for every cluster in scope and report them per cluster family.
- Join each insight to who has to fix it: the calling user agent and service account (from the
  insight detail and `k8s.io/deprecated` audit-log annotations) and the owning team of the
  namespace.
- Confirm the `apiserver_requested_deprecated_apis` metric is quiet for the APIs the _target_
  version removes, not only the current one.
- Track resolution across runs so a team sees "12 callers last week, 3 now".
- Report "auto-upgrade paused by an unresolved insight" as the cause when a cluster is behind its
  channel for that reason, rather than as an unexplained lag.

## Workload compatibility

- Inventory third-party add-ons per family (service mesh, cert-manager, monitoring agents, GitOps
  controllers, CSI and CNI drivers, custom admission webhooks) and their versions against the
  target Kubernetes minor's support matrix.
- Admission webhooks with `failurePolicy: Fail`, no `timeoutSeconds`, or cluster-wide scope: these
  stall an upgrade when the webhook backend restarts mid-drain.
- Manifests in the operator's GitOps repositories, not only live clusters, that use removed
  fields or defaults: in-tree volume plugins, `PodSecurityPolicy`, seccomp annotations, legacy
  `Ingress` classes, `batch/v1beta1 CronJob`. `api_deprecation_scan.py` already does the
  `apiVersion`/`kind` half of this on request (see Scope); what is new is the rest of the surface
  and running it on the audit's schedule rather than only when asked.
- Images that assume cgroup v1 or a dockershim-era socket mount.

## Drain safety and disruption

The PodDisruptionBudget, single-replica, rigid-scheduling and spreading checks are served today by
`obtainability-audit` (see Scope). The additions:

- Node pools with `max-surge` 0, blue/green needed but not configured, or regional quota that
  cannot fit a surge node.
- Workloads pinned by `nodeSelector` or affinity to a pool being retired.
- Whether anything consumes the cluster's `UpgradeEvent` and `SecurityBulletinEvent` Pub/Sub
  notifications, beyond their being configured.

## Rollout orchestration and verification

Run-over-run rollout tracking against one target version is served today by
`fleet-upgrade-verification` (see Scope). The additions:

- A proposed sequence: canary one cluster per family, soak, then the rest — with each family's
  blocking findings named.
- A post-upgrade diff per cluster: new `CrashLoopBackOff` or `ImagePullBackOff`, webhook latency,
  pending pods, new deprecation warnings that were not there before.
