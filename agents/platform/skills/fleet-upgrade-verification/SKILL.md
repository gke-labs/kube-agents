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

"Version skew" here is the gap between a member's versions and the target. It is not the
configuration drift the fleet-consistency audit measures against Git.

## Run the report

```bash
./skills/fleet-upgrade-verification/scripts/fleet_upgrade_report.py \
  [--project <project>]... [--target-version <version>] --output /opt/data/scratch/fleet_versions.json
```

- `--project` is repeatable. Without it the script enumerates `MONITORED_PROJECT_IDS`, then
  `GCP_PROJECT_ID`, then gcloud's configured project.
- `--target-version` sets one target for every member, in the `MAJOR.MINOR.PATCH-gke.BUILD` form
  the fleet reports (`1.31.4-gke.1183000`). Without it, each member is measured against its own
  release channel's `defaultVersion` from `gcloud container get-server-config`, fetched once per
  location, and the target column says which baseline was used, for example
  `1.31.4-gke.1183000 channel default (REGULAR)`.
- `--output` writes the same data as JSON: `members[]`, `errors[]` and a `summary` count per
  status.

The script runs `gcloud container clusters list`, `gcloud container get-server-config` and
`gcloud config get-value project`. It changes nothing. A failed read is listed under the table
and sets exit code 1; the other projects and locations are still reported.

## Read the table

One row per cluster: project, cluster, location, channel, control-plane version, the lowest
node-pool version with its pool name, target, gap in minors, status, note.

- `lagging`: the control plane or at least one node pool is below the target. The gap is the
  number of minors the lowest component trails by; `0` with `lagging` means the same minor but a
  patch or build behind, and the note says so.
- `current`: control plane and every pool equal the target.
- `ahead`: newer than the target and nothing below it. Reported, never flagged; channel rollout
  waves are staged, so a member ahead of its channel default is routine. The gap is negative.
- `unknown`: a version did not parse, the cluster has no release channel and no
  `--target-version` was given, the cluster record has no node pools, or `get-server-config`
  failed for the location. The note names which.

A note reading `upgrade in flight` means the cluster or a pool is `RECONCILING` or
`PROVISIONING`; report that row as in progress rather than as a stall.

## Report

Paste the table into the reply, then name the lagging members with both versions and the gap.
Recommend the upgrade path; do not run it. Cite no CVE identifiers: there is no vulnerability
feed here, and every finding is version currency.
