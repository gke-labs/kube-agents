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
since the previous run and which members have stopped moving (see "Track a rollout across runs" and `references/track-a-rollout.md`).
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

## Before you run anything: is this the brief?

A request for **the upgrade readiness brief**, for **which findings need a decision**, or for
**this week's upgrade readiness report** is not answered by this skill's table. Stop here, load the
`upgrade-readiness-brief` skill (`skill_view upgrade-readiness-brief`) and follow it: it reads the
Saturday review's copy under `/opt/data/upgrade-readiness/weekly/` and the findings queue, and
answers in the brief's shape, which opens with **Decide now** and the undecided critical findings'
queue ids. Run this skill's report only when that skill tells you to (no weekly review exists yet),
or when the question is the readiness table itself ("is every cluster ready", "what would stop the
upgrade", "which clusters lag").

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
  full run only, the `rollout` block and the per-member `progress` fields `references/track-a-rollout.md` describes. A
  `--cluster` run writes neither, since it neither reads nor compares the record; it prints a
  note where the progress section would be.
  The file is written whole (a temp file, then a rename) at the end of the run, so a reader never
  meets a partial document and a run that dies leaves the path as it found it. `/opt/data/scratch`
  is shared by every card, so an output name you choose carries a timestamp and the shell's PID, as
  the block above does; read back the path the script prints on its `Wrote … to` line.
  The weekly audit's collector is the exception: it moves its own per-run file onto the fixed name
  described below.
- `--rollout-in-progress` belongs to rollout tracking, below, and `--state-dir` to its record
  (`references/track-a-rollout.md`); `--readiness`, `--at` and `--kubeconfig-dir` to the
  readiness check, below that.

The script runs `gcloud container clusters list`, `gcloud container get-server-config` and, when neither
`--project` nor a declared scope is passed, `gcloud config get-value project` and `gcloud projects list` (plus `gcloud projects describe` for a
project whose `clusters list` was refused as API-disabled or whose configured identifier is a
project number), each with a 60-second timeout, and
with `--readiness` three `gcloud` list reads per project (`compute backend-services list`,
`compute instances list`, `container operations list`, each projected to the fields the per-entry
rules read), one `gcloud container clusters get-credentials` and the `kubectl get`s per
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

Every full run compares itself with the previous run for the same target and prints a "Rollout
progress" section saying, per member, what started, completed or stalled since then. Pass
`--rollout-in-progress` when the user says a rollout is under way; when there is no earlier record
to compare with and the user asks which members have stalled, run the report twice in the turn
with the flag and read the second run's section. What each progress value means, the record
file and the JSON: `references/track-a-rollout.md`.

## Check upgrade readiness

`--readiness` adds a readiness table after the version table, one row per member, graded against
the same target as the member's version row, and a `readiness` object per member in the JSON
(`members[].readiness`, with a top-level `readiness` block holding the instant evaluated and a
count per verdict), then the add-on inventory table (`members[].readiness.addons` in the JSON, the
known-issues rule's input). Without the flag nothing changes. The rules below each name the governance
SOP check they derive from, except the webhook rule, which has no SOP check yet; after them run
the per-entry rules registered in `upgrade_readiness.EXTRA_RULES` (`scripts/readiness_rules/`, one
module per rule, each stamped with the entry of the upgrade-failure catalogue in the repository's
design docs it reads (an entry can have several); the package
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
need pods; the Nodes through a jsonpath template that keeps each node's name, `providerID`, Pod CIDR, labels, taints,
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
note column does. The rules, one per catalogue entry or incident scenario, with their reads and
actions: [`references/readiness-rules.md`](references/readiness-rules.md). After them runs one plan-level rule, `rollout-sequence-precheck`
(no catalogue entry, never a blocker): for a fleet member it lists the fleet host project's
RolloutSequences once per run, with
`gcloud container fleet rolloutsequences list --project <host> --format=json`, filed on the host's
first member in the listing (the host's other members carry a note saying where, a non-member a
note that no sequence can carry it), and an existing sequence is
a risk naming its stages, since the platform allows one sequence per host project and refuses a
plan that creates a second; the clause after the cell's last `; ` is the one action.

A member is `blocked` when any rule blocks, whatever else could not be evaluated; `unknown` when
nothing blocked but a rule that could have blocked could not be evaluated (the cluster read
failed, there is no target, an exclusion's scope or a pool's version was unreadable, a per-entry
rule said so); `ready` only when every rule was evaluated and none blocks. A risk never moves the
verdict, and neither does an unknown from a rule that declares it can never block, which is
reported in the note. In the JSON each member carries `readiness.rules` (per rule id: its entry,
whether it can block, and a count of its blocking, risk, unknown and note entries),
`rule_blocking`, `risks` and `unknown` (the findings across rules, once: `rule`, `entry`, `text`; an unknown adds `reason` and `can_block`), `notes`, `addons` (the add-on inventory rows), `read_errors` (per kind) and `audit_log` (per read); the top-level
`readiness` block counts the members with risks (`members_with_risks`). `notes[]` are strings prefixed `<rule id>: <text>`; `rule_blocking[]`, `risks[]` and `unknown[]` are objects with `rule` and `text`. A per-rule question is answered from the JSON per member by [the recipe](references/readiness-rules.md#a-per-rule-answer): a note is an entry unless the question is for a risk or blocker (its finding-only form); `none` where `readiness.rules.<id>` counts nothing. A failed
`get-credentials`, PDB read or webhook read is listed under the table as a read failure for that
member and sets exit code 1, like a failed gcloud read. A failed `get-credentials` or PDB read
leaves the PDB rule and every per-entry rule that needs an object ungraded (the webhook read is
skipped when the API server did not answer the PDB read, and the reads after the webhook read are
skipped on any PDB failure), and a failed webhook read leaves the webhook rule alone ungraded,
while the member's maintenance and skew rules, and a per-entry rule that reads the cluster
record alone, are still graded, and the other members are unaffected. `--at` with a
value that is not RFC 3339, or `--at` or `--kubeconfig-dir` without `--readiness`, is a usage
error (exit 2).

## Scheduled runs of the readiness report

Besides the Monday upgrade audit, whose `upgrade-blocked` check runs `--readiness` for each cluster
found behind, two cron jobs on the Platform Agent's roster run the whole report without a request
(`cron/README.md` in the platform profile says how):

- `upgrade-readiness-watch`, daily: for a cluster below a version its release channel offers; the
  report lands under `upgrade-readiness/reports/<version>/` on the agent's volume with one chat line.
- `upgrade-readiness-weekly-review`, Saturdays: every cluster in scope, every blocker and risk
  registered on the findings queue; the technical report is `upgrade-readiness/weekly/latest.md`
  on the agent's volume, with a copy at `/opt/data/upgrade-readiness/weekly/latest.md` in the
  sandbox; the review job writes both, so never create, copy over or edit them from this skill
  (the brief reads them as the review's record). `upgrade-readiness-weekly-brief` follows it with
  the week's brief in chat. A user who
  asks for "this week's upgrade readiness report" gets that copy printed; one who asks for the brief,
  or for what needs their decision, gets the brief's shape composed from the copy and the findings
  queue by the `upgrade-readiness-brief` skill (the SOP's "On request" route as the agent loads
  it), never this skill's table or a summary of it: the brief leads with **Decide now** and the
  undecided critical findings' queue ids.

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
