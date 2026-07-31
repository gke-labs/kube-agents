# Release Candidate Automation Scripts

This directory contains executable scripts supporting the Release Candidate (RC) end-to-end automation pipeline.

## Overview of Scripts

- `common.sh`: Shared helper functions for Git operations and automated bot tagging (`ensure_git_tag`).
- `resolve_rc_tag.sh`: Validates candidate commit SHAs, resolves input tags/commit inputs, and sets workflow step outputs.
- `verify_candidate_images.sh`: Verifies that prebuilt container images (`k8s-operator`, `platform-agent`) exist in GHCR for the target candidate SHA.
- `create_release_tag.sh`: Creates and pushes candidate release tags (`rc_YYMMDDHHMM_<short_sha>`) safely and idempotently.
- `validate_and_log_deploy_summary.sh`: Validates required environment variables and secrets, then logs a formatted deployment matrix and GCP cluster target overview for auditing before provisioning.
- `provision_rc_environment.sh`: Orchestrates cluster teardown and fresh provisioning against the dedicated RC GCP project.
- `tag_validated_release.sh`: Attaches the `*_validated` tag to a candidate commit upon 100% test pass.

## Workflow Mapping

These modular scripts back the corresponding child workflows in `.github/workflows/`:

| GitHub Workflow                                  | Release Step                            | Executed Scripts                                                                         |
| ------------------------------------------------ | --------------------------------------- | ---------------------------------------------------------------------------------------- |
| `rc-create-tag.yml`                              | Step 1 - Create Candidate Tag           | `resolve_rc_tag.sh`, `verify_candidate_images.sh`, `create_release_tag.sh`               |
| `rc-deploy-environment.yml`                      | Step 2 - Deploy Environment             | `resolve_rc_tag.sh`, `validate_and_log_deploy_summary.sh`, `provision_rc_environment.sh` |
| `e2e-gchat-test.yml` / `rc-release-pipeline.yml` | Step 3 - GKE Readiness & E2E Validation | `install_e2e_deps.sh`, `wait_for_gke_readiness.sh`, `execute_e2e_tests.sh`               |
| `rc-tag-validated.yml`                           | Step 4 - Validate Candidate Commit      | `resolve_rc_tag.sh`, `tag_validated_release.sh`                                          |
