---
title: Release lifecycle, versioning & operations
description: The release cadence, and how Kube-Agents automates SemVer 2.0 releases, validates release candidates on live GKE clusters, and publishes immutable artifacts.
sidebar:
  order: 4
---

`kube-agents` follows strict [Semantic Versioning 2.0.0](https://semver.org/) (`MAJOR.MINOR.PATCH`) without a `v` prefix for official releases across container images, OCI Helm charts, and Terraform modules.

The release pipeline guarantees that installer scripts (`install.sh`, `uninstall.sh`, `upgrade.sh`) and container runtime images are bit-for-bit synchronized from the exact same commit and, absent an emergency bypass, validated on a live GKE cluster before any release tag is published.

Moving an install back to the previous GA release is its own page,
[Rolling back a release](/kube-agents/deploy/rollback/): the two `upgrade.sh` commands run from the
older release's checkout, and what they leave as it is.

## Tag and artifact taxonomy

Every commit and build progresses through six distinct lifecycle tiers:

| Tier                       | Format                                | Trigger                                       | Purpose and guarantees                                                                                                                                                      |
| :------------------------- | :------------------------------------ | :-------------------------------------------- | :-------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Candidate Build**        | `<COMMIT_SHA>` (bare 40-char SHA)     | Push to `main` or to a `release/<X.Y>` branch | Developer build in GHCR; container images built once.                                                                                                                       |
| **Release Candidate (RC)** | `rc_YYMMDDHHMM_<SHORT_SHA>`           | 3-hour cron / manual dispatch                 | Candidate build selected for live cluster testing.                                                                                                                          |
| **RC Validated**           | `rc_YYMMDDHHMM_<SHORT_SHA>_validated` | Successful GKE E2E suite                      | Quality gate: proof that `install.sh` succeeded on a real GKE cluster.                                                                                                      |
| **Eval Candidate**         | `evalcand_YYMMDDHHMM_<SHORT_SHA>`     | Successful nightly matrix                     | Nomination, not a promotion: starts the agent eval against the candidate's images, over the same case matrix that gates a pull request. Deploys nothing.                    |
| **Staging Promoted**       | `staging_YYMMDDHHMM_<SHORT_SHA>`      | Green eval on the nomination                  | Quality gate for GA: the nightly E2E matrix and the agent eval both passed. Also the deploy trigger for the staging estate.                                                 |
| **GA Stable**              | `X.Y.Z` (pure numeric SemVer)         | Weekly cron / manual dispatch                 | Official production release tagged on a stamped commit parented by the target commit (staging-promoted by default), and pushed to the version's release line `release/X.Y`. |

Only a staging-promoted commit is releasable from `main`; a patch from a release line is gated on
the `rc_*_validated` tag on the line's head instead. An `rc_*_validated` tag records the narrow
three-hourly suite; `main`'s GA gate reads the `staging_<ts>_<sha>` tag, which the nightly pipeline
pushes only after the full matrix passes and the agent eval it nominated the candidate for comes
back green.

The two-tag split is what makes the eval a gate rather than a report. Both tags name the same
candidate and the eval runs between them: pushing `evalcand_<core>` starts the eval against the
candidate's published images, and the pipeline waits for its verdict before pushing
`staging_<core>`. One tag could not do both — the staging deploy would reach the cluster in
minutes while the eval was still hours from an answer. An `evalcand_` tag with no `staging_` tag
beside it is therefore a candidate the eval rejected, one whose eval has not finished, or — rarely —
one whose eval never returned a verdict and whose nomination the pipeline then failed to withdraw.
Nothing deploys from any of them.

Withdrawal is the difference between the first case and the last. A rejected candidate keeps its
`evalcand_` tag for good, which is what stops every later nightly re-measuring a build already
judged. A candidate whose eval broke before measuring anything has its tag deleted instead, so the
next nightly can pick the same commit up and ask again; if that deletion fails, the nightly run
fails with it, and the tag has to be deleted by hand before the commit can be measured.

## Release cadence

The RC pipeline, the nightly staging promotion, and the GA release all run on schedules,
with manual dispatches available for overrides and off-schedule releases.

| Step                        | When it runs                                                                                                                         |
| :-------------------------- | :----------------------------------------------------------------------------------------------------------------------------------- |
| RC selection and validation | Every three hours, at 17 minutes past. Dispatches nothing when the newest candidate on `main` has already been tried.                |
| Staging promotion           | Daily at 02:17 UTC, against the newest validated candidate on `main`. One already nominated or promoted is re-tested, not re-tagged. |
| GA release                  | Weekly on Fridays at 05:17 UTC, or when a maintainer dispatches it.                                                                  |

A staging promotion is not finished when its matrix goes green. The eval runs between the
nomination and the tag and takes hours, so the `staging_*` tag can appear most of a working day
after the run that produced it started.

Scheduled runs start when GitHub's scheduler picks them up, so the minute is a floor, not a
promise. A scheduled GA release ships unattended on Fridays if a new staging-promoted
candidate exists, or maintainers may dispatch the workflow by hand.

### What the next release contains

Generated release notes are the tracking mechanism. GitHub milestones are unused.

GitHub writes each release's notes from the pull requests merged between the previous release tag
and the new one, grouped by label: features, bug fixes, security, documentation, infrastructure,
and a catch-all for anything else. The grouping is the label-to-category map in
[`.github/release.yml`](https://github.com/gke-labs/kube-agents/blob/main/.github/release.yml),
so a pull request's labels decide the heading it appears under. Dependabot's pull requests, and any
labelled `duplicate`, `invalid` or `wontfix`, are left out. Read them on
[the releases page](https://github.com/gke-labs/kube-agents/releases) once the release exists.

Before it exists, the next release from `main` is whatever has merged since the most recent
release cut from `main`. [The latest release](https://github.com/gke-labs/kube-agents/releases/latest)
is the numerically highest version, which may be a patch cut on the newest release line; a patch
below the newest minor is published without the `latest` mark. Neither moves `main`'s base.

- `https://github.com/gke-labs/kube-agents/compare/<MAIN_GA_TAG>...main`, with the newest GA tag
  cut from `main`, lists every pull request the next release will contain if it is cut from the
  tip of `main`. The GA tag sits on a stamped commit whose parent is the released candidate, so
  the three-dot compare starts from that candidate.
- Replace `main` with the newest `staging_<ts>_<sha>` tag to see what is releasable now; that is
  the commit an ordinary dispatch releases.

## Automated SemVer 2.0 calculation

When the GA release workflow runs, it inspects Conventional Commits in the range `<BASE_GA_TAG>..<TARGET_COMMIT>`, where the base is the GA release the target's own history descends from (resolving to the latest staging-promoted commit on the standard automated path, the specified commit / `HEAD` under emergency bypass, or the head of `release/X.Y` for a patch from a release line):

<!-- prettier-ignore -->
| Commit type in release range | Current version | Calculated next version | Precedence and action |
| :--- | :--- | :--- | :--- |
| `fix:`, `chore:`, `docs:`, `perf:` | `0.2.0` | `0.2.1` | Patch bump |
| `feat:` | `0.2.0` | `0.3.0` | Minor bump, Patch resets to 0 |
| `feat!:`, `fix!:`, `BREAKING CHANGE:` | `0.2.0` | `0.3.0` | Minor bump (SemVer 2.0 Clause 4 in `0.y.z`) |
| `feat!:`, `fix!:`, `BREAKING CHANGE:` | `1.2.0` | `2.0.0` | Major bump (in `1.x.x`+) |
| _(No new commits in release range)_ | `0.2.0` | `0.2.0` | No changes; the eligibility check then skips the release (`skip_release=true`) |
| `fix:` on `main` once `release/0.2` exists | `0.2.0` | `0.3.0` | Minor bump: patch numbers belong to the line |
| `fix:` on `release/0.2` | `0.2.0` | `0.2.1` | Patch bump; a `feat:` or a breaking change on a line is refused |

### SemVer 2.0 Clause 4 and the 1.0.0 manual governance rule

During initial development (`0.y.z`), any breaking change increments `MINOR` (`0.2.1` -> `0.3.0`) and resets `PATCH` to 0, per [SemVer 2.0 Clause 4](https://semver.org/#spec-item-4).

The automated version calculator never promotes `0.y.z` to `1.0.0` on its own. Declaring API stability and graduating to `1.0.0` is a manual governance decision by project maintainers, who publish that release with an explicit version.

Once `1.0.0` is established, the automated calculator resumes standard SemVer rules: breaking changes bump `MAJOR`, new features bump `MINOR`, and bug fixes bump `PATCH`, except on `main` once the base release's line has its branch, where a fix-only range bumps `MINOR` because the patch numbers belong to the line.

## Who cuts a release

Maintainers do, on the Friday schedule above or by hand. A patch release, `X.Y.Z+1`, is cut from the release line `release/X.Y` rather than from `main`: fixes are cherry-picked onto the line, its head passes the RC validation, and a maintainer dispatches the release naming the line; once a line has its branch, a fix-only week on `main` releases as the next minor. An emergency hotfix can skip the live-cluster validation gate — reserved for a zero-day vulnerability in a container dependency or a regression prolonging user-facing downtime — but never the build-integrity guarantees below: the images must already exist for the commit, a written justification is required, and a version tag that already points at another commit aborts the release. The dispatch commands, the bypass, and the post-release reconciliation are the maintainers' runbook in [`scripts/release/README.md`](https://github.com/gke-labs/kube-agents/tree/main/scripts/release).

## Clean promotion and artifact guarantees

The release publish workflow enforces byte-for-byte fidelity with tested candidate binaries across seven layers:

1. Container images are compiled once, when a commit is pushed to `main` or merged onto a `release/<X.Y>` branch; on a release line a commit that already has images is never rebuilt, `:latest` moves only for `main`, and a push to one of the legacy per-release `release/X.Y.Z` branches (0.1.0 through 0.7.0) builds nothing. The release retags the existing `<TARGET_COMMIT>` manifests to numeric `X.Y.Z` in GHCR without rebuilding.
2. Promoted container images in GHCR are cryptographically signed using Keyless Cosign via GitHub Actions OIDC tokens.
3. The Helm chart is packaged at version `X.Y.Z` (matching `appVersion`), pushed as an OCI package to `oci://ghcr.io/gke-labs/kube-agents/charts/kube-agents:X.Y.Z`, and its OCI manifest signed via Cosign.
4. A single-parent release commit is created on detached HEAD with `BAKED_RELEASE_VERSION="X.Y.Z"` stamped into the root scripts (`install.sh`, `uninstall.sh`, `upgrade.sh`), the Helm chart version (`charts/kube-agents/Chart.yaml`) and the Terraform default image tags (`terraform/examples/full-install/variables.tf`, `terraform.tfvars.example`); the tag is placed on that stamped commit and pushed together with the release line `release/X.Y`, in one atomic push: the line is created by its first release and fast-forwarded by each later patch.
5. `install.sh` and `upgrade.sh` verify that unversioned source directories match `BAKED_RELEASE_VERSION` and that Git checkouts match the requested tag's commit, halting if local scripts diverge from the container images.
6. The offline release bundle is staged directly from the tagged release commit with `git archive`, carries the `.release-bundle` provenance marker, and is packaged as both `.tar.gz` and `.zip`.
7. Software Bills of Materials are generated with Syft — SPDX 2.3 JSON and CycloneDX 1.5 JSON for the filesystem bundle, SPDX 2.3 JSON for each container image — and published alongside `checksums.txt` with SHA256 checksums for every release asset.

## Offline distribution bundles and SBOMs

For air-gapped or restricted network environments where cloning the repository or pulling directly from GitHub is disallowed, official releases provide pre-packaged distribution bundles and Software Bill of Materials (SBOM).

### Distribution bundle assets

Each GA release attaches the following distribution artifacts to the GitHub Release:

- `kube-agents-<VERSION>.tar.gz` and `kube-agents-<VERSION>.zip`: Complete, self-contained offline distribution bundles containing Terraform provisioning modules (`terraform/`), Kubernetes operator manifests (`k8s-operator/`), deployment configs (`deploy/`), Helm charts (`charts/`), utility scripts (`scripts/`), examples (`examples/`), installer scripts (`install.sh`, `upgrade.sh`, `uninstall.sh`), and the mirrored image catalog (`images.json`). Tracked example files (`terraform.tfvars.example`) are preserved, while sensitive tokens and local caches are sanitized.
- `kube-agents-<VERSION>.tgz`: Packaged Helm chart with matching `version` and `appVersion`.
- `kube-agents-<VERSION>.spdx.json` and `kube-agents-<VERSION>.cdx.json`: Software Bill of Materials (SBOM) for the filesystem bundle in SPDX 2.3 and CycloneDX 1.5 JSON formats.
- `<image>-<VERSION>.spdx.json` for each of the seven release images (`k8s-operator`, `platform-agent`, `credential-proxy`, `agent-sandbox`, `replay-proxy`, `pubsub-platform`, `gke-stockout-investigator`): Container image SBOMs in SPDX 2.3 JSON format generated by Syft.
- `checksums.txt`: SHA256 cryptographic checksums covering all distribution tarballs, zips, charts, and SBOM JSON files.
- `checksums.txt.bundle`: Keyless Cosign signature bundle attesting to the provenance and authenticity of `checksums.txt` signed via GitHub Actions OIDC.

### Provenance attribution and `.release-bundle` marker

Every packaged release bundle contains a `.release-bundle` metadata file at its root, attesting to the release provenance:

```ini
name=kube-agents
version=<VERSION>
tag=<VERSION>
commit=<STAMPED_RELEASE_COMMIT_SHA>
build_date=YYYY-MM-DDTHH:MM:SSZ
```

The `commit` field records the SHA of the tagged release commit (the single-parent stamped commit created on detached HEAD parented by the candidate commit).

When `install.sh` or `upgrade.sh` executes from an unversioned directory outside Git, `verify_local_source_ref` verifies source integrity in two steps:

1. If `.release-bundle` is present with matching `version` or `tag`, and the `BAKED_RELEASE_VERSION` stamped into the running script matches the requested release, it attributes the source directory to the official release bundle and logs:

```text
✓ Verified install sources match official release bundle <VERSION>.
```

(or `✓ Verified upgrade sources match official release bundle <VERSION>.` during upgrades). If the marker, or the version stamped into the tree's own root scripts, names a different release, `upgrade.sh` refuses; `install.sh` does not yet make that check.

2. If `.release-bundle` is absent, both scripts accept a matching `BAKED_RELEASE_VERSION` stamped into the script and report matching the baked release. With no stamp, `install.sh` refuses unless `--allow-unverified-source` is passed and `upgrade.sh` refuses; under `--dry-run` both warn and continue.

### Verifying release bundle integrity and provenance

Consumers can verify both the cryptographic provenance and integrity of downloaded release assets. First, verify the authenticity of `checksums.txt` using Keyless Cosign:

```bash
cosign verify-blob \
  --bundle checksums.txt.bundle \
  --certificate-identity-regexp "^https://github\.com/gke-labs/kube-agents/" \
  --certificate-oidc-issuer "https://token.actions.githubusercontent.com" \
  checksums.txt
```

Once `checksums.txt` is verified against GitHub Actions OIDC provenance, verify downloaded files against the checksums:

```bash
sha256sum -c checksums.txt --ignore-missing
```

### Inspecting Software Bill of Materials (SBOM)

SBOMs are generated using Syft and can be inspected using standard security and compliance tooling:

```bash
# Inspect filesystem package inventory from SPDX SBOM using jq:
jq '.packages[] | {name: .name, version: .versionInfo, license: .licenseConcluded}' kube-agents-<VERSION>.spdx.json

# Inspect filesystem components in CycloneDX format:
jq '.components[] | {name: .name, version: .version, type: .type}' kube-agents-<VERSION>.cdx.json

# Inspect container image packages (e.g. operator runtime dependencies):
jq '.packages[] | {name: .name, version: .versionInfo}' k8s-operator-<VERSION>.spdx.json
```

## Helm chart versioning

The chart `version` tracks the application `appVersion`: the release workflow packages the
chart with both `version` and `appVersion` set to the exact SemVer release tag `X.Y.Z`, so every
chart release corresponds to exactly one application release. There is no chart-only release
train — a chart-template fix ships with the next `X.Y.Z` tag.

## Pinning Terraform module versions in GitOps

When configuring GitOps repositories, pin companion Terraform modules using the exact SemVer Git tag:

```hcl
module "gke_cluster" {
  source       = "git::https://github.com/gke-labs/kube-agents.git//terraform/modules/gke-cluster?ref=0.3.0"
  project_id   = var.project_id
  cluster_name = "production-host-01"
  location     = "us-central1"
}
```
