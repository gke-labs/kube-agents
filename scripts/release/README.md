# Release Candidate Automation Scripts

This directory contains executable scripts supporting the Release Candidate (RC) end-to-end automation pipeline.

## Overview of Scripts

- `common.sh`: Centralized registry/repository helpers (`DEFAULT_REGISTRY_PREFIX`, `DEFAULT_RELEASE_REPO`, `REQUIRED_RELEASE_IMAGES`), commit discovery (`find_latest_built_commit`), validation check (`is_commit_already_validated`), container image promotion (`promote_release_images`), and automated bot tagging (`ensure_git_tag`).
- `resolve_rc_tag.sh`: Validates candidate commit SHAs, resolves input tags/commit inputs, discovers the latest built commit on `main` during scheduled runs, checks for existing `*_validated` tags to skip redundant runs, and sets workflow step outputs.
- `verify_candidate_images.sh`: Verifies that prebuilt container images (`k8s-operator`, `platform-agent`, `credential-proxy`, `replay-proxy`) exist in GHCR/registry for the target candidate SHA.
- `create_release_tag.sh`: Creates and pushes candidate release tags (`rc_YYMMDDHHMM_<short_sha>`, derived from commit timestamp) safely and idempotently. When executed locally outside CI, runs in dry-run mode (creates tag locally and skips remote push).
- `validate_and_log_deploy_summary.sh`: Validates required environment variables and secrets, then logs a formatted deployment matrix and GCP cluster target overview for auditing before provisioning.
- `rc_teardown_common.sh`: Sourced by the two scripts below, which both call `uninstall.sh` and read the same three outcomes out of its exit code (`./uninstall.sh --help` lists them). Holds the invocation, the `RC_TEARDOWN_STRICT` parsing, and the job-summary rendering; each caller decides for itself what a failure means.
- `provision_rc_environment.sh`: Tears the RC environment down with `uninstall.sh`, then reinstalls it at the candidate commit with `install.sh`, against the dedicated RC GCP project. A failed teardown raises an `::error` annotation and a job-summary entry carrying the teardown output, and provisions anyway unless `RC_TEARDOWN_STRICT` is truthy — the choice between validating a candidate against stale state and letting a teardown problem block every release.
- `teardown_rc_environment.sh`: Destroys the RC environment after a run that passed end to end, so the cluster exists only for the length of a run rather than idling between the 3-hourly ones. A failure here is always fatal and `RC_TEARDOWN_STRICT` does not apply: nothing runs afterwards, so the alternative to a red job is a GKE cluster billing under a green pipeline. It runs only when steps 1–4 all succeeded, which is what leaves a failed run's environment standing to be examined live.
- `tag_validated_release.sh`: Attaches the `*_validated` tag to a candidate commit upon 100% test pass.
- `calculate_next_version.sh`: Automatically calculates the next SemVer 2.0 version from Conventional Commits since the latest numeric GA release tag.
- `verify_release_eligibility.sh`: Release gatekeeper that verifies commit eligibility, checks for live RC validation tags (`rc_*_validated`), performs tag collision detection, and verifies all 4 required container images exist in registry.
- `tag_ga_release.sh`: Creates and pushes official GA SemVer Git tags (`X.Y.Z`) directly on the validated commit SHA.
- `promote_release_images.sh`: Promotes verified container images from candidate commit SHA to GA release tag in GHCR without rebuilding.
- `sign_release_images.sh`: Signs promoted GA release container images in GHCR using Keyless Cosign OIDC.
- `publish_helm_chart.sh`: Packages, publishes, and signs the official kube-agents Helm chart to GHCR as an OCI artifact.
- `publish_github_release.sh`: Publishes official GitHub Releases with auto-generated release notes from Conventional Commits.

## Pipeline Cadence & Execution Flow

The end-to-end pipeline (`.github/workflows/rc-release-pipeline.yml`) runs on a recurring schedule and can also be triggered manually:

- **Scheduled Cadence (every 3 hours `17 */3 * * *`, best-effort)**:
  - Automatically scans recent commits on `main` (`FETCH_HEAD`) for published container images in GHCR.
  - **Redundant Run Skipping**: If the latest candidate commit already carries a `*_validated` tag or was previously attempted, the pipeline skips subsequent provisioning and E2E test execution (`skip_rc=true`), finishing in seconds.
  - _Note_: Scheduled runs are scheduled at minute `17` to avoid GitHub Actions peak top-of-the-hour queue congestion; actual start times are best-effort based on GitHub scheduler availability.
- **Manual Trigger (`workflow_dispatch`)**:
  - Requires an explicit `commit_sha` input to rigorously test a specific target commit.

## What Happens to the RC Cluster

The pipeline builds a full GKE cluster per candidate and destroys it twice over: step 2 removes whatever was there before it installs, and step 5 removes what the run itself built. A run that passes therefore leaves nothing behind and nothing billing.

A run that fails anywhere does leave its environment standing, deliberately — step 5 hangs off the success of every earlier job, and the E2E failures worth diagnosing are the ones that only reproduce on the cluster that produced them. Two consequences to know about:

- Nothing else removes that environment. The next run's step 2 does, which on the schedule is up to three hours later, so an investigation that needs longer than that wants the schedule paused rather than a race against it.
- Step 2 is the only thing standing between a surviving environment and a candidate validated against stale state, which is what `RC_TEARDOWN_STRICT` decides. Truthy stops the run instead of installing on top; the same failure in step 5 is fatal regardless, because no later step compensates for it. Set it on the `rc` environment, where `GCP_PROJECT_ID` and every other value these jobs read already live — the repository level holds none of them, and `vars` resolving environment over repository makes a stray repository-level copy easy to set and then not find again.

## Workflow Mapping

These modular scripts back the corresponding child workflows in `.github/workflows/`:

| GitHub Workflow                                  | Release Step                            | Executed Scripts                                                                                                                                                                               |
| ------------------------------------------------ | --------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `rc-create-tag.yml`                              | Step 1 - Create Candidate Tag           | `resolve_rc_tag.sh`, `verify_candidate_images.sh`, `create_release_tag.sh`                                                                                                                     |
| `rc-deploy-environment.yml`                      | Step 2 - Deploy Environment             | `resolve_rc_tag.sh`, `validate_and_log_deploy_summary.sh`, `provision_rc_environment.sh`                                                                                                       |
| `e2e-gchat-test.yml` / `rc-release-pipeline.yml` | Step 3 - GKE Readiness & E2E Validation | `install_e2e_deps.sh`, `wait_for_gke_readiness.sh`, `execute_e2e_tests.sh`                                                                                                                     |
| `rc-tag-validated.yml`                           | Step 4 - Validate Candidate Commit      | `resolve_rc_tag.sh`, `tag_validated_release.sh`                                                                                                                                                |
| `rc-teardown-environment.yml`                    | Step 5 - Tear Down Environment          | `resolve_rc_tag.sh`, `teardown_rc_environment.sh`                                                                                                                                              |
| `release-publish.yml`                            | GA Release Orchestration                | `calculate_next_version.sh`, `verify_release_eligibility.sh`, `promote_release_images.sh`, `sign_release_images.sh`, `tag_ga_release.sh`, `publish_helm_chart.sh`, `publish_github_release.sh` |
