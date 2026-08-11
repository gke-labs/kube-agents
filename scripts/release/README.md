# Release Candidate Automation Scripts

This directory contains executable scripts supporting the Release Candidate (RC) end-to-end automation pipeline.

## Overview of Scripts

- `common.sh`: Centralized registry/repository helpers (`DEFAULT_REGISTRY_PREFIX`, `DEFAULT_RELEASE_REPO`, `REQUIRED_RELEASE_IMAGES`), commit discovery (`find_latest_built_commit`), validation check (`is_commit_already_validated`), and automated bot tagging (`ensure_git_tag`).
- `resolve_rc_tag.sh`: Validates candidate commit SHAs, resolves input tags/commit inputs, discovers the latest built commit on `main` during scheduled runs, checks for existing `*_validated` tags to skip redundant runs, and sets workflow step outputs.
- `verify_candidate_images.sh`: Verifies that prebuilt container images (`k8s-operator`, `platform-agent`) exist in GHCR/registry for the target candidate SHA.
- `create_release_tag.sh`: Creates and pushes candidate release tags (`rc_YYMMDDHHMM_<short_sha>`) safely and idempotently.
- `validate_and_log_deploy_summary.sh`: Validates required environment variables and secrets, then logs a formatted deployment matrix and GCP cluster target overview for auditing before provisioning.
- `provision_rc_environment.sh`: Orchestrates cluster teardown and fresh provisioning against the dedicated RC GCP project.
- `tag_validated_release.sh`: Attaches the `*_validated` tag to a candidate commit upon 100% test pass.

## Pipeline Cadence & Execution Flow

The end-to-end pipeline (`.github/workflows/rc-release-pipeline.yml`) runs on a recurring schedule and can also be triggered manually:

- **Scheduled Cadence (every 3 hours `0 */3 * * *`)**:
  - Automatically scans recent commits on `main` (`FETCH_HEAD`) for published container images in GHCR.
  - **Redundant Run Skipping**: If the latest candidate commit already carries a `*_validated` tag from a previous successful run, the pipeline skips subsequent provisioning and E2E test execution (`skip_rc=true`), finishing in seconds.
- **Manual Trigger (`workflow_dispatch`)**:
  - Requires an explicit `commit_sha` input to rigorously test a specific target commit.

## Workflow Mapping

These modular scripts back the corresponding child workflows in `.github/workflows/`:

| GitHub Workflow                                  | Release Step                            | Executed Scripts                                                                         |
| ------------------------------------------------ | --------------------------------------- | ---------------------------------------------------------------------------------------- |
| `rc-create-tag.yml`                              | Step 1 - Create Candidate Tag           | `resolve_rc_tag.sh`, `verify_candidate_images.sh`, `create_release_tag.sh`               |
| `rc-deploy-environment.yml`                      | Step 2 - Deploy Environment             | `resolve_rc_tag.sh`, `validate_and_log_deploy_summary.sh`, `provision_rc_environment.sh` |
| `e2e-gchat-test.yml` / `rc-release-pipeline.yml` | Step 3 - GKE Readiness & E2E Validation | `install_e2e_deps.sh`, `wait_for_gke_readiness.sh`, `execute_e2e_tests.sh`               |
| `rc-tag-validated.yml`                           | Step 4 - Validate Candidate Commit      | `resolve_rc_tag.sh`, `tag_validated_release.sh`                                          |
