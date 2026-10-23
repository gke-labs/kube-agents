---
name: gke-upgrades
metadata:
  version: "1.1.0"
  category: Containers
description: >-
  Plans, executes, and validates Google Kubernetes Engine (GKE) cluster upgrades
  and maintenance operations for both Standard and Autopilot clusters. Produces
  upgrade plans, pre/post-upgrade checklists, maintenance runbooks with gcloud
  commands, release channel strategy, and troubleshooting guides. Handles node
  pool upgrade strategies (surge, blue-green), version compatibility, PDB
  management, and workload-specific concerns (stateful, GPU, operators). Use this
  skill whenever the user mentions GKE upgrades, Kubernetes version bumps, node
  pool maintenance, GKE patching, cluster version management, release channel
  selection, maintenance windows, surge upgrades, stuck upgrades, or any GKE
  lifecycle management task — even casual mentions like "we need to upgrade our
  clusters" or "plan our next GKE maintenance" or "our upgrade is stuck." Don't
  use for GKE cluster creation, application onboarding, general networking/routing
  setup, or security policy configurations (use gke-basics or relevant GKE skills
  instead).
---

# GKE Upgrades & Maintenance

Produce clear, actionable documents — upgrade plans, runbooks, or checklists — tailored to the user's environment. Output should be specific to their cluster mode, release channel, version, and workload types rather than generic advice.

Always frame guidance around the auto-upgrade model: auto-upgrade with maintenance windows and exclusions is the preferred control mechanism.

> A change goes out as a pull request to the GitOps repository, never as a write from here: Kubernetes objects as YAML through `gke-manifest-generation`, cluster settings and other Google Cloud resources as Config Connector YAML through `gcp-config-connector`, opened with `submit-suggestion`. Each profile's config offers only the `gke` MCP server's read tools, and the broker refuses `kubectl` and `gcloud` writes. A control-plane upgrade reaches the cluster as a pull request changing the version floor or release-channel field (`spec.minMasterVersion` or `spec.releaseChannel.channel` on the `ContainerCluster`). A node-pool version change (an upgrade, a downgrade or rollback, or resuming a stopped upgrade) is an operation, not desired state: write the exact `gcloud container clusters upgrade CLUSTER_NAME --node-pool NODE_POOL_NAME --cluster-version VERSION --location LOCATION` command as a step for the platform operator to run, not one to run from here, and never propose `spec.version` on a `ContainerNodePool`. A pool on a release channel cannot turn auto-upgrade off, so a version pinned in the repository fights it: Config Connector moves the pool back to the pin after each automatic upgrade, re-rolling its nodes, and a rollback pin holds the pool at the old version indefinitely. The pool's upgrade strategy (`spec.upgradeSettings`) is desired state and stays a pull request.

## Context Gathering

Before producing any upgrade artifact, establish:

- **Cluster mode** — Standard or Autopilot? (Autopilot has no node pool management, mandatory resource requests, no SSH)
- **Current and target versions** — Node version skew must be within 2 minor versions of control plane.
- **Release channel** — Rapid, Regular, Stable, or Extended.
- **Environment topology & Rollout Sequencing** — Single vs multi-cluster, dev/staging/prod tiers, and whether Rollout Sequencing is used.
- **Workload sensitivity** — StatefulSets, databases, GPU, long-running batch need special handling.

If the user provides these upfront, skip straight to the deliverable. If they're vague, fill in reasonable defaults and flag assumptions.

## Core Principles

GKE versions follow Kubernetes version terminology: **Major.Minor.Patch** (e.g., 1.30.1-gke.1187000). A **Minor** version bump (e.g., 1.29 → 1.30) introduces new features and APIs. A **Patch** version bump (e.g., 1.30.1 → 1.30.2) introduces security and bug fixes. Ensure the user understands this distinction.

1. **Sequential control plane, skip-level node pools** -- Control plane upgrades are sequential (N → N+1 → N+2). Node pools support skip-level (N+2) upgrades.
2. **Control plane first** -- Control plane must be upgraded before node pools. Nodes can trail by up to 2 minor versions.
3. **Environment progression** -- Always upgrade dev/staging before production. Use **Rollout Sequencing** (preferred) to automate and enforce this progression across environments (e.g., dev → staging → prod), or manually coordinate version progression if Rollout Sequencing is not used.
4. **Workload-aware** -- Upgrade strategy depends on what's running (stateless, stateful, GPU, batch).
5. **Release channels first** -- Always recommend release channels. Note that "No channel" (static versioning) is deprecated and clusters should be migrated to release channels.
6. **Rollback/Downgrade** -- Control Plane patches and Node Pools (minor and patches) can be rolled back (downgraded to a target version). GKE supports a 2-step Control Plane minor upgrade where step 1 is rollbackable. Other Control Plane minor version rollbacks are NOT customer-doable and require GKE Support.
7. **Node pool upgrade ordering** -- When upgrading multiple node pools, always recommend sequential ordering: upgrade non-critical/stateless pools first (acting as a canary) to verify cluster health before upgrading critical stateful (database) or GPU pools.

## Release Channels

| Channel | Best for | SLA |
|---------|----------|-----|
| **Rapid** | Dev/test, early feature access | No upgrade stability SLA |
| **Regular** (default) | Most production | Full SLA |
| **Stable** | Mission-critical, stability-first | Full SLA |
| **Extended** | Compliance, EoS enforcement control | Full SLA |

### Support Lifecycle
Standard GKE versions are supported for 14 months after they become available in the **Regular** channel. This means:

- **Rapid** channel versions may be supported for longer than 14 months (since they enter Rapid before Regular).
- **Stable** channel versions may be supported for less than 14 months (since they enter Stable after Regular).
- **Extended** support extends this period up to 24 months. Note that extra cost applies only during the extended support period (months 15-24).

### Current Capabilities

- **Extended channel math**: 14 months of standard support + ~10 months of extended support ≈ 24 months total per minor version. Even on Extended, forced upgrades still occur: if you take no action, GKE auto-upgrades the cluster at End of Support — averaging a minor version bump roughly every 4 months, the same cadence as other channels (features just arrive later).
- **Upgrade reliability (KubeCon NA 2025)**: Google reports a 99.99% upgrade success rate across GKE control planes and nodes, with safe rollback and skip-version upgrade support positioned to let teams upgrade less often (e.g., annually instead of quarterly). Pilot skip-version upgrades in non-production clusters first.
- **Autoscaled blue-green node upgrades** (Preview): a blue-green variant that scales the green pool on demand instead of pre-provisioning a full duplicate pool — for disruption-sensitive workloads that cannot reserve 2x capacity.
- **Scheduled cluster upgrade notifications** (Preview): opt in to be notified ahead of scheduled minor upgrades and wire the notifications into alerting.
- **Graceful termination and PDBs during drains**: blue-green (including autoscaled blue-green, Preview) is the *only* strategy that honors `terminationGracePeriodSeconds` for up to 24 hours; surge upgrades honor it for up to 60 minutes. During node drains, GKE respects PDBs for a maximum of 60 minutes, after which pods are force-deleted (a notification is sent).

## Maintenance Windows & Exclusions

Configure maintenance windows to control auto-upgrade timing. GKE also supports node pool level maintenance exclusions (in addition to cluster level) to block upgrades for specific workloads.

**Exclusion types & Limits:**

- **"No upgrades" (Scope: `no_upgrades`)**: Blocks all upgrades (minor, patch, node).
  - **Limits**: Max **90 days per exclusion**, and a cluster can have at most **3** such exclusions. Together they must still allow at least **48 hours of maintenance availability in any rolling 92-day window** — so you cannot chain them into a continuous freeze longer than 90 days. GKE recommends keeping these under 30 days.
- **"No minor or node upgrades" (Scope: `no_minor_or_node_upgrades`)**: Blocks minor and node upgrades, but allows control plane patch upgrades (low risk).
  - **Limit**: No fixed day cap — bounded by the minor version's **End of Support (EoS)**. Recommendation: keep under ~6 months.
- **"No minor upgrades" (Scope: `no_minor_upgrades`)**: Blocks minor upgrades, but allows control plane patches and node upgrades.
  - **Limit**: No fixed day cap — bounded by EoS. Recommendation: keep under ~6 months.

**Important Exclusion Rules (MUST follow when recommending exclusions and MUST include in the final text response):**

1. **Auto-upgrades only**: Maintenance exclusions **only block automatic upgrades**. Manual upgrades initiated by the user will bypass exclusions. You MUST explain this to the user.
2. **Warn against "No channel"**: You MUST explicitly warn that disabling release channels ("No channel" / static versioning) is deprecated and must not be used as a replacement for exclusions.
3. **Compare Scopes**: You MUST explain the difference between 'No upgrades' (limitations, blocks patches) and 'No minor or node upgrades' (allows patches, longer duration). Recommend 'No minor or node upgrades' when the user wants to allow security patches/fixes while blocking minor version jumps.
4. **Handle periods > 90 days**: If the user needs to block upgrades for more than 90 days, you MUST explain that 'No upgrades' is limited to 90 days per exclusion (max 3 per cluster, and 48 hours of maintenance availability must remain in any rolling 92-day window, preventing chaining into longer continuous freezes) and advise using scoped exclusions ('No minor or node upgrades' / 'No minor upgrades'), which have no fixed day cap and can run until the minor version's End of Support.
5. **Version skew**: Be mindful of version skew (between control plane and node pools) when using exclusions. Ensure skew does not exceed the supported 2 minor versions.
6. **Correct exclusion fields**: When proposing exclusions, you MUST write each one as its own entry in `spec.maintenancePolicy.maintenanceExclusion` on the cluster's `ContainerCluster`, with `exclusionName`, `startTime`, `endTime`, and `exclusionOptions.scope` (`NO_UPGRADES`, `NO_MINOR_UPGRADES` or `NO_MINOR_OR_NODE_UPGRADES`, as the GKE API spells them, not gcloud's lowercase forms). For an exclusion that runs until End of Support, set `endTime` to the minor version's End of Support date (from the GKE release schedule) with scope `NO_MINOR_UPGRADES` or `NO_MINOR_OR_NODE_UPGRADES`. The `ContainerCluster` schema carries a fixed `endTime` and no field for gcloud's form that follows the End of Support date if it moves (confirm with `kubectl explain` against the customer's CRD), so a later End of Support date is a new pull request moving `endTime`.

## Mandatory Upgrade Overrides

GKE reserves the right to override user-defined maintenance windows and exclusions for mandatory operations. These overrides cannot be disabled or blocked.

**Common Override Scenarios:**

- **Critical Security Patches**: Urgent vulnerability fixes that must be applied immediately to protect infrastructure.
- **End of Support (EoS) / End of Life (EOL) Enforcement**: If a cluster is running an unsupported version, GKE will force upgrade it to a supported version.
- **Expiring Certificates**: If control plane certificates (CAs) are expiring (within 30 days) and rotation is required to prevent cluster unrecoverability.
- **Maintenance Starvation**: GKE requires at least 48 hours of maintenance availability in any rolling 92-day window. If exclusions block too much, GKE may force an upgrade.

**Guidance (MUST follow when overrides are discussed):**

1. **Correlate with Bulletins**: If GKE performs an unexpected upgrade, you MUST explicitly suggest checking GKE Release Notes or Security Bulletins to correlate the event with emergency patches (do not just suggest checking Cloud Audit Logs).
2. **Design for Resilience**: Workloads must be designed to survive unexpected control plane or node rotation. You MUST recommend:
   - Regional clusters (multi-master) to ensure API availability during control plane upgrades.
   - Multi-zone workload deployments.
   - Replicas > 1 for critical deployments.
   - Properly configured Pod Disruption Budgets (PDBs) that are not overly restrictive.

## Upgrade Planning

When asked to plan an upgrade, produce a structured document covering:

- Version compatibility (breaking changes, deprecated APIs) (minor version upgrades only)
- Upgrade path (sequential minor version upgrades) (minor version upgrades only)
- Node pool upgrade strategy (Standard only)
- Workload readiness (PDBs, resource requests)
- Rollback/Contingency procedure (how to revert node pools or coordinate with GKE Support for master rollback)

**Compatibility Search Rule:**

- If compatibility information (e.g., third-party operator compatibility, GPU driver/CUDA compatibility matrix) is not immediately available in the workspace or via a quick web search, **do NOT loop or make multiple search attempts**. Instead, list the compatibility verification as a **critical pre-upgrade action item** for the user in the checklist.

### Node Pool Strategy (Standard Only)

Recommend **Surge upgrade** as the default and most common strategy, with per-pool settings:

- **Stateless**: Higher `maxSurge` (2-3) for speed, `maxUnavailable=0` for safety.
- **Stateful/DB**: `maxSurge=1, maxUnavailable=0` (conservative).
- **GPU (fixed reservation)**: `maxSurge=0, maxUnavailable=1` (no surge capacity).
- **Large (50+ nodes)**: `maxSurge=20, maxUnavailable=0` (max parallelism).

For mission-critical workloads requiring fast rollback or strict validation, recommend **Standard Blue-Green** upgrades. Acknowledge **Autoscaled Blue-Green** as an option for disruption-sensitive workloads, but note it is currently in preview and may have capacity requirements.

**Upgrade Ordering (User-initiated only):** When planning manual upgrades, specify the sequence of node pool upgrades. Recommend upgrading stateless pools first, verifying cluster stability, and then upgrading stateful/GPU pools. For auto-upgrades, GKE automatically manages sequential node pool upgrades.

For standard command sequences and runbook templates, see [`references/runbook-template.md`](references/runbook-template.md).

### Large-Scale AI/ML Clusters (GPU/TPU)

When advising on GPU/TPU upgrades, you MUST cover all of the following:

- **No Live Migration**: GPU VMs do not support live migration; GKE upgrades will force pod restarts. Explain this to the user.
- **Fixed Reservations & Quota**: H100/A100 typically use fixed reservations with no spare quota. Recommend a **rolling upgrade with zero surge** (`maxSurge=0, maxUnavailable=1`), which releases the reservation of the node being upgraded before provisioning its replacement. Explain that **Blue-Green upgrades are not feasible** here because they require double (2x) the GPU resources (both quota and reservations) during the transition.
- **Driver Coupling**: The GPU driver is tightly coupled to the node OS image, so node upgrades introduce new Linux kernels and NVIDIA drivers that can break CUDA compatibility. Advise upgrading and testing CUDA compatibility in a staging environment/cluster first, and updating workload dependencies (CUDA version in container images) to match the new driver before attempting the upgrade again. To diagnose driver regressions, compare OS image, kernel version (`uname -r`), and driver versions between old (working) and new (non-working) nodes, and deploy a test pod (e.g., vector addition) to verify GPU access. If production is blocked, rolling the node pool back to the previous version is the quickest mitigation.
- **Operational Safety**: Recommend GKE **maintenance exclusions** to block auto-upgrades during active training campaigns. Prior to manual upgrades, cordon GPU nodes and wait for active training jobs to checkpoint/complete.
- **TPU Considerations**: TPU slices are recreated atomically (not rolling); maintenance on one slice restarts all slices in the environment.

## Checklists

Produce checklists as copyable markdown with checkboxes. See [`references/checklists.md`](references/checklists.md) for the full pre-upgrade and post-upgrade checklist templates. Adapt them to the user's environment.

**Stateful Workloads:** When stateful workloads (databases) are present, always include checks for PV backup completion and verification of PV reclaim policies (e.g., Retain vs Delete) in the pre-upgrade checklist.

**Autopilot Checklists:** For Autopilot clusters, ensure the checklists include:

- Verification of `resources.requests` on all containers (Autopilot requirement).
- You MUST include specific `kubectl` commands for API deprecation checks, specifically: `kubectl get --raw /metrics | grep apiserver_request_total | grep deprecated` to check if any active workloads are using deprecated APIs.
- Verifying PDBs to ensure they don't block node drain (even though GKE manages nodes, PDBs are still respected).
- Identifying and deleting "bare pods" (pods not managed by a ReplicaSet/Deployment/StatefulSet) as they won't be rescheduled during node recreation.
- Verification of `terminationGracePeriodSeconds` to ensure pods have enough time to shut down gracefully during node recreation.

## Maintenance runbooks

Produce step-by-step runbooks in which each change is a pull request editing the field in the GitOps repository's format, a node pool's version change is the exact `gcloud container clusters upgrade --node-pool` command as a step for the platform operator, and each check is an actual `gcloud` or `kubectl` read command. See `references/runbook-template.md` for the standard sequences.

**Any runbook that relaxes a safety control must restore it in the same runbook.** This applies above all to PDBs during a node-pool migration or rollback: back the PDBs up before draining, and make restoring them by pull request a numbered step with its own verification, not a closing remark. A runbook that sets `maxUnavailable: 100%` to unblock a drain and never reverts it leaves the cluster without disruption protection, and the gap is invisible until the next voluntary eviction. The same rule covers maintenance exclusions, cordons, and autoscaler `minNodes` overrides added to get through the procedure.

## Maintenance Window Pauses

When diagnosing a \"stuck\" upgrade, consider if it was paused by a maintenance window:

- **Silent Pause Behavior:** If a maintenance window closes before a surge upgrade (auto or manual) completes, GKE intentionally pauses the rollout to prevent disruption outside allowed times. A blue-green node-pool upgrade does not pause: it continues past the window to completion (`references/troubleshooting.md` §11), so for a blue-green pool the window is not the cause and the rest of this section does not apply.
- **Mixed-Version State:** The cluster is left in a stable mixed-version state (some nodes upgraded, some not). You MUST explicitly state that this is a supported and safe intended outcome.
- **Resumption:** The upgrade will automatically resume when the next maintenance window opens.
- **Mitigation for immediate completion:** If the user wants to complete the upgrade immediately, you MUST suggest **temporarily widening the maintenance window** to include the current time (a pull request changing `spec.maintenancePolicy.recurringWindow` on the cluster's `ContainerCluster`). Do not suggest re-triggering the manual upgrade or bypassing the window.

## Troubleshooting

When a user reports a stuck or failing upgrade, you MUST systematically analyze and address ALL 5 potential causes in your final response. Do not omit checks even if you suspect one is the primary cause:

1. **PDB blocking drain:** Identify if any PDB has `ALLOWED DISRUPTIONS = 0` using `kubectl get pdb -A`.
2. **Resource constraints:** Check if pods are stuck in `Pending` due to capacity limits.
3. **Bare pods:** Identify pods without owner references that are blocking the drain (recommend deleting them).
4. **Admission webhooks:** Check if Validating/Mutating webhooks are rejecting pod creation on new nodes.
5. **PVC attachment issues:** Check for volume attachment failures (especially zone constraints).

**Stockout / Quota Exhaustion Rule:**

- If the upgrade is stuck due to `ZONE_RESOURCE_POOL_EXHAUSTED` (stockout) or `QUOTA_EXCEEDED` for Compute Engine resources:
  1. Recommend modifying the upgrade strategy to `maxSurge=0` (rolling in-place) to bypass quota limits.
  2. For `QUOTA_EXCEEDED`, suggest requesting a quota increase from Google Cloud.
  3. You MUST suggest **migrating workloads or creating new node pools in a different zone or region** where capacity/quota is available as a mitigation.

Refer to [`references/troubleshooting.md`](references/troubleshooting.md) for the exact diagnostic commands and fix procedures for each step.

## References

- [GKE Release Notes](https://cloud.google.com/kubernetes-engine/docs/release-notes)
- [Upgrading GKE Clusters](https://cloud.google.com/kubernetes-engine/docs/how-to/upgrading-a-cluster)
- [Troubleshoot GKE upgrades](https://docs.cloud.google.com/kubernetes-engine/docs/troubleshooting/upgrades.md.txt)
- [Get upgrade information (upgrade assist)](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/upgrade-assist.md.txt)
- [Node pool upgrade strategies (surge & blue-green)](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/node-pool-upgrade-strategies.md.txt)
- [Maintenance Windows & Exclusions](https://cloud.google.com/kubernetes-engine/docs/concepts/maintenance-windows-and-exclusions)
- [Rollout Sequencing Concepts](https://docs.cloud.google.com/kubernetes-engine/docs/concepts/rollout-sequencing/about-rollout-sequencing)
- [Configure Rollout Sequencing](https://cloud.google.com/kubernetes-engine/docs/how-to/rollout-sequencing/manage-upgrades-with-rollout-sequencing)

<!-- kube-agents: local addition (auto-injected by sync-upstream-skills.py) -->

## Executed version checks: the fleet-upgrade-verification skill

This skill plans one upgrade at a time; its references read one cluster at a time. When the
question is which clusters in a fleet lag a target version, by how many minors, and whether the
control plane or a node pool is the laggard, run the
[fleet-upgrade-verification](../fleet-upgrade-verification/SKILL.md) skill's script and paste its
table rather than reasoning from memory:

```bash
/opt/data/profiles/platform/skills/fleet-upgrade-verification/scripts/fleet_upgrade_report.py --target-version <version> \
  --output /opt/data/scratch/fleet_versions_$(date +%s)_$$.json
```

Without `--target-version` it measures each cluster against its own release channel's default and
prints that baseline per member. Run again during a rollout, it says which members started,
completed or stalled since the previous run. Without `--readiness` (below) it reads with
`gcloud container` only and changes nothing in GCP; the only thing it then writes is its own
record of each run under `/opt/data/state/fleet-upgrade-verification/`. The plan, runbook and
checklist for the members it flags are this skill's job.

The same script's `--readiness` flag executes three items of this skill's pre-upgrade checklist
per member, against the same target: PodDisruptionBudgets that would block a node drain
(`maxUnavailable: 0`, or `minAvailable` demanding every expected pod), maintenance exclusions and
the maintenance window at a given instant (`--at`, default now), and node-pool version skew
against the target control plane. It also grades a fourth thing the checklist's admission-webhook
item points at: a fail-closed webhook whose backend is unreachable and whose rules reach the
upgrade's path (the writes a node drain and a node join make, as `UPGRADE_PATH_TARGETS` in
`/opt/data/profiles/platform/skills/fleet-upgrade-verification/scripts/upgrade_readiness.py` lists them), which is a cause of
a drain that will not finish, or whose rules reach the bootstrap RBAC writes a new master reconciles
(ClusterRoles and ClusterRoleBindings always; Roles and RoleBindings in `kube-system` or
`kube-public` when its namespace selector admits that namespace), which is a cause of a master that
will not start. It then runs the per-entry rules the fleet-upgrade-verification skill lists for the
upgrade-failure catalogue, each a blocker the report lists under `other blockers` or a risk it lists
under `risks` (a zonal control plane is one). Run it before writing the plan; carry its `blocked`
rows and `other blockers` into the checklist rather than asking the operator to check those items
by hand, and its risks as what to watch. The cluster reads cost one `get-credentials` and the
`kubectl get`s that skill's "Check upgrade readiness" section lists per member, and leave a
per-member kubeconfig under `${HERMES_HOME:-/opt/data}/.kubeconfigs/`; an exclusion is reported as
holding back automatic upgrades only.

When the checklist's deprecated-API item comes up, the same skill's `api_deprecation_scan.py` scans
the linked GitOps repositories' manifests for apiVersions the target removes and reports each with
its replacement and the commit it read; run it with `--target-version` and the version report's
`--output` from a full run, since it refuses a `--cluster` run's file as one cluster's floor. It
reads Git only; live client usage of removed and deprecated APIs is what `--readiness` reads from
the audit log, with GKE Deprecation Insights as the thirty-day cross-check.

## Stuck operation

A cluster whose `status` is not `RUNNING` (`RECONCILING`, `DEGRADED`, `ERROR`) accepts no cluster
or node pool change, and only the provider can end an operation that has wedged inside it. The
fleet-upgrade-verification report's `cluster-status-age` rule files such a cluster as a blocker
once an operation on it or on one of its node pools has been `RUNNING` for more than four hours,
naming that operation; when the project's operations list shows nothing `RUNNING`, it describes
the cluster and re-lists its operations first, and blocks at once only when the status still
stands with nothing running, quoting the cluster's `statusMessage`. What to do with it, in order:

1. **Read the operation, not only the status.** List the newest operations on the cluster and
   on its node pools (a node pool upgrade, resize or repair names the pool and holds the cluster as
   a cluster operation does):
   `gcloud container operations list --project <project> --filter="targetLink~/clusters/<cluster>($|/nodePools/)" --sort-by=~startTime --limit=5 --format=json`,
   then `gcloud container operations describe <operation id> --location <location> --project <project>`
   for the newest one whose `status` is `RUNNING`: its `operationType`, `status`, `startTime`,
   `statusMessage` and `error`. The age is now minus `startTime`; quote both. When none is
   `RUNNING`, what holds the cluster is in its own `statusMessage`
   (`gcloud container clusters describe <cluster> --location <location> --project <project> --format="value(status,statusMessage)"`);
   quote it with the newest finished operation.
2. **Freeze changes on the cluster.** No node pool upgrade, resize, rollback or cluster update
   until the operation ends, and say so in the answer and on any card about the cluster. A pull
   request that changes the cluster's spec waits too: Config Connector would retry against a
   cluster that refuses the change.
3. **Past a few hours, draft the support-case text** (the rule's threshold is four): the project,
   the cluster and its location, the operation id, its type, its start time and age (or, with no
   operation `RUNNING`, the status and its `statusMessage`), the `statusMessage` and `error` text
   verbatim, what was being changed when it started, and that the operation needs the provider to
   abort it. P2 while the workloads run; P1 when the API or the
   nodes are affected.
4. **File a kanban card for a person to open the case the same day**, titled
   `Support case: <cluster> held by <operation id>`, carrying the drafted text, and block it as
   needing input: the agent cannot open a support case, and in one production fleet a cluster
   sat in `RECONCILING` for six days before anyone did.
5. **After the operation ends**, re-run the readiness report: `RUNNING` with the operation listed
   as `DONE` closes the finding; a status that returns is the same card, reopened with the new
   operation id.
