---
name: install-kube-agents
description: Provision and install the Kubernetes Agentic Harness (kube-agents) onto a GKE cluster non-interactively or interactively.
---

# `install-kube-agents` Skill

This skill provides step-by-step instructions for AI Agents to non-interactively provision Google Cloud GKE infrastructure and deploy the `kube-agents` Platform Agent.

## What `install.sh` actually does

It is a front-end, not a second provisioner. It collects configuration, writes
`k8s-operator/scripts/vars.sh` (the install's machine-readable record), generates
`terraform/examples/full-install/terraform.tfvars` from it, and then runs the composition's
`lifecycle.sh apply` — the Terraform root in
[`terraform/examples/full-install/`](../../../terraform/examples/full-install/README.md) owns every
GCP resource and installs the Helm chart (`charts/kube-agents`) that owns every Kubernetes one.
Terraform state goes to a GCS bucket (`<project>-kube-agents-tfstate`, versioned, prefix
`kube-agents/<cluster>`), so `uninstall.sh` and `upgrade.sh` can find the install from a fresh
clone. The installer sources
[`k8s-operator/scripts/installer_common.sh`](../../../k8s-operator/scripts/README.md) before its
first prompt, so its defaults and accepted values are the ones defined there; that file is where a
default changes.

Order of operations: resolve the image/source ref → check CLI prerequisites (including
`terraform`, which it offers to install; `make` is not needed) → put the repository on disk and
verify it against that ref → interview → write `vars.sh` and generate `terraform.tfvars` → run
`lifecycle.sh apply`. The source check happens **before** the interview, so a bad ref fails in
seconds rather than after a dozen answers. Two steps stay `gcloud` calls after the apply — the
managed-OTel scope and CMEK on a pre-existing cluster — and the GitHub App PEM import runs through
the Minty CLI so the key never enters Terraform state. Re-running the installer (or its `--menu`
Day-2 panel's Save & Apply) reconciles every change through one `terraform apply`.

## Quick Execution for AI Agents

To run the installer non-interactively in automated subagent execution, pass `--non-interactive` along with explicit configuration flags:

```bash
curl -fsSL https://raw.githubusercontent.com/gke-labs/kube-agents/main/install.sh | bash -s -- \
  --non-interactive \
  --project-id="YOUR_GCP_PROJECT_ID" \
  --cluster-name="platform-agent-host" \
  --region="us-central1" \
  --image-tag="<SEMVER_TAG_OR_FULL_COMMIT_SHA>" \
  --model-provider="gemini" \
  --permission-set="read-only"
```

`--image-tag` accepts a SemVer release tag or a full 40-character commit SHA; mutable refs
(`latest`, `main`, `master`, `HEAD`) are rejected. When the installer runs from a kube-agents
checkout it defaults to that checkout's `HEAD`; anywhere else — including the `curl | bash` path —
the flag is required. Pass it explicitly unless a container image exists for that exact commit: CI
publishes one per `main` commit and per release tag, so an unmerged local commit will pass
validation and then fail at image pull.

## Dry-Run Inspection

To validate prerequisites and preview the install without creating GCP resources, use `--dry-run`.
It always runs `terraform validate` against the generated configuration, and adds a full
`terraform plan` when Application Default Credentials exist — on local state, so it never creates
the state bucket:

```bash
./install.sh --dry-run --non-interactive \
  --project-id="YOUR_GCP_PROJECT_ID" \
  --image-tag="<SEMVER_TAG_OR_FULL_COMMIT_SHA>"
```

A dry run still overwrites `k8s-operator/scripts/vars.sh` (and `terraform.tfvars`). Back it up
first if a real deployment's state is already there.

## Source verification

Before provisioning, the installer requires the checkout holding the Terraform configuration and
chart to be at the same commit as `--image-tag` and to have no uncommitted changes — the install
sources and the container image must come from one revision. A dirty or mismatched checkout aborts with instructions.
`--allow-unverified-source` (or `ALLOW_UNVERIFIED_SOURCE=true`) downgrades that to a warning; use it
when iterating on the installer itself, not for a deployment you intend to keep. `--dry-run` is
lenient already.

## GCP IAM permission sets

`--permission-set` chooses which GCP IAM role bundle the composition grants the agent's GSA (its
`permission_set` variable; `custom` becomes a `project_roles` list). It does **not** affect
Kubernetes RBAC, which is read-only in every set, and it does not gate the GitOps pull-request
path, which works in every set. See the site's
[security and IAM reference](../../../docs/site/src/content/docs/reference/security-and-iam.md).

| Set         | Grants                                                            |
| ----------- | ----------------------------------------------------------------- |
| `read-only` | Viewer roles only — no GCP write capability. **Default.**         |
| `custom`    | Exactly the roles passed in `--custom-roles`; no built-in bundle. |

## Machine-Readable Results

Upon completion, `install.sh` generates a machine-readable JSON status report at `/tmp/kube-agents-install-report.json`:

```json
{
  "status": "SUCCESS",
  "dry_run": false,
  "non_interactive": true,
  "project_id": "YOUR_GCP_PROJECT_ID",
  "cluster_name": "platform-agent-host",
  "timestamp": "2026-08-05T03:35:00Z"
}
```

## Supported Command-Line Flags

Defaults marked "`installer_common.sh`" come from `k8s-operator/scripts/installer_common.sh` and
are listed there, not here. Run `./install.sh --help` for the authoritative list.

| Flag                                 | Description                                                                                                                                            | Default                                         |
| :----------------------------------- | :----------------------------------------------------------------------------------------------------------------------------------------------------- | :---------------------------------------------- |
| `-y, --non-interactive`              | Run without blocking on `/dev/tty` prompts                                                                                                             | `false`                                         |
| `--dry-run`                          | Output plan and `vars.sh` without creating resources                                                                                                   | `false`                                         |
| `--menu, --config`                   | Launch the Day-2 control panel instead of installing                                                                                                   | `false`                                         |
| `--project-id=ID`                    | Target GCP Project ID                                                                                                                                  | Active `gcloud` project                         |
| `--region=REGION`                    | Target GCP Region                                                                                                                                      | `installer_common.sh` `DEFAULT_REGION`          |
| `--cluster-name=NAME`                | GKE Cluster Name                                                                                                                                       | `installer_common.sh` `DEFAULT_CLUSTER_NAME`    |
| `--cluster-mode=MODE`                | Shape of a cluster this run creates: `autopilot` \| `standard`. No bearing on an existing cluster, whose live shape the generator probes               | `standard`                                      |
| `--image-tag=TAG`                    | SemVer release tag or full 40-character commit SHA                                                                                                     | Checkout `HEAD`; required via `curl \| bash`    |
| `--registry-prefix=PATH`             | Registry path (no URL scheme) for the four images this project builds                                                                                  | `installer_common.sh` `DEFAULT_REGISTRY_PREFIX` |
| `--third-party-registry-prefix=PATH` | Registry path holding the mirrored third-party images (cert-manager, LiteLLM, fluent-bit, token minter, Hindsight). Not implied by `--registry-prefix` | _unset_ — upstream registries                   |
| `--allow-unverified-source`          | Provision from a dirty or mismatched checkout                                                                                                          | `false`                                         |
| `--model-provider=NAME`              | `gemini` \| `vertex_ai` \| `anthropic` \| `openai`                                                                                                     | `installer_common.sh` `DEFAULT_MODEL_PROVIDER`  |
| `--vertex-location=LOCATION`         | Vertex AI serving location, a region or `global`. The global endpoint gives no in-region ML processing guarantee                                       | `installer_common.sh` `DEFAULT_VERTEX_LOCATION` |
| `--gemini-api-key=KEY`               | Gemini API key                                                                                                                                         | Looked up in Secret Manager                     |
| `--openai-api-key=KEY`               | OpenAI API key                                                                                                                                         | _unset_                                         |
| `--anthropic-api-key=KEY`            | Anthropic API key                                                                                                                                      | _unset_                                         |
| `--permission-set=SET`               | Agent GCP IAM set: `read-only` \| `custom`                                                                                                             | `read-only`                                     |
| `--custom-roles=ROLES`               | Roles for `--permission-set=custom` (space- or comma-separated)                                                                                        | _unset_                                         |
| `--gitops-org=ORG`                   | GitHub org/user for the GitOps IaC repository                                                                                                          | _unset_                                         |
| `--gitops-repo=REPO`                 | GitOps IaC repository name                                                                                                                             | `gke-fleet-iac`                                 |
| `--enable-google-chat`               | Enable the Google Chat integration                                                                                                                     | `false`                                         |
| `--gvisor=true\|false`               | Enable GKE Sandbox (gVisor) runtime isolation                                                                                                          | `false`                                         |
| `--enable-web-ui=true\|false`        | Enable the Hermes Web UI on port 9119                                                                                                                  | `false`                                         |
| `-h, --help, -?`                     | Output CLI usage banner and parameter details                                                                                                          | `N/A`                                           |
