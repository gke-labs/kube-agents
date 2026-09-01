---
title: Release lifecycle, versioning & operations
description: How Kube-Agents automates SemVer 2.0 releases, validates release candidates on live GKE clusters, and publishes immutable artifacts.
sidebar:
  order: 4
---

`kube-agents` follows strict [Semantic Versioning 2.0.0](https://semver.org/) (`MAJOR.MINOR.PATCH`) without a `v` prefix for official releases across container images, OCI Helm charts, and Terraform modules.

The release pipeline guarantees that installer scripts (`install.sh`, `uninstall.sh`, `upgrade.sh`) and container runtime images are bit-for-bit synchronized from the exact same commit and, absent an emergency bypass, validated on a live GKE cluster before any release tag is published.

## Tag and artifact taxonomy

Every commit and build progresses through five distinct lifecycle tiers:

| Tier                       | Format                                | Trigger                       | Purpose and guarantees                                                                                                 |
| :------------------------- | :------------------------------------ | :---------------------------- | :--------------------------------------------------------------------------------------------------------------------- |
| **Candidate Build**        | `<COMMIT_SHA>` (bare 40-char SHA)     | Push to `main` branch         | Developer build in GHCR; container images built once.                                                                  |
| **Release Candidate (RC)** | `rc_YYMMDDHHMM_<SHORT_SHA>`           | 3-hour cron / manual dispatch | Candidate build selected for live cluster testing.                                                                     |
| **RC Validated**           | `rc_YYMMDDHHMM_<SHORT_SHA>_validated` | Successful GKE E2E suite      | Quality gate: proof that `install.sh` succeeded on a real GKE cluster.                                                 |
| **Staging Promoted**       | `staging_YYMMDDHHMM_<SHORT_SHA>`      | Successful nightly matrix     | Quality gate for GA: the full nightly E2E matrix passed on the commit. Also the deploy trigger for the staging estate. |
| **GA Stable**              | `X.Y.Z` (pure numeric SemVer)         | Release publish workflow      | Official production release tagged on a stamped commit parented by the target commit (staging-promoted by default).    |

Only a staging-promoted commit is releasable. An `rc_*_validated` tag records the narrow
three-hourly suite; the GA gate reads the `staging_<ts>_<sha>` tag that `nightly-pipeline.yml`
pushes after the full matrix passes
([`scripts/release/README.md`](https://github.com/gke-labs/kube-agents/tree/main/scripts/release)).
The gate matches that tag's shape rather than the bare `staging_` prefix, because the prefix is
also a hand-pushable redeploy trigger.

## Automated SemVer 2.0 calculation

When the GA release workflow runs, `scripts/release/calculate_next_version.sh` inspects Conventional Commits in the range `<LATEST_GA_TAG>..<TARGET_COMMIT>` (resolving to the latest staging-promoted commit on the standard automated path, or the specified commit / `HEAD` under emergency bypass):

<!-- prettier-ignore -->
| Commit type in release range | Current version | Calculated next version | Precedence and action |
| :--- | :--- | :--- | :--- |
| `fix:`, `chore:`, `docs:`, `perf:` | `0.2.0` | `0.2.1` | Patch bump |
| `feat:` | `0.2.0` | `0.3.0` | Minor bump, Patch resets to 0 |
| `feat!:`, `fix!:`, `BREAKING CHANGE:` | `0.2.0` | `0.3.0` | Minor bump (SemVer 2.0 Clause 4 in `0.y.z`) |
| `feat!:`, `fix!:`, `BREAKING CHANGE:` | `1.2.0` | `2.0.0` | Major bump (in `1.x.x`+) |
| _(No new commits in release range)_ | `0.2.0` | `0.2.0` | No changes (`skip_release=true`) |

### SemVer 2.0 Clause 4 and the 1.0.0 manual governance rule

During initial development (`0.y.z`), any breaking change increments `MINOR` (`0.2.1` -> `0.3.0`) and resets `PATCH` to 0, per [SemVer 2.0 Clause 4](https://semver.org/#spec-item-4).

The automated version calculator never promotes `0.y.z` to `1.0.0` on its own. Declaring API stability and graduating to `1.0.0` is a manual governance decision by project maintainers, triggered explicitly through the `explicit_release_version: "1.0.0"` workflow input.

Once `1.0.0` is established, the automated calculator resumes standard SemVer rules: breaking changes bump `MAJOR`, new features bump `MINOR`, and bug fixes bump `PATCH`.

## Cutting a GA release

### Prerequisites

Before triggering a production release:

1. Target commit must exist on the `main` branch.
2. Target commit must carry a `staging_<ts>_<sha>` tag created by the nightly promotion pipeline ([`scripts/release/README.md`](https://github.com/gke-labs/kube-agents/tree/main/scripts/release)). An `rc_*_validated` tag is not checked alongside it: a staging tag is only ever derived from a candidate that already carries one.
3. All four required container images (`k8s-operator`, `platform-agent`, `credential-proxy`, `replay-proxy`) must exist in GHCR under `<TARGET_COMMIT>`.
4. GitHub CLI (`gh`) version 2.40.0 or newer installed and authenticated with `repo` and `workflow` permissions (`gh auth status`).

### Triggering the release workflow

Execute `.github/workflows/release-publish.yml` from the GitHub Actions web interface or via the GitHub CLI:

```bash
# Standard automated release (SemVer calculated automatically from Conventional Commits):
gh workflow run release-publish.yml --repo gke-labs/kube-agents

# Releasing a specific staging-promoted commit:
gh workflow run release-publish.yml --repo gke-labs/kube-agents \
  -f target_commit="<TARGET_COMMIT_SHA>"

# Manual version override (e.g. promoting 0.y.z to 1.0.0):
gh workflow run release-publish.yml --repo gke-labs/kube-agents \
  -f explicit_release_version="1.0.0"
```

Every release is started by hand: the workflow has no `schedule:`. It carries the gate an
unattended run would need — release only a staging-promoted candidate, skip quietly when nothing
new has landed since the last GA tag, and stop for a human when a breaking change is waiting to
ship — behind a `schedule_gate` input that defaults to `bypass`, so a dispatch publishes exactly as
it did before. `dry-run` reports the verdict in the job summary and publishes nothing; `evaluate`
acts on it, as a cron tick would.
[`scripts/release/README.md`](https://github.com/gke-labs/kube-agents/tree/main/scripts/release) is
canonical for that gate and for the cadence.

## Emergency hotfix runbook

The release gatekeeper enforces staging promotion (`staging_<ts>_<sha>`, the full nightly GKE E2E matrix) by default. In emergency situations, maintainers can bypass the live GKE validation gate while preserving all cryptographic and build integrity invariants.

### Eligibility criteria

Emergency gate bypass (`skip_staging_validation: true`) is strictly reserved for two scenarios: zero-day CVE vulnerabilities in container dependencies requiring immediate publication, or critical production regressions where waiting for the next nightly promotion or cluster provisioning would prolong user-facing downtime.

### Enforced security invariants

Even during an emergency bypass, the pipeline enforces three hard safety barriers:

1. `scripts/release/verify_release_eligibility.sh` checks that all four container images (`k8s-operator`, `platform-agent`, `credential-proxy`, `replay-proxy`) exist in GHCR under `<TARGET_COMMIT>`. Releases of unbuilt commits hard-fail.
2. `emergency_override_reason` must contain a non-whitespace justification; empty or whitespace-only reasons abort the workflow (`exit 1`).
3. Target SemVer tags must not already exist on another commit; tag collisions abort the release.

### Executing an emergency hotfix

To publish an emergency release via the GitHub CLI. Always specify `target_commit` to pin the exact hotfix commit SHA; omitting `target_commit` causes the workflow to default to the tip of `main` (`HEAD`), releasing all intervening commits without prior GKE E2E validation:

```bash
# Emergency release from a specific commit SHA (version calculated automatically):
gh workflow run release-publish.yml --repo gke-labs/kube-agents \
  -f skip_staging_validation=true \
  -f emergency_override_reason="CVE-2026-XXXX: Critical vulnerability in base container dependencies" \
  -f target_commit="<HOTFIX_COMMIT_SHA>"

# Emergency release from a specific commit SHA with explicit SemVer override:
gh workflow run release-publish.yml --repo gke-labs/kube-agents \
  -f skip_staging_validation=true \
  -f emergency_override_reason="Critical regression fix for gateway admission deadlock" \
  -f target_commit="<HOTFIX_COMMIT_SHA>" \
  -f explicit_release_version="0.3.1"
```

### Post-release reconciliation

After an emergency publication, complete these three reconciliation steps:

1. Confirm the published release tag, container images in GHCR, and signed Helm OCI chart via `gh release view <VERSION>`.
2. Manually trigger `.github/workflows/rc-release-pipeline.yml` against the hotfix commit (`-f commit_sha="<HOTFIX_COMMIT_SHA>"`, matching the SHA passed as `target_commit`) to run the full GKE E2E suite and ensure the fix validates cleanly on live infrastructure. Do not pass the tagged release commit: `tag_ga_release.sh` creates a stamped release commit on detached HEAD that lacks SHA-tagged container images in GHCR, causing the RC pipeline's image verification step to fail.
3. Attach the GitHub Actions run URL and emergency justification to the corresponding tracking issue or incident report.

## Clean promotion and artifact guarantees

The release publish workflow enforces byte-for-byte fidelity with tested candidate binaries across five layers:

1. Container images are compiled only once on push to `main`. `scripts/release/promote_release_images.sh` retags existing `<TARGET_COMMIT>` manifests to numeric `X.Y.Z` in GHCR using `docker buildx imagetools create`.
2. Promoted container images in GHCR are cryptographically signed using Keyless Cosign via GitHub Actions OIDC tokens (`scripts/release/sign_release_images.sh`).
3. `scripts/release/publish_helm_chart.sh` packages `charts/kube-agents` at version `X.Y.Z` (matching `appVersion`), pushes the OCI package to `oci://ghcr.io/gke-labs/kube-agents/charts/kube-agents:X.Y.Z`, and signs the OCI manifest via Cosign.
4. `scripts/release/tag_ga_release.sh` creates a single-parent release commit on detached HEAD, stamps `BAKED_RELEASE_VERSION="X.Y.Z"` into root scripts (`install.sh`, `uninstall.sh`, `upgrade.sh`), and tags the stamped commit.
5. `verify_local_source_ref` in `install.sh` verifies that unversioned source directories match `BAKED_RELEASE_VERSION` and that Git checkouts match the requested tag commit SHA, halting execution if local scripts diverge from container images.

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
