---
name: fleet-upgrade-verification
description: Reports every GKE cluster's control-plane and node-pool versions against a target version or each cluster's release-channel default, naming the members that lag and by how many minors. Read-only, from gcloud container reads; the executed counterpart to gke-upgrades' advice.
---

# Fleet upgrade verification

Answer "against version X, which clusters lag, by how much, and is it the control plane or a node
pool" with a table read from the fleet, not from memory. Use it when a user asks whether an
upgrade has reached every cluster, which members are behind a target, or how far the fleet is
from a release-channel default. For upgrade plans, runbooks and checklists, use the `gke-upgrades`
skill; it links back here when the question is one this table answers.

"Version skew" here is the gap between a member's versions and the target. It is not
configuration drift: the fleet-consistency audit compares a cluster's configuration against its
live peers, and the drift-detection design (not yet shipped) means live state diverging from Git.
Neither reads versions against a target.

## Run the report

```bash
./skills/fleet-upgrade-verification/scripts/fleet_upgrade_report.py \
  [--project <project>]... [--target-version <version>] --output /opt/data/scratch/fleet_versions.json
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
- `--output` writes the same data as JSON: `members[]`, `errors[]` and a `summary` count per
  status.

The script runs `gcloud container clusters list`, `gcloud container get-server-config` and
`gcloud config get-value project`, each with a 60-second timeout. It changes nothing. A failed
or timed-out read is listed under the table and sets exit code 1; the other projects and
locations are still reported.

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

## Report

Paste the table into the reply, then name the lagging members with both versions and the gap,
and the patch-behind members separately. Recommend the upgrade path; do not run it. Cite no CVE
identifiers: there is no vulnerability feed here, and every finding is version currency.
