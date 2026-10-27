---
title: Release versioning & artifacts
description: What a kube-agents version number means, what each release publishes, how to verify a release bundle, how the Helm chart is versioned, and how to pin a release in Terraform.
sidebar:
  order: 4
---

`kube-agents` follows [Semantic Versioning 2.0.0](https://semver.org/) (`MAJOR.MINOR.PATCH`) without a `v` prefix across container images, OCI Helm charts, and Terraform modules.

Every release is cut from one commit: the installer scripts (`install.sh`, `uninstall.sh`, `upgrade.sh`), the Helm chart and the container images of a release all come from it, and the release has passed an install and end-to-end run on a live GKE cluster before its tag is published. An emergency release can skip that live run, never the build checks: its images must already exist for the commit, and a version tag that already points elsewhere aborts it. The maintainers' procedure is [`scripts/release/README.md`](https://github.com/gke-labs/kube-agents/tree/main/scripts/release).

Moving an install back to the previous GA release is its own page,
[Rolling back a release](/kube-agents/deploy/rollback/): the two `upgrade.sh` commands run from the
older release's checkout, and what they leave as it is.

## What a version number means

The next version is calculated from the Conventional Commits merged since the previous release:

- A release from `main` bumps `MINOR`. Almost every range carries a `feat:`, and once the previous minor has its release line (`release/X.Y`), a fix-only range bumps `MINOR` too, because the patch numbers belong to the line. So the minor number advances with each release rather than with how much changed; the release notes say what changed between two versions.
- A `PATCH` release, `X.Y.Z+1`, is cut from the release line `release/X.Y`: fixes are cherry-picked onto the line. A `feat:` or a breaking change is refused on a line.
- A breaking change (`feat!:`, `fix!:`, `BREAKING CHANGE:`) bumps `MINOR` while the version is `0.y.z`, per [SemVer 2.0 Clause 4](https://semver.org/#spec-item-4), and `MAJOR` from `1.0.0` on.
- Nothing promotes `0.y.z` to `1.0.0` on its own. Declaring API stability is a decision the maintainers make and publish with an explicit version.

[The releases page](https://github.com/gke-labs/kube-agents/releases) lists every release; count
the releases between two versions there, not by date.
[The latest release](https://github.com/gke-labs/kube-agents/releases/latest) is the numerically
highest version, which may be a patch cut on the newest release line; a patch below the newest
minor is published without the `latest` mark.

## What the next release contains

Generated release notes are the tracking mechanism. GitHub milestones are unused.

GitHub writes each release's notes from the pull requests merged between the previous release tag
and the new one, grouped by label: features, bug fixes, security, documentation, infrastructure,
and a catch-all for anything else. The grouping is the label-to-category map in
[`.github/release.yml`](https://github.com/gke-labs/kube-agents/blob/main/.github/release.yml),
so a pull request's labels decide the heading it appears under. Dependabot's pull requests, and any
labelled `duplicate`, `invalid` or `wontfix`, are left out.

No workflow applies these labels, so today every pull request appears under "Other Changes". The
titles follow Conventional Commits, and a `!` before the colon marks a breaking change. The Upgrade
page lists [the breaking changes in recent releases](/kube-agents/install/upgrade/#breaking-changes-in-recent-releases).

Before it exists, the next release from `main` is whatever has merged since the most recent
release cut from `main`:
`https://github.com/gke-labs/kube-agents/compare/<MAIN_GA_TAG>...main`, with the newest GA tag
cut from `main`, lists every pull request it will contain if it is cut from the tip of `main`.

## What a release publishes

1. The container images built for the release's commit are retagged to `X.Y.Z` in GHCR without being rebuilt, and signed with keyless Cosign through GitHub Actions OIDC.
2. The Helm chart is packaged at version `X.Y.Z` (matching `appVersion`), pushed as an OCI package to `oci://ghcr.io/gke-labs/kube-agents/charts/kube-agents:X.Y.Z`, and its OCI manifest signed with Cosign.
3. The tag sits on a stamped release commit: `BAKED_RELEASE_VERSION="X.Y.Z"` is written into the root scripts (`install.sh`, `uninstall.sh`, `upgrade.sh`), the chart version (`charts/kube-agents/Chart.yaml`) and the Terraform default image tags (`terraform/examples/full-install/variables.tf`, `terraform.tfvars.example`). Both `install.sh` and `upgrade.sh` check that a Git checkout matches the tag's commit and that a tree outside Git carries a matching stamp or bundle marker; `upgrade.sh` also refuses a tree whose stamp names another release. [Provenance attribution](#provenance-attribution-and-release-bundle-marker) has the steps.
4. The offline bundle is staged from the tagged commit with `git archive`, carries the `.release-bundle` provenance marker, and is packaged as `.tar.gz` and `.zip`.
5. Software Bills of Materials are generated with Syft, SPDX 2.3 JSON and CycloneDX 1.5 JSON for the bundle and SPDX 2.3 JSON for each container image, and published with `checksums.txt`, which carries SHA256 checksums for every release asset.

## Offline distribution bundles and SBOMs

For air-gapped or restricted network environments where cloning the repository or pulling directly from GitHub is disallowed, official releases provide pre-packaged distribution bundles and Software Bill of Materials (SBOM).

### Distribution bundle assets

Each GA release attaches the following distribution artifacts to the GitHub Release:

- `kube-agents-<VERSION>.tar.gz` and `kube-agents-<VERSION>.zip`: Complete, self-contained offline distribution bundles containing Terraform provisioning modules (`terraform/`), Kubernetes operator manifests (`k8s-operator/`), deployment configs (`deploy/`), Helm charts (`charts/`), utility scripts (`scripts/`), examples (`examples/`), installer scripts (`install.sh`, `upgrade.sh`, `uninstall.sh`), and the mirrored image catalog (`images.json`). Tracked example files (`terraform.tfvars.example`) are preserved, while sensitive tokens and local caches are sanitized.
- `kube-agents-<VERSION>.tgz`: Packaged Helm chart with matching `version` and `appVersion`.
- `kube-agents-<VERSION>.spdx.json` and `kube-agents-<VERSION>.cdx.json`: Software Bill of Materials (SBOM) for the filesystem bundle in SPDX 2.3 and CycloneDX 1.5 JSON formats.
- `<image>-<VERSION>.spdx.json` for each container image the release ships, the `REQUIRED_RELEASE_IMAGES` list in `scripts/release/common.sh` at the release's own commit (a patch cut from an older release line ships the images that line has): Container image SBOMs in SPDX 2.3 JSON format generated by Syft.
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
  source       = "git::https://github.com/gke-labs/kube-agents.git//terraform/modules/gke-cluster?ref=0.19.0"
  project_id   = var.project_id
  cluster_name = "production-host-01"
  location     = "us-central1"
}
```
