---
name: fleet-upgrade-verification
description: Reports every GKE cluster's control-plane and node-pool versions against a target version or each cluster's release-channel default, naming the members that lag and by how many minors, and answers whether one cluster is current for its channel (--cluster); run again during a rollout, it shows which members started, completed or stalled since the previous run; with --readiness, it also grades each member on what would stop the upgrade, naming drain-blocking PodDisruptionBudgets, fail-closed admission webhooks in the upgrade's path or able to refuse the control plane's bootstrap RBAC writes (ClusterRoles and ClusterRoleBindings; Roles and RoleBindings in kube-system and kube-public) with an unreachable backend, maintenance exclusions and windows, node-pool version skew, and what the per-entry rules of the upgrade-failure catalogue find, each a blocker or a risk that does not block (a zonal control plane is one). Scans the linked GitOps repositories' manifests for apiVersions the target removes, with each hit's replacement. Read-only against GCP, the clusters and Git, from gcloud container, kubectl get and repository reads, plus the audit log through gcloud logging read when a registered rule reads it, keeping only its own record of each run and per-member kubeconfig files; the executed counterpart to gke-upgrades' advice.
---

# Fleet upgrade verification

Answer "against version X, which clusters lag, by how much, and is it the control plane or a node
pool" with a table read from the fleet, not from memory, and "which manifests in our GitOps
repositories declare an API that version X removes" with a scan read from Git. Use it when a user
asks whether an upgrade has reached every cluster, which members are behind a target, how far the
fleet is from a release-channel default, whether one cluster's control plane is current for its
channel (`--cluster <location>/<name>`), or whether the repositories are ready for a target
version. Run the version report again during a rollout and it also says, per member, what changed
since the previous run and which members have stopped moving (see "Track a rollout across runs").
With `--readiness` it also says, per member, what would stop the upgrade: a PodDisruptionBudget
that blocks every node drain, a fail-closed admission webhook with an unreachable backend in the
upgrade's path or able to refuse the control plane's bootstrap RBAC writes (ClusterRoles and
ClusterRoleBindings; Roles and RoleBindings in kube-system and kube-public), a maintenance
exclusion or window, node pools too far below the target, or a blocker one of the per-entry rules
of the upgrade-failure catalogue files, and, as risks that do not block, what those rules name to
watch, a zonal control plane among them (see "Check upgrade readiness"). For upgrade plans,
runbooks and checklists, use the
`gke-upgrades` skill; it links back here when the question is one these two scripts answer.

"Version skew" here is the gap between a member's versions and the target. It is not
configuration drift: the fleet-consistency audit compares a cluster's configuration against its
live peers, and the drift-detection design (not yet shipped) means live state diverging from Git.
Neither reads versions against a target.

## Run the report

```bash
/opt/data/profiles/platform/skills/fleet-upgrade-verification/scripts/fleet_upgrade_report.py \
  <collector_args from the fleet_scope tool, verbatim, or nothing on an install that declares no scope> \
  [--project <project>]... [--cluster <location>/<name>... | --rollout-in-progress] [--target-version <version>] \
  [--readiness [--at <RFC 3339>] [--kubeconfig-dir <dir>]] \
  --output /opt/data/scratch/fleet_versions_$(date +%s)_$$.json
```

The script lives under the profile home, `/opt/data/profiles/platform` (`/opt/data/profiles/platform/skills/fleet-upgrade-verification/scripts/fleet_upgrade_report.py`).

- `--cluster` is repeatable and, when given, restricts the report to those clusters, as
  `<location>/<name>` or a bare name and in no other form: each half is a GKE name, lowercase
  letters, digits and hyphens, so an empty half (`/prod`, `prod/`), whitespace or an uppercase
  letter is a usage error (exit 2), not a bare name and not a miss. Every other member of the projects is
  skipped, reads included, and the rollout record is neither read nor written, since a narrowed
  read would file every member it did not name as gone. A spec that matches no cluster in the
  projects is an error line and exit 1, not an empty table, and when a project's listing failed
  the line says so rather than claiming no match; one cluster named in both forms is one request,
  not a hit and a miss. `--rollout-in-progress` beside `--cluster` is a usage error (exit 2): the
  flag speaks to the rollout record, which a narrowed run does not touch. The JSON
  names the specs under `narrowed_to`, so a reader can tell the narrowed `members[]` from the
  fleet; the deprecation scan refuses such a file. The fleet-audit collector passes the clusters
  it found behind, and reads back only a report marked as narrowed to that cluster.
- `--project` is repeatable and, when given, is the whole scope; on an install with a declared scope each one must be a
  project the `fleet_scope` tool lists and the `collector_args` must be passed beside it, or the script refuses. Otherwise `--scope-projects`, passed once as one
  comma-separated list, is the whole scope: the install's declared scope as the platform_control `fleet_scope`
  tool reports it. Call that tool first and paste its `collector_args`, which spells each flag once
  (`declared: false` means the fallback that follows). `--scope-unread` sets no scope: it names the declared
  projects the install could not read, recorded as a coverage gap, and alone it means a declared scope with
  nothing readable, which the script reports instead of enumerating. Without `--project` or a declared scope the script
  takes the union of `GCP_PROJECT_ID`, `GKE_PROJECT_ID` and `PROJECT_ID` with `MONITORED_PROJECT_IDS`
  (comma- or whitespace-separated) when set, otherwise with every project visible to `gcloud projects list`
  plus gcloud's configured project when it is unset or blank.
- `--target-version` sets one target for every member, in the `MAJOR.MINOR.PATCH-gke.BUILD` form
  the fleet reports (`1.31.4-gke.1183000`); the `-gke.BUILD` suffix is optional and reads as build
  0 without it. Without the flag, each member is measured against its own release channel's
  `defaultVersion` from `gcloud container get-server-config`, fetched once per project and
  location, and the target column says which baseline was used, for example
  `1.31.4-gke.1183000 channel default (REGULAR)`.
- `--output` writes the same data as JSON: `members[]`, `errors[]`, a `summary` count per
  status, `narrowed_to` (the `--cluster` specs of a narrowed run, `null` on a full one) and, on a
  full run only, the `rollout` block and the per-member `progress` fields described below. A
  `--cluster` run writes neither, since it neither reads nor compares the record; it prints a
  note where the progress section would be.
  The file is written whole (a temp file, then a rename) at the end of the run, so a reader never
  meets a partial document and a run that dies leaves the path as it found it. `/opt/data/scratch`
  is shared by every card, so an output name you choose carries a timestamp and the shell's PID, as
  the block above does; read back the path the script prints on its `Wrote … to` line.
  The weekly audit's collector is the exception: it moves its own per-run file onto the fixed name
  described below.
- `--rollout-in-progress` and `--state-dir` belong to rollout tracking, below; `--readiness`,
  `--at` and `--kubeconfig-dir` to the readiness check, below that.

The script runs `gcloud container clusters list`, `gcloud container get-server-config` and, when neither
`--project` nor a declared scope is passed, `gcloud config get-value project` and `gcloud projects list` (plus `gcloud projects describe` for a
project whose `clusters list` was refused as API-disabled or whose configured identifier is a
project number), each with a 60-second timeout, and
with `--readiness` one `gcloud container clusters get-credentials` and the `kubectl get`s per
member that "Check upgrade readiness" lists (the PDB rule's, the webhook rule's, then one per
group of kinds the per-entry rules read), plus whatever a registered rule reads itself through the
script's own runner under the same cap (a rule that reads the audit log runs paged `gcloud logging
read`s).
It changes nothing in GCP or in any cluster; the only things it writes are its own record under
`/opt/data/state/fleet-upgrade-verification/`, the per-member kubeconfig files `--readiness`
needs, and the `--output` file. A failed or timed-out gcloud read, `get-credentials`, PDB read,
webhook read or audit-log read is listed under the table and sets exit code 1; the other projects,
locations and members are still reported. The `kubectl get`s after the webhook read are recorded
per kind in the member's note and JSON and set no exit code (see "Check upgrade readiness"), so
exit 0 says the version table, the PDB rule and the webhook rule were read on every member, not
that every later read was. A project whose own Kubernetes
Engine API is off holds no cluster and is not a failed read; a refusal naming another project,
such as a quota project, is one. The weekly upgrade audit's collector also runs the script, with
`--readiness --cluster <location>/<name>`, once per behind cluster: that run writes
`upgrade-readiness_<project>_<location>_<name>.json` under `/opt/data/scratch/`, and a `--cluster` run
neither reads nor writes the rollout record, so the record above is untouched by it.

### Reading `get-server-config` by hand

Prefer the script, even for one cluster: the command above with `--cluster <location>/<name>`
added (and the `collector_args`, as always) answers "is this control plane current for its
channel?" against the right field. Read the control-plane column for that question: a member's
status also folds in its node pools. If you do read `gcloud container get-server-config`
yourself, read it at the cluster's location (`--location`, `--project`). Each entry of
`channels[]` carries up to three versions, and only one of them is the channel's current version:

- `defaultVersion` is the version the channel offers now, what a new cluster on it gets. A cluster
  is behind its channel when its `currentMasterVersion` is below this, and the lag in minors is
  measured against it.
- `upgradeTargetVersion`, where present, is the version GKE's automatic upgrades currently move
  clusters on the channel to, and it can trail `defaultVersion` by a minor: on 2026-10-16 REGULAR's
  was `1.35.8-gke.1439001` while its default was `1.36.4-gke.1391002`. It is not the channel's
  current version, so a cluster at or above it can still be a minor behind `defaultVersion`.
- `validVersions` is what the channel currently offers. A running version absent from it can still
  be served (GKE keeps patching a version a channel has promoted past), so its absence does not
  mean unsupported.

So "at the automatic-upgrade version" and "current" are different answers. Report the cluster's
version, the channel's `defaultVersion`, and the gap in minors between them.

## Read the table

One row per cluster: project, cluster, location, channel, control-plane version, the lowest
node-pool version with its pool name, target, gap in minors, status, note.

The gap is the target's minor minus the minor of the member's lowest component (control plane or
lowest pool): positive when behind, `0` on the same minor, negative when the lowest component is
ahead. It is empty when the major version differs from the target, and the note says so.

- `lagging`: the control plane or at least one node pool is a minor or more below the target, or
  on a different major.
- `patch-behind`: everything is on the target's minor, but the control plane or a pool has a
  lower patch or gke build. A new patch reaches a channel default before any rollout wave has
  applied it, so a fleet is routinely patch-behind the morning after; report it, and keep it
  apart from `lagging`.
- `current`: control plane and every pool equal the target.
- `ahead`: newer than the target somewhere and nothing below it. Reported, never flagged; channel
  rollout waves are staged, so a member ahead of its channel default is routine. The gap is `0`
  when one component is at the target and the other ahead, negative when both are ahead.
- `unknown`: the control-plane version did not parse, no node pool's version parsed (or the
  cluster record has no node pools), the cluster has no release channel and no
  `--target-version` was given, `get-server-config` failed for the location, or the channel is
  not in that location's server config. The note names which.

A pool whose version does not parse is skipped and named in the note; the row is still graded on
the pools that do parse. A note reading `upgrade in flight` means the cluster or a pool is
`RECONCILING` or `PROVISIONING`; report that row as in progress rather than as a stall.

## Track a rollout across runs

Every full run records its per-member result and compares itself with the previous run for the
same target, so two runs during a rollout show what moved between them. A `--cluster` run does
neither: it prints a note in place of the progress section, and its JSON carries no `rollout`
block and no per-member `progress`, `unchanged_since` or `unchanged_for_seconds`. The record
lives at
`/opt/data/state/fleet-upgrade-verification/<target>.json` (`channel-default.json` for a run
without `--target-version`), on the persistent volume the shell sandbox keeps between turns;
`--state-dir` points it elsewhere. Each target has its own record, so a run against a different
target starts a new baseline rather than comparing across targets.

The first run for a target prints one line saying the baseline was recorded and where. Every later
run prints a "Rollout progress" section after the table, naming the previous run's time and, per
member, the control-plane and lowest-pool versions then and now, the current status, and one of:

- `completed`: the member is `current` or `ahead` after a version change, or its status became
  `current` or `ahead` since the previous run. A `current` member that followed its channel
  default to a new version is `completed`, not `started`.
- `started`: the control plane or the lowest pool changed version without reaching the target,
  or the cluster or a pool is `RECONCILING`/`PROVISIONING`.
- `unchanged`: the same versions as the previous run. A member whose versions are the same but
  whose status changed (the channel default advanced under it) is also `unchanged`, but it starts
  a new observation series: it is not `stalled` in that run, and its elapsed time counts from it.
- `stalled (unchanged for <elapsed>)`: an `unchanged` member that is `lagging` or `patch-behind`
  while a rollout is active, seen at these versions and this status by the previous run too.
  Elapsed counts from the first of the consecutive runs that saw the member so, and keeps growing
  across runs until the member moves.
- `new`: no record of this member in the previous run.

An `unknown` grade (a failed `get-server-config` read, an unparsable version) is not evidence of a
move: the comparison falls back to the versions alone, the record keeps the last graded status
and its clock, and a member that is `current` at the same versions as an ungraded baseline is
`unchanged`, not `completed`.

A rollout is active when at least one member is `started` or `completed` in the same comparison,
or when the operator passed `--rollout-in-progress`, which is for a rollout whose first wave has
not produced a mover yet. Pass the flag when the user says a rollout is under way; when there is
no earlier record to compare with and the user asks which members have stalled, run the report
twice in the turn with the flag and read the second run's section. Without a mover or the flag,
an unmoved member is `unchanged`, never `stalled`, so two quiet runs a week apart do not invent a
stall. A member that is `current`, `ahead` or
`unknown` is never `stalled`.

A member in the previous record with no row this run is listed once under the section: dropped
from the record when its project was read cleanly (the cluster is gone), carried forward when
its project failed to read or was not in this run's `--project` scope, so a failed read never
loses a record or manufactures a stall. A record the script cannot read is reported on stderr and
replaced by a new baseline; a record it cannot write sets exit code 1 with the table still
printed. In a full run's JSON, each member carries `progress`, `unchanged_since` and
`unchanged_for_seconds`, and the top-level `rollout` block has the record path, both run
timestamps, whether the rollout counted as active and why, a count per progress value, and the
members missing this run.

## Check upgrade readiness

`--readiness` adds a second table after the version table, one row per member, graded against
the same target as the member's version row, and a `readiness` object per member in the JSON
(`members[].readiness`, with a top-level `readiness` block holding the instant evaluated and a
count per verdict). Without the flag nothing changes. The rules below each name the governance
SOP check they derive from, except the webhook rule, which has no SOP check yet; after them run
the per-entry rules registered in `upgrade_readiness.EXTRA_RULES` (`scripts/readiness_rules/`, one
module per entry of the upgrade-failure catalogue in the repository's design docs; the package
docstring is the contract a module meets); the maintenance rule departs from its SOP where the two
differ, and says so. Run it once, from this profile, for every cluster the question covers; a
readiness question fanned out to the Cluster Agents loses the maintenance read and the grading
below, and a hand survey in their place reports every fail-closed webhook without a visible
backend, GKE's own URL-backed ones included, as a blocker.

- **Drain-blocking PDBs** (`obtainability_audit_sop.md` §3.4). For each member the script runs
  `gcloud container clusters get-credentials` into a kubeconfig of its own under
  `${HERMES_HOME:-/opt/data}/.kubeconfigs/` (`kubeconfig_<project>_<cluster>_<location>.yaml`, one file per
  target so concurrent reads never share a current-context; `--kubeconfig-dir` moves the
  directory), adding `--dns-endpoint` when the cluster record says its DNS endpoint accepts
  external traffic, then one `kubectl get pdb,deploy,statefulset -A -o json`, the first of the
  member's reads (the per-entry rules' reads follow it, below). A PDB is matched to
  the Deployments and StatefulSets in its namespace whose pod-template labels satisfy its
  selector (`matchLabels` and `matchExpressions`), and it blocks every drain when
  `maxUnavailable` is `0` or `0%`, or when `minAvailable` demands every expected pod: an integer
  at or above the pods the controller expects, or a percentage that rounds up to them the way
  the disruption controller rounds (`100%` always; `90%` on nine replicas too). The expected
  count is the matched workloads' replica total, or the PDB's `status.expectedPods` when that is
  larger, as it is when the selector also covers pods of a kind the read does not include. The
  cell names the PDB as `namespace/name`, the offending field and the workloads with their
  replica counts; each JSON finding carries `expected_pods` and `disruptions_allowed`, from the
  PDB's `status.expectedPods` and `status.disruptionsAllowed`, as corroboration. Skipped and
  counted in the note rather than graded: a PDB whose matched workloads are scaled to zero, an
  orphan matching no workload and covering no pod, and a PDB that covers pods of a kind the read
  does not include (a bare ReplicaSet, a custom controller), which is noted so it is never
  silently `ready`. DaemonSets are never matched: a drain deletes their pods rather than evicting
  them.
- **Fail-closed webhooks**, from a second read with the same kubeconfig: `kubectl get
validatingwebhookconfigurations,mutatingwebhookconfigurations,services,endpointslices,namespaces -A -o json`.
  It is a read of its own so that a failure there grades only this rule `unknown` and never costs
  the PDB result. In the agent sandbox the credential proxy cuts a list past its output cap on the
  largest clusters; the error row names the cap, and the rule is `unknown` there. A webhook is graded when its `failurePolicy` is `Fail` (or absent, which
  `admissionregistration.k8s.io/v1` defaults to `Fail`) and its backend is a Service the API
  server cannot reach: the Service does not exist, no Service port equals the webhook's port (443
  when unset), or no ready endpoint sits behind that port in the Service's EndpointSlices (an
  endpoint without a `ready` condition counts as ready, as the API requires). Such a webhook
  rejects every request its rules match, and what it matches decides the grade. When a rule can
  match a write the node drain or the node join makes through the API server — `UPGRADE_PATH_TARGETS` in `scripts/upgrade_readiness.py` is the list, grouped by phase with the kubelet or controller package behind each write — the
  member is `blocked`: the workloads it gates lose their pods on the drain and cannot get them
  back, a budget over one of them also stalls the drain, and a gate on evictions or nodes stops
  the drain itself. When no rule matches any of those, the webhook is still a current outage for
  what it does match and the cell names it, but it does not grade the member; that list is what
  the rule knows of the upgrade's path, not a proof the upgrade is unaffected, which is why the
  cell names what the webhook does match. Rules are matched on
  API group, API version, operation, resource (with the API's `*`, `*/*` and `pods/*` semantics)
  and scope, as the API server matches them: a rule's `apiVersions` must carry `*` or the version
  the server serves the write at (`v1` for every write on the list), so a rule pinned to a version
  the server no longer serves (`policy/v1beta1`, `certificates.k8s.io/v1beta1`) matches nothing. Each
  rule of a dead off-path webhook is then classed by what the server serves it, on the resources it
  names and not on its operations: a rule at `*` or `v1` fails now every request the server sends it (whether the server serves an off-list resource at all is not the reader's to say, and the cell claims no more); a rule naming graded
  resources only, at an unserved version, is sent nothing (whatever the operation; the cell then names
  the resource, and a group-resource pair the rule spells that the server serves at no version, such as
  `policy/pods`, is said so rather than served at `v1`); a rule at an unserved version on a resource, group or wildcard off the lists is one
  whose served versions the reader does not know. The cell says each for the rules the webhook has: a
  webhook whose every rule is pinned is told the server sends it no request at a served version rather
  than that its requests fail now; otherwise the live rules are named as failing now every request the server sends them,
  the pinned rules as sent nothing, and the unknown rules as failing only if the server still serves
  their version, so a failure now is asserted for the live rules alone; `objectSelector` and
  `matchConditions` are not evaluated, and `namespaceSelector`
  is read only for the `kube-system` reach, so a webhook they narrow is otherwise reported as
  able to match. A dead webhook off the node path is graded `blocked` all the same when its rules match a
  bootstrap RBAC write a new master's start-up reconciles and fatals without, so the
  control-plane upgrade cannot complete: a ClusterRole or ClusterRoleBinding write, whatever the
  `namespaceSelector` says, because admission matches a cluster-scoped object before it reads the
  selector (`CONTROL_PLANE_CLUSTER_WRITES` in the script); or a Role or RoleBinding write when the
  `namespaceSelector` admits `kube-system` or `kube-public` (absent or empty admits every
  namespace; a requirement the reader cannot evaluate counts as admitting; the two namespaces
  are judged on the labels the read returns for them, so an exemption label an operator put on
  them reads as admission reads it; `CONTROL_PLANE_KUBE_SYSTEM_WRITES`). Its cell names the write and, for the namespaced rows, the
  namespaces admitted. A ConfigMap gate is
  not on that list: the start-up ConfigMap write the Jetstack outage deadlocked on left the
  start-up path in Kubernetes 1.17, and its successor retries. The cell names the
  configuration, the webhook, the reason and what it matches (an outage cell lists the webhook's
  own rules, with their `apiVersions` when a rule pins any); each JSON finding carries
  `reason`, `upgrade_path` and `rules`, split into `blocking` and `outage`. A fail-closed webhook with a URL backend is counted in the
  JSON (`url_backends`) and never graded, because nothing read here says whether the URL answers;
  GKE installs two on every cluster. Fail-open webhooks are counted in the JSON (`fail_open`), and so is a fail-closed webhook with no `rules` (`no_rules`): it matches no request, so the server never calls it and a dead backend behind it fails nothing now.
- **Maintenance** (`security_patch_orchestrator_sop.md` §3.7 and §3.8), evaluated at `--at`, an
  RFC 3339 instant, by default now. An exclusion in effect blocks when its scope covers the upgrade
  the target needs: `NO_UPGRADES` (the default when the record carries no scope) always;
  `NO_MINOR_UPGRADES` when the target is a minor above the control plane or a pool;
  `NO_MINOR_OR_NODE_UPGRADES` when it is a minor above the control plane or any pool is below the
  target. This grades the scope against the target, which the SOP's `blocking-exclusion` check
  does not: that check never flags `NO_MINOR_UPGRADES` and adds a 30-day threshold, so the two
  disagree on a cluster whose `NO_MINOR_UPGRADES` exclusion holds exactly the minor upgrade its
  target needs, and this table is the one that reads it as a blocker. The cell says
  `blocks auto-upgrade`, because that is what an exclusion holds back: an
  operator running `gcloud container clusters upgrade` by hand is not subject to it. An exclusion
  in effect whose scope does not cover the upgrade (a patch-only target under
  `NO_MINOR_UPGRADES`) is reported and not a blocker. The maintenance window is reported as
  `open` (with when it closes) or `closed` (with the next opening) at the instant, for a
  `dailyMaintenanceWindow` or a `recurringWindow` whose recurrence is `FREQ=DAILY` or
  `FREQ=WEEKLY` with a plain `BYDAY` list; any other recurrence is `not evaluated`, never a
  verdict. The window is informational: a closed window delays automatic maintenance, it does not
  block a manual upgrade. No window is reported as such.
- **Node-pool skew** (§3.2). Per pool, the target control plane's minor minus the pool's minor:
  more than 2, or a different major, blocks the control-plane upgrade until the pool moves
  (GKE keeps nodes within two minors of the control plane); exactly 2 is at the ceiling and
  goes in the note. Autopilot members read `n/a`.

The per-entry rules run after those four, over the same cluster record and the reads after
the webhook read, one per group of kinds: `kubectl get daemonset,cronjob,pvc,networkpolicy,namespace
-A -o json`; `kubectl get pods -A -o json` on its own, because the pod list is the one most
likely to exceed the credential proxy's output cap and a cut read must cost only the rules that
need pods; the Nodes through a jsonpath template that keeps each node's name, labels, taints,
whether it is cordoned, node info, allocatable and conditions and drops the rest (so a few
hundred nodes stay under that cap); and `kubectl get storageclass,pv -o json`. Each of them
fails on its own: a refused or unparsable read, or one the credential proxy cut at its output
cap (recorded as that kind's error naming the cap, never as unparsable JSON), costs only the
rules that need its kinds, each of which says so as `unknown` and grades what it has, and it
sets no exit code; the note names the kinds not read and the JSON records the error per kind
under `read_errors` (every kind carries the reason when `get-credentials` or the PDB read failed:
no read ran, or the reads after the webhook read were skipped, whether or not the webhook read
itself ran). A rule that needs more reads makes
them through the script's own runner, under the same per-call cap. The audit-log reader beside the
rules (`readiness_rules/audit_log.py`) is that kind of read: two paged `gcloud logging read`s per
member over the seven days ending at `--at`, cached so every rule that asks reads once, and
writes only, because the Admin Activity log carries no reads, so a caller that only reads an API
is not in it. Each audit-log read a member's rules performed is listed in the JSON under
`audit_log` (empty for a member whose rules performed none) with its commands, entry and page
counts, whether it was sampled, its error and the window bounds, and a failed one is listed under
the table as an audit-log read that failed, apart from the kubectl read failures, so the member stays
graded by the rules that did not need it while the rule that did says `unknown`; it sets exit code 1.
Each rule returns blockers,
risks, reasons it could not decide, and notes, and declares whether it can block at all; a
blocker joins the verdict, a risk never does, and the table prints them under `other blockers`
and `risks`, each as `<rule id>: <the rule's description>`, so the cell names the rule the way the
note column does. The rules registered today, with the catalogue entry each reads:

- **Surge capacity** (`surge-capacity`, entry 2). A pool whose upgrade settings take a node away
  before its replacement exists (`maxUnavailable` above 0 on the surge strategy, or a blue-green
  upgrade with the autoscaled rollout policy), measured by placing the pods of each of its nodes
  in turn, largest first, onto the schedulable nodes left that each pod may schedule on (its node
  selector, its required node affinity and the taints it tolerates decide which; a tainted pool's
  spare capacity never counts for a pod without the toleration), each onto the eligible node with
  the most room left by CPU and memory requests (native sidecars and the pod's `overhead` counted
  as the scheduler counts them), DaemonSet and mirror pods excepted; a pod no eligible node can
  take is stranded. It blocks when `maxSurge` is 0, `maxUnavailable` above 0, the autoscaler
  cannot grow the pool (at its ceiling, or not autoscaled) and a pod is stranded; a stranded pod
  with some surge left, a growable autoscaler or an autoscaled blue-green rollout is a risk; a pool
  whose every node's pods are placed is a note. The node measured is the one whose stranded pods
  are the largest in either resource, so a memory-bound node counts as a CPU-bound one does, and
  the cell names the pool, its settings, that node, the stranded requests, the room on the nodes
  the pods may schedule on, the largest stranded workloads and any other node of the pool that
  strands pods. Autopilot members are not graded. A pool at or above the target is not graded; no
  target, an unparsable pool version or quantity, or Nodes or Pods not read is unknown, and the
  rule can block, so that unknown moves the verdict. GKE's defaults (`maxSurge` 1,
  `maxUnavailable` 0) never trigger the placement half. The same pools are read for the surge
  node that must exist before a drain: every pool below the target on the surge strategy with
  `maxSurge` above 0 (the default is 1; a blue-green pool creates its green pool instead and is
  not counted). A pool carrying accelerators (`config.accelerators`) that surges with no
  reservation affinity (`config.reservationAffinity` consuming neither `ANY_RESERVATION` nor
  `SPECIFIC_RESERVATION`) blocks, since a surge node needs accelerator capacity the zone may
  not have and a pool upgrade that cannot create one stalls between versions; one under a
  reservation is a note. Per Pod range (the pool's `networkConfig.podIpv4CidrBlock`, else the
  cluster's `clusterIpv4Cidr`), the free aligned blocks of the pools' per-node size (each
  node's `spec.podCIDR`, which the projected node read carries, else the block GKE sizes for
  the pool's pod limit; the largest block among the pools is the unit) are counted against the
  sum of the pools' `maxSurge`, and fewer free blocks than surge nodes blocks: a surge node with
  no block never registers. The cell names the range with its free, held and total blocks, the
  surge need and the pools, or the pool with its accelerators and `maxSurge`, and the action:
  lower `max-surge` or add a secondary Pod range; set the in-place strategy (`max-surge` 0,
  `max-unavailable` 1) or hold a reservation. No target is one unknown for the surge pools;
  Nodes not read, or a record with no Pod range, is unknown for the range half while the
  accelerator half still grades. Quota is not read here: the stockout-prevention audit reads it.
- **Data on the node** (`data-on-the-node`, entry 4). Deployments and StatefulSets are read for
  state, not scratch: a StatefulSet's `emptyDir` (its identity survives the rebuild, its
  node-local data does not), a `hostPath` a container mounts writable, a `hostPath` under a
  Local SSD mount, or a claim (or a StatefulSet's claim template) on a StorageClass whose
  provisioner is `kubernetes.io/no-provisioner` (how Local SSD becomes a PersistentVolume) is a
  `risk`, named with the volume; a volume name that suggests state is marked. A Deployment's
  `emptyDir`, a memory-backed `emptyDir` and a `hostPath` mounted read-only are notes.
- **Removed-API callers** (`removed-api-callers`, entry 6; can block). From the removed-release
  audit read: each caller is one principal, user agent and API (`core/v1 endpoints`). The
  operator's callers are user accounts, service accounts, and Kubernetes service accounts outside
  the system namespaces (`kube-system`, `kube-public`, `kube-node-lease`, `gatekeeper-system`,
  `cnrm-system`, `asm-system`, and the `gke-`, `gmp-` and `config-management-` namespaces); every
  other `system:` principal, the node identities and Google-managed service agents are the
  provider's. An operator caller of an API removed at or before the target minor whose last write
  is within 48 hours blocks, named with the release and, where `removed_apis.json` knows one, the
  replacement; one silent for longer is a risk naming its last write, since a migrated caller must
  not block for a week. A removal after the target, a provider caller (GKE moves its own
  components) and a principal the rule cannot place are risks, never blockers. A read that failed
  or timed out is `unknown`, graded on the pages before it; a sampled read is a note.
- **Changed defaults** (`changed-defaults`, entry 8). The minors the upgrade crosses, from the
  control plane for an admission default and from the lowest node pool for a kubelet default, are
  looked up in `upgrade_shape_tables.DEFAULT_CHANGES_BY_MINOR`, each row with its source (the Pod
  Security Standards page, the Kubernetes 1.33 changelog, the 1.36 release notes). A namespace
  whose `pod-security.kubernetes.io/enforce-version` is `latest` or absent follows the running
  minor; where a crossed minor tightens the level it enforces (1.23 restricted: no `runAsUser: 0`;
  1.34 baseline: no host field on probes), it is a risk naming the namespace and the setting; a
  minor that only widens an allowlist is a note; a namespace pinned to a named version is the safe
  case and is counted. A workload with a `gitRepo` volume is a risk across 1.33 (disabled by
  default) and 1.36 (permanently). A target past the table's `as_of` (1.36) is graded on the
  minors the table covers, with a note that the table ends there; a namespaced read that failed
  leaves the labels ungraded, in the note, since the rule never blocks.
- **Deprecated-API callers** (`deprecated-api-callers`, entry 9). From the deprecated-or-kubectl
  audit read: each operator caller of an API stamped `k8s.io/deprecated` with no removal release
  is a risk named with its principal, user agent, API and, for Endpoints, the successor
  (EndpointSlice); a principal the rule cannot place is a risk naming it. The provider's callers
  the filter did not express are counted in the note, not filed; kube-system's
  endpoint-controller is stamped on every Service's Endpoints it writes, so the label alone
  identifies nothing.
- **Client and add-on skew** (`client-addon-skew`, entry 10). kubectl is supported within one
  minor of kube-apiserver (`upgrade_shape_tables.KUBECTL_SKEW_SOURCE`, the Kubernetes version skew
  policy): a `kubectl/vX.Y` user agent in the deprecated-or-kubectl read, or a workload image
  whose repository ends in `kubectl`, further than one minor from the target is a risk naming the
  version and the principal or workload. A client-go user agent carries the binary's version, not
  the library's, so those callers are counted, not graded; the log carries writes, so a kubectl
  that only reads is not in it. `addonsConfig` names the GKE-managed add-ons that are on; they
  expose no version and GKE owns them, so they are listed in the note and not graded. A
  third-party add-on recognised by image repository is graded against
  `upgrade_shape_tables.ADDON_SUPPORT` (the vendor's page and the day it was read): a target
  outside the release's supported range is a risk; an add-on with no table, or at a release the
  table does not list, is a note ("version not graded"). Nothing is inferred.
- **Zonal control plane** (`zonal-control-plane`, entry 11). A cluster whose `location` is a zone
  is a risk on every report, never a blocker: the Kubernetes API is unreachable for a short window
  while the single control-plane replica is rebuilt, running pods are unaffected, and the cell
  says what to do about it. A location that is neither a zone nor a region is unknown, and since
  the rule can never block, that unknown goes in the note and leaves the verdict to the other rules.
- **Removed node label** (`removed-node-label`, entry 12). Every `nodeSelector` and required
  node affinity term that selects nodes carrying a label (`In`, `Exists`) is read against the
  Node list; `NotIn`, `DoesNotExist`, `Gt`, `Lt` and preferred terms never strand a pod and are
  not graded. A label or label value in the drop table (`node-role.kubernetes.io/master` from
  1.24, `cloud.google.com/gke-container-runtime=docker` from 1.24) is `blocking` only when a
  pool the template can land on crosses the drop in this upgrade (its current minor below the
  drop, the target at or past it); a drop every such pool passed already is a note, since the
  pods either run or are already unschedulable and the upgrade changes neither. A label no
  node in the template's pools carries today is a `risk`, and a deprecated beta label the
  kubelet still sets (`beta.kubernetes.io/arch`, `failure-domain.beta.kubernetes.io/zone` and
  their siblings) is a `risk`, named with its GA replacement. The carried question is not
  asked on Autopilot, nor for a pool that is empty today and scales from zero (a note), and it
  is `unknown` when the Node read failed.
- **Container runtime** (`container-runtime`, entry 13). For each pool below the target, the
  containerd major its nodes report (`status.nodeInfo.containerRuntimeVersion`, or the table by the
  pool's minor when the nodes were not read) against the major the target's node image ships
  (`upgrade_shape_tables.CONTAINERD_MAJOR_BY_GKE_MINOR`: Linux images at 1.33, Windows Server at
  1.35, GKE's containerd 2 page as the source), paired with every pod or template that mounts a
  runtime socket (`/run/containerd/containerd.sock`, `/var/run/docker.sock`, either spelling) through
  a `hostPath` volume. A pool whose major changes under socket clients is a risk naming the pool,
  both majors and the clients' owners; a change with no client, and clients with no change, are
  notes; a change on a member whose Pods or DaemonSets were not read is unknown, and since the
  rule can never block, that unknown goes in the note and leaves the verdict to the other rules.
- **cgroup v2 under an old runtime** (`cgroup-v2-runtime`, entry 14). A Java image tag below
  JDK 8u372, 11.0.16 or 15, or a .NET image tag below 5.0, on a pool the target moves to
  cgroup v2 (`effectiveCgroupMode` v1 and a target at or past GKE's 1.33 migration, or a pool
  pinned to v1 and a target at or past the 1.35 removal) is a `risk`; a known runtime whose tag
  names no version on such a pool, or a pool whose mode the record does not carry, is
  `unknown`; a pool already on cgroup v2, or staying on v1 at the target, is a note.
- **Group OOM kill** (`group-oom-kill`, entry 15). A heuristic, marked as one in every finding,
  because the process count is not readable from the API: a container whose command or args
  run `supervisord`, `s6-svscan` or `runsvdir`, whose shell `-c` script forks a background job,
  or whose image is a documented multi-process base, on a pool where the target turns the group
  kill on (a move to cgroup v2 at 1.28 or later, or a cgroup v2 pool's kubelet crossing 1.28)
  is a `risk`, never `blocking`. A pool already group-killing, or one whose node config sets
  `singleProcessOOMKill`, is a note. A `-c` script the rule cannot read (an unterminated
  quote, heredoc or expansion) is `unknown` on a pool where a fork would have been a risk,
  never clean.
- **Network dataplane** (`network-dataplane`, entry 16). A cluster on the legacy dataplane
  (`networkConfig.datapathProvider` absent or `LEGACY_DATAPATH`) whose network policy add-on is off
  and whose namespaces carry NetworkPolicies is a risk naming the namespaces and their counts:
  nothing enforces those policies today, and all of them take effect the moment enforcement does,
  at the node rebuild after the add-on is enabled or on the Dataplane V2 cluster that replaces
  this one, which is the shape GKE's recommender files as `NETWORK_POLICIES_UNRECONCILED`; the
  cell names the Dataplane V2 members among the clusters the run listed (the projects' whole
  listing, even when `--cluster` narrows who is graded), where the same policies would be
  enforced. Policies the add-on enforces are a note, and so is a legacy member with no policy at
  all, so every legacy member states its dataplane and enforcement; a Dataplane V2 member is
  clean; a legacy member whose NetworkPolicies were not read is unknown, which never moves the
  verdict since the rule cannot block.
- **Node-image-coupled agent** (`node-image-coupled-agent`, entry 17). A DaemonSet with
  `hostNetwork: true`, outside the system namespaces, that selects on a
  node-image label (`cloud.google.com/gke-os-distribution`,
  `cloud.google.com/gke-container-runtime`), mounts the kernel module tree or the network
  plugin's directories (`/etc/cni/net.d`, `/opt/cni/bin`, `/home/kubernetes/bin`), or runs
  `modprobe` or `insmod`, is a `risk`; the detail names the pools below the target, whose image
  this upgrade changes.
- **GPU driver mismatch** (`gpu-driver`, entry 18). For a template requesting
  `nvidia.com/gpu`, the CUDA version is read from the image tag or from the `CUDA_VERSION` or
  `NVIDIA_REQUIRE_CUDA` env value (no other env: a compute-capability list is not a toolkit
  version), and the driver from the pools carrying the accelerator the template selects: the
  pool's `imageType` and `gpuDriverVersion` (`DEFAULT` or `LATEST`) pick a branch from GKE's
  per-minor table at the target's minor. On Autopilot GKE provisions the node itself, on
  Container-Optimized OS with the driver the template's `cloud.google.com/gke-gpu-driver-version`
  selector names (`default` when absent), so the same table answers with no pool. A driver
  below the CUDA major's floor is `blocking`; one below the toolkit's own minimum is a `risk`
  (minor version compatibility); an image naming no CUDA version, or a Standard template whose
  accelerator no pool carries, is a `risk`; a target the driver table does not cover (it ends
  at 1.33 today, as GKE's GPU page does), an operator-installed driver, or a record with no
  install mode is `unknown`. A pin the driver serves is a note, with the reminder that
  forward-compatibility packages inside the image are not readable from the tag.
- **In-tree volumes** (`in-tree-volumes`, entry 19). Every PersistentVolume with an in-tree
  `gcePersistentDisk` source, its claim, and the pods and templates that mount the claim, against
  `addonsConfig.gcePersistentDiskCsiDriverConfig.enabled`. With the add-on off, a volume a workload
  mounts blocks (the rescheduled pod cannot attach it), naming the volume and the workload, and an
  unmounted one is a risk; with the add-on on, every in-tree volume is a risk, since it still
  depends on the migration shim rather than on the driver. PersistentVolumes not read is unknown
  and, since the rule can block, moves the verdict; Pods not read is unknown too, with the
  templates still graded.
- **Retired registry** (`retired-registry`, entry 20). A container or init container pulling
  from `k8s.gcr.io`, `gcr.io/google-containers`, `gcr.io/google_containers` or
  `gcr.io/kubernetes-helm` is a `risk`, named with the image, the host and what the host does
  today: a rebuilt node has no image cache and pulls from a host that stopped publishing,
  which still answers (`k8s.gcr.io` by a redirect to registry.k8s.io) until it does not. The
  detail names the pools below the target, whose nodes this upgrade rebuilds, or says no pool
  is below it yet.

A member is `blocked` when any rule blocks, whatever else could not be evaluated; `unknown` when
nothing blocked but a rule that could have blocked could not be evaluated (the cluster read
failed, there is no target, an exclusion's scope or a pool's version was unreadable, a per-entry
rule said so); `ready` only when every rule was evaluated and none blocks. A risk never moves the
verdict, and neither does an unknown from a rule that declares it can never block, which is
reported in the note. In the JSON each member carries `readiness.rules` (per rule id: its entry,
whether it can block, and a count of its blocking, risk, unknown and note entries),
`rule_blocking`, `risks` and `unknown` (the findings across rules, once, each with its `rule`,
`entry` and the `text` the cell or the note prints; an unknown entry with its `reason` and
`can_block`), `notes`, `read_errors` (per kind) and `audit_log` (per read); the top-level
`readiness` block counts the members with risks (`members_with_risks`). A failed
`get-credentials`, PDB read or webhook read is listed under the table as a read failure for that
member and sets exit code 1, like a failed gcloud read. A failed `get-credentials` or PDB read
leaves the PDB rule and every per-entry rule that needs an object ungraded (the webhook read is
skipped when the API server did not answer the PDB read, and the reads after the webhook read are
skipped on any PDB failure), and a failed webhook read leaves the webhook rule alone ungraded,
while the member's maintenance and skew rules, and a per-entry rule that reads the cluster
record alone, are still graded, and the other members are unaffected. `--at` with a
value that is not RFC 3339, or `--at` or `--kubeconfig-dir` without `--readiness`, is a usage
error (exit 2).

## Scan the GitOps manifests

```bash
/opt/data/profiles/platform/skills/fleet-upgrade-verification/scripts/api_deprecation_scan.py \
  --versions <the path the version report printed on its Wrote line> --target-version <version> \
  [--repo <owner/name | host/path>]... [--manifests-dir <path>] --output /opt/data/scratch/api_deprecations_$(date +%s)_$$.json
```

A scan for a second target names the target as well as the run
(`api_deprecations_<target>_$(date +%s)_$$.json`): two scans in one shell share `$$` and can
share the second, and a name without the run's suffix is a file another card can write or has
already written.

- `--versions` is the version report's `--output`. The scan's floor is its lowest control-plane
  minor: the API server is what stops serving a removed version, so node-pool versions do not
  enter into it. A file from a `--cluster` run (`narrowed_to` set) is refused: its floor is one
  cluster's, not the fleet's; rerun the report without `--cluster`, or replace `--versions` with
  `--current-version <version>` (the two flags together are refused before the file is read).
  `--current-version <version>` replaces the file when there is none.
  `--target-version` defaults to the file's `target_version` when the report was run with one.
- Without `--repo` the script scans every repository under `managed_repos`, on every forge, the
  same list the GitOps skills write to; `--repo` is repeatable and, when given, is the whole scope.
  A repository on another forge than GitHub is named `host/path`, as the list names it, and is read
  through the content workspace only: on a broker not armed for content-passing it is listed under
  errors, because the leased checkout reaches GitHub alone.
  `--manifests-dir` scans a local tree instead of, or as well as, repositories.
- It reads each repository the way `inspect-repository` does: through the credential broker's
  content workspaces as a shallow read-only clone, or, on an install whose broker is not armed
  for content-passing, through a leased checkout on the shared volume. That checkout is under
  a lease of the scan's own (`--lease` overrides it), so positioning it on the base branch
  never resets a checkout another skill is working in -- `fleet-audit`'s remediation clone is
  leased on its audit id on the same volume. It runs no `gcloud` and writes to no repository. A repository the broker or git cannot
  serve is listed under errors and sets exit code 1; the other repositories are still reported.
- Removal data is `removed_apis.json` beside the script: Kubernetes 1.16 through 1.32, from the
  upstream Deprecated API Migration Guide, whose URL and `as_of` version the report prints. The
  script cannot call MCP tools, so when the target is newer than the table (the report says so)
  confirm removals after it through `mcp-developer_knowledge` and cite what you read; confirm the
  replacement column the same way before you recommend a migration.
- It reads `*.yaml`, `*.yml` and `*.json`, every document in a file and the items of a
  `kind: List`. A file that does not parse (a Helm template, a Kustomize patch the loader
  refuses) is listed as skipped with the reason and is not scanned; rendering charts and
  overlays is not this script's job.

## Read the deprecation report

One section per repository, headed `repo manifests as of <sha>` (or `local directory <path>`),
so a reader knows which commit was read. A section with hits has one row per manifest: repo
path, kind, name, the apiVersion used, the minor that removes it, the replacement, and the
members affected, which are the members whose control plane is still below that minor. A hit
is an apiVersion removed after the floor and no later than the target; a removal at or below
the floor has already happened on every member and is not reported. A clean section says so
with the file and document counts it read. Under either, `skipped` lines name the files it did
not read and why, and a `partial` line means a size cap stopped the scan; a clean section with
skipped files is clean for what it read, so say that. A section headed `not scanned` means this
run did not read that repository: report it as not read, with the error the section gives, and
never answer for it from a scan file this run did not write or from another card's result.

The scan reads what Git declares. Whether a client is still calling a deprecated API on a live
cluster is a different question, answered by GKE Deprecation Insights from the cluster's audit
logs: the console page the report's footer links, or
`gcloud recommender insights list --insight-type=google.container.DiagnosisInsight`, which the
report quotes for a human to run. That command is not in the agent's gcloud read allowlist, so
give it to the user rather than running it, and do not present the Git scan as proof that no
client uses the API.

## Report

Paste the table into the reply, then name the lagging members with both versions and the gap,
and the patch-behind members separately. When the run printed a Rollout progress section, paste
it too and name each `stalled` member with its elapsed time and the previous run's time, as an
observation rather than a fault: a member whose wave has not been scheduled yet reads the same
as one that is stuck, and the elapsed time is what lets the user tell them apart. A first-run
baseline line means there is nothing to compare yet; say when to run again. When the run printed
a readiness table, paste it too and name each `blocked` member with what blocks it as the table
states it: the PDB by `namespace/name` with its field and workload, the exclusion by name with
its scope and end time and that it holds back automatic upgrades only, the webhook as
`configuration/webhook` with its configuration kind, why its backend is unreachable and what it
matches, the pool with its skew, and each `other blockers` entry as the cell states it. A webhook
the cell lists as matching none of the upgrade's path is an outage to report, not a blocker, with
what it does match named so the operator can judge it. Name each member's risks from the `risks`
column as risks, not blockers (the zonal control plane with the retry advice), and say which rules
read as `unknown` and why, from the note; never read an `unknown` as clean. When the user asks for
a verdict per object — does this budget, exclusion or webhook block the upgrade — take it from the
table: an entry under `blocking` (or a blocking exclusion or pool) is yes, a webhook under `outage`
is no, with the outage named; do not re-grade by hand. An exclusion in effect whose scope covers
the target is `yes` whatever the aside says about automatic versus manual upgrades: the table
graded the member `blocked` on it, and that is the verdict; the automatic-only qualification
belongs in the prose beside the line, never in the yes/no. Name a webhook by its configuration,
the part of the table's `configuration/webhook` cell before the slash; the webhook's own name may
follow it after a slash, never stand in for it.
Say what the operator has to change before the upgrade can proceed; do not change it, and do not
propose deleting an exclusion. When the question is a target version's readiness, paste each
repository's deprecation section too, with its source line copied word for word
(`repo manifests as of <sha>`, not a rewording such as "at commit <sha>"), and state the floor
and target the scan used. Recommend the upgrade path and the manifest migrations; do not run either. Cite no CVE
identifiers: there is no vulnerability feed here, and every finding is version currency,
readiness or a declared API version.
