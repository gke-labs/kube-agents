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
