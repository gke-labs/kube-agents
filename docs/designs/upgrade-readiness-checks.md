# Upgrade readiness checks for a large GKE fleet

## Purpose

A platform team running GKE at scale — millions of cores, dozens of cluster families (fleets of
clusters built to one template), and multi-tenant clusters holding thousands of namespaces each —
assesses readiness for every Kubernetes minor-version bump by hand. Several tools are stitched
together to find deprecated API callers, check add-on compatibility, and confirm workloads will
survive a node drain, and the exercise is repeated per cluster family.

This document lists the checks such an operator would realistically ask the Kube-Agent to run on a
schedule ahead of an upgrade, as candidate criteria for a scheduled audit. Each check is
read-only: it inspects the fleet and reports a finding, with an owner and a run-over-run delta,
and never upgrades a cluster.

## How it is delivered

The checks are one capability on the shared
[capability delivery vehicle](capability-delivery-vehicle.md), which gives them five properties
without any of them being built here: they ship **pre-defined** with the agent; they run
**scheduled** as a weekly cron job; they are **triggerable** from chat at any time, for the whole
fleet or for one family ahead of its upgrade window; they are **customizable**, in that the
criteria below — target versions, compatibility matrix, drain-safety rules, blocking insight
types — are held apart from the procedure so the operator can revise them by describing the change
to the agent and agreeing the edit; and they are **self-learning**, in that the agent revises those
criteria on its own after reflecting on conversations, within the limits the vehicle sets. This
document lists only the checks.

## Version posture

- Control plane behind its release-channel target; node pools skewed from the control plane;
  fleet-wide minor-version spread wider than a set number of versions.
- Clusters on no release channel; auto-upgrade or auto-repair disabled; no maintenance window; a
  maintenance exclusion that blocks the next minor.
- Deprecated or unoffered node image variants.
- Days to end of support per cluster against a per-family target ("family X on 1.32 by date D"),
  from `gcloud container get-server-config`.

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

## Workload compatibility

- Inventory third-party add-ons per family (service mesh, cert-manager, monitoring agents, GitOps
  controllers, CSI and CNI drivers, custom admission webhooks) and their versions against the
  target Kubernetes minor's support matrix.
- Admission webhooks with `failurePolicy: Fail`, no `timeoutSeconds`, or cluster-wide scope: these
  stall an upgrade when the webhook backend restarts mid-drain.
- Manifests in the operator's GitOps repositories, not only live clusters, that use removed
  fields or defaults: in-tree volume plugins, `PodSecurityPolicy`, seccomp annotations, legacy
  `Ingress` classes, `batch/v1beta1 CronJob`.
- Images that assume cgroup v1 or a dockershim-era socket mount.

## Drain safety and disruption

- PodDisruptionBudgets that can never be satisfied (`maxUnavailable: 0`, or `minAvailable` equal
  to the replica count); single-replica Deployments and StatefulSets with no PDB; bare pods;
  pods holding state in `emptyDir`.
- Node pools with `max-surge` 0, blue/green needed but not configured, or regional quota that
  cannot fit a surge node.
- Workloads pinned by `nodeSelector` or affinity to a pool being retired.
- Whether the cluster's `UpgradeEvent` and `SecurityBulletinEvent` Pub/Sub notifications are
  configured, and whether anything consumes them.

## Rollout orchestration and verification

- A proposed sequence: canary one cluster per family, soak, then the rest — with each family's
  blocking findings named.
- A post-upgrade diff per cluster: new `CrashLoopBackOff` or `ImagePullBackOff`, webhook latency,
  pending pods, new deprecation warnings that were not there before.
