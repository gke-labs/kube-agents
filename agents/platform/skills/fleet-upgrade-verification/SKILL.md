---
name: fleet-upgrade-verification
description: Reports every GKE cluster's control-plane and node-pool versions against a target version or each cluster's release-channel default, naming the members that lag and by how many minors; run again during a rollout, it shows which members started, completed or stalled since the previous run. Read-only against GCP, from gcloud container reads, keeping only its own record of each run; the executed counterpart to gke-upgrades' advice.
---

# Fleet upgrade verification

Answer "against version X, which clusters lag, by how much, and is it the control plane or a node
pool" with a table read from the fleet, not from memory. Use it when a user asks whether an
upgrade has reached every cluster, which members are behind a target, or how far the fleet is
from a release-channel default. Run it again during a rollout and it also says, per member, what
changed since the previous run and which members have stopped moving (see "Track a rollout across
runs"). For upgrade plans, runbooks and checklists, use the `gke-upgrades` skill; it links back
here when the question is one this table answers.

"Version skew" here is the gap between a member's versions and the target. It is not
configuration drift: the fleet-consistency audit compares a cluster's configuration against its
live peers, and the drift-detection design (not yet shipped) means live state diverging from Git.
Neither reads versions against a target.

## Run the report

```bash
./skills/fleet-upgrade-verification/scripts/fleet_upgrade_report.py \
  [--project <project>]... [--target-version <version>] [--rollout-in-progress] \
  --output /opt/data/scratch/fleet_versions.json
```

- `--project` is repeatable and, when given, is the whole scope. Without it the script takes the
  union of `MONITORED_PROJECT_IDS` (comma-separated), `GCP_PROJECT_ID`, `GKE_PROJECT_ID` and
  `PROJECT_ID`, and asks gcloud for its configured project only when all four are empty.
- `--target-version` sets one target for every member, in the `MAJOR.MINOR.PATCH-gke.BUILD` form
  the fleet reports (`1.31.4-gke.1183000`); the `-gke.BUILD` suffix is optional and reads as build
  0 without it. Without the flag, each member is measured against its own release channel's
  `defaultVersion` from `gcloud container get-server-config`, fetched once per project and
  location, and the target column says which baseline was used, for example
  `1.31.4-gke.1183000 channel default (REGULAR)`.
- `--output` writes the same data as JSON: `members[]`, `errors[]`, a `summary` count per
  status, and the `rollout` block described below.
- `--rollout-in-progress` and `--state-dir` belong to rollout tracking, below.

The script runs `gcloud container clusters list`, `gcloud container get-server-config` and
`gcloud config get-value project`, each with a 60-second timeout. It changes nothing in GCP;
the only thing it writes is its own record under `/opt/data/state/fleet-upgrade-verification/`
and the `--output` file. A failed or timed-out read is listed under the table and sets exit code
1; the other projects and locations are still reported.

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

Every run records its per-member result and compares itself with the previous run for the same
target, so two runs during a rollout show what moved between them. The record lives at
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
printed. In the JSON, each member carries `progress`, `unchanged_since` and
`unchanged_for_seconds`, and the top-level `rollout` block has the record path, both run
timestamps, whether the rollout counted as active and why, a count per progress value, and the
members missing this run.

## Report

Paste the table into the reply, then name the lagging members with both versions and the gap,
and the patch-behind members separately. When the run printed a Rollout progress section, paste
it too and name each `stalled` member with its elapsed time and the previous run's time, as an
observation rather than a fault: a member whose wave has not been scheduled yet reads the same
as one that is stuck, and the elapsed time is what lets the user tell them apart. A first-run
baseline line means there is nothing to compare yet; say when to run again. Recommend the
upgrade path; do not run it. Cite no CVE identifiers: there is no vulnerability feed here, and
every finding is version currency.
