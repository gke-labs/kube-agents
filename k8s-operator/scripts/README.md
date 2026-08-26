# Installer Helper Scripts

The install engine is Terraform + Helm: `terraform/examples/full-install` driven through
its `lifecycle.sh`, with the repository-root `install.sh` / `uninstall.sh` / `upgrade.sh`
as the front doors. This directory holds the helpers those front doors (and the dev
tooling) share.

## Shared defaults live in `installer_common.sh`

`installer_common.sh` is the single home for the values every installer front-end must
agree on. `install.sh`, `uninstall.sh`, and `upgrade.sh` source it rather than keeping
their own copies:

| Symbol                                  | What it fixes                                                        |
| --------------------------------------- | -------------------------------------------------------------------- |
| `DEFAULT_CLUSTER_NAME`                  | GKE cluster name (`platform-agent-host`)                             |
| `DEFAULT_REGION`                        | GCP region (`us-central1`)                                           |
| `DEFAULT_VERTEX_LOCATION`               | Vertex AI serving location (`global`)                                |
| `DEFAULT_MODEL_PROVIDER`                | Model provider (`gemini`)                                            |
| `DEFAULT_REGISTRY_PREFIX`               | Container registry prefix                                            |
| `default_model_for_provider <provider>` | The default model for a provider                                     |
| `is_valid_model_provider <provider>`    | Accepted providers: `gemini`, `vertex_ai`, `anthropic`, `openai`     |
| `is_valid_permission_set <set>`         | Accepted GCP IAM permission sets: `read-only`, `gke-admin`, `custom` |
| `is_valid_cluster_mode <mode>`          | Accepted cluster shapes: `autopilot`, `standard`                     |
| `derive_kms_location <region>`          | Region for Cloud KMS (strips a zone suffix)                          |
| `tf_state_bucket` / `tf_state_prefix`   | Where the install's Terraform state lives in GCS                     |
| `write_tfvars_from_state <dest> [tag]`  | The `terraform.tfvars` generator (reads the `vars.sh` variable set)  |

Change a default here and every front door follows. Do **not** restate these values in
`install.sh`, in a chart, or in prose — link to this table instead.

## The state file: `vars.sh`

`vars.sh` (git-ignored, `chmod 600`) is the install's machine-readable record, written by
`install.sh` and read by the Day-2 menu, `uninstall.sh`, `upgrade.sh`, the admin console,
the e2e tests, and `scripts/live_test_lease.py` — which, being run by a hook on a
contributor's machine, matches an allowlist of `export K=V` lines with a regex and never
sources the file. The `terraform.tfvars` the engine consumes is generated from it on
every run, so the two cannot disagree. Set `PERSIST_SECRETS_ON_DISK=false` to keep
credentials out of both files: the generator omits them from `terraform.tfvars` and
exports them as `TF_VAR_*` for the apply instead, and later runs recover them from the
live `platform-agent-secrets` Secret (only when kubectl's current context is this
install's cluster). `SKIP_CERT_MANAGER=true` makes the generator emit
`enable_cert_manager = false`, for a cluster whose cert-manager comes from somewhere
else.

`CLUSTER_MODE` (`autopilot` or `standard`, from `install.sh --cluster-mode`) is the one
key whose saved value the generator will discard. It supplies the shape of a cluster that
does not exist yet, and nothing else: whenever the liveness probe finds a cluster, that
cluster's own shape is written instead, and `install.sh` writes that shape back to
`vars.sh` so the record and the cluster stay in step. `uninstall.sh` and
`upgrade.sh` regenerate `terraform.tfvars` from `vars.sh` with no flag to correct it
with, and a `CLUSTER_MODE` that disagreed with the live cluster would take that
cluster's resource count to 0 — turning the next apply into a replacement.

## File directory

- **[installer_common.sh](installer_common.sh)**: shared defaults, validators, `vars.sh`
  persistence, GitHub org checks, and the `terraform.tfvars` generator (table above).
- **[common.sh](common.sh)**: utilities the dev tooling and the Prow CI scripts
  (`hack/ci-deploy.sh`) use — colour output, `init_var`/`load_state`,
  registry and third-party-image resolution, cluster connection helpers. Sources
  `installer_common.sh`, so nothing is defined twice.
- **[gke_dns_endpoint.sh](gke_dns_endpoint.sh)**: `gke_dns_endpoint_flag`, which decides whether a given cluster should be reached with `get-credentials --dns-endpoint`. Kept out of `common.sh` and free of its helpers so `hack/ci-env.sh`, `scripts/release/common.sh`, `upgrade.sh`, and the staging-workload scripts can source the one predicate without also taking on the state file. It sets `GKE_DNS_ENDPOINT_FLAG` rather than echoing, so that callers do not run it in a `$(...)` subshell that would discard its memo of whether the local gcloud offers the flag at all. That answer leaves it empty — as do a cluster with no externally reachable DNS endpoint and a describe call that fails — leaving today's IP-endpoint command untouched.
- **[min_versions.sh](min_versions.sh)**: minimum tool versions, side-effect-free so
  `install.sh` can source it standalone before any checkout exists.
- **[update_cluster_name.sh](update_cluster_name.sh)**: patches the target GKE cluster
  name into the deployed `PlatformAgent` spec, triggering the operator to reconcile.
- **[print_instructions_gchat.sh](print_instructions_gchat.sh)** /
  **[print_instructions_slack.sh](print_instructions_slack.sh)**: post-install manual-step
  instructions, printed by `install.sh` when the integration is enabled.
- **[dev/dev_rebuild_agent.sh](dev/dev_rebuild_agent.sh)**: fast local development utility
  that builds, pushes, and redeploys agent container images.
