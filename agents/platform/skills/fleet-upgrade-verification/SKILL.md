---
name: fleet-upgrade-verification
description: Reports every GKE cluster's control-plane and node-pool versions against a target version or each cluster's release-channel default, naming the members that lag and by how many minors, and scans the linked GitOps repositories' manifests for apiVersions the target removes, with each hit's replacement. Read-only, from gcloud container reads and Git; the executed counterpart to gke-upgrades' advice.
---

# Fleet upgrade verification

Answer "against version X, which clusters lag, by how much, and is it the control plane or a node
pool" with a table read from the fleet, not from memory, and "which manifests in our GitOps
repositories declare an API that version X removes" with a scan read from Git. Use it when a user
asks whether an upgrade has reached every cluster, which members are behind a target, how far the
fleet is from a release-channel default, or whether the repositories are ready for a target
version. For upgrade plans, runbooks and checklists, use the `gke-upgrades` skill; it links back
here when the question is one these two scripts answer.

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

## Scan the GitOps manifests

```bash
./skills/fleet-upgrade-verification/scripts/api_deprecation_scan.py \
  --versions /opt/data/scratch/fleet_versions.json --target-version <version> \
  [--repo <owner/name>]... [--manifests-dir <path>] --output /opt/data/scratch/api_deprecations.json
```

- `--versions` is the version report's `--output`. The scan's floor is its lowest control-plane
  minor: the API server is what stops serving a removed version, so node-pool versions do not
  enter into it. `--current-version <version>` replaces the file when there is none.
  `--target-version` defaults to the file's `target_version` when the report was run with one.
- Without `--repo` the script scans every GitHub repository under `managed_repos`, the same
  list the GitOps skills write to; `--repo` is repeatable and, when given, is the whole scope.
  `--manifests-dir` scans a local tree instead of, or as well as, repositories.
- It reads each repository the way `inspect-repository` does: through the credential broker's
  content workspaces as a shallow read-only clone, or, on an install whose broker is not armed
  for content-passing, through a leased checkout on the shared volume. It runs no `gcloud` and
  writes nothing. A repository the broker or git cannot serve is listed under errors and sets
  exit code 1; the other repositories are still reported.
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
skipped files is clean for what it read, so say that.

The scan reads what Git declares. Whether a client is still calling a deprecated API on a live
cluster is a different question, answered by GKE Deprecation Insights from the cluster's audit
logs: the console page the report's footer links, or
`gcloud recommender insights list --insight-type=google.container.DiagnosisInsight`, which the
report quotes for a human to run. That command is not in the agent's gcloud read allowlist, so
give it to the user rather than running it, and do not present the Git scan as proof that no
client uses the API.

## Report

Paste the table into the reply, then name the lagging members with both versions and the gap,
and the patch-behind members separately. When the question is a target version's readiness,
paste each repository's deprecation section too, with its source line, and state the floor and
target the scan used. Recommend the upgrade path and the manifest migrations; do not run either.
Cite no CVE identifiers: there is no vulnerability feed here, and every finding is version
currency or a declared API version.
