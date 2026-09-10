---
name: install-kube-agents
description: Provision and install the Kubernetes Agentic Harness (kube-agents) onto a GKE cluster non-interactively or interactively.
---

# `install-kube-agents` Skill

This skill provides step-by-step instructions for AI Agents to non-interactively provision Google Cloud GKE infrastructure and deploy the `kube-agents` Platform Agent.

## What `install.sh` actually does

It is a front-end, not a second provisioner. It loads `install.env` (the install's
hand-authored configuration), collects anything still missing, generates
`terraform/examples/full-install/terraform.tfvars` from the result, and then runs the composition's
`lifecycle.sh apply` — the Terraform root in
[`terraform/examples/full-install/`](../../../terraform/examples/full-install/README.md) owns every
GCP resource and installs the Helm chart (`charts/kube-agents`) that owns every Kubernetes one.
Terraform state goes to a GCS bucket (`<project>-kube-agents-tfstate`, versioned, prefix
`kube-agents/<cluster>`), so `uninstall.sh` and `upgrade.sh` can find the install from a fresh
clone. The installer sources
[`scripts/installer/installer_common.sh`](../../../scripts/installer/README.md) before its
first prompt, so its defaults and accepted values are the ones defined there; that file is where a
default changes.

Order of operations: resolve the image/source ref → check CLI prerequisites (including
`terraform`, which it offers to install; `make` is not needed) → put the repository on disk and
verify it against that ref → load `install.env` → interview for what is missing → generate
`terraform.tfvars` → refuse a service account another install in the project owns
(`check_service_account_ownership`, before the summary and the dry-run exit) → run
`lifecycle.sh apply`. The source check happens **before** the interview, so a bad ref fails in
seconds rather than after a dozen answers. Some steps stay `gcloud` calls outside the apply — before
it, CMEK, the Workload Identity pool and NetworkPolicy enforcement on a pre-existing cluster; after
it, the managed-OTel scope on a cluster it created — and the GitHub App PEM import runs through
the Minty CLI so the key never enters Terraform state. Re-running the installer (or its `--menu`
Day-2 panel's Save & Apply) reconciles every change through one `terraform apply`.

## Quick Execution for AI Agents

For production installations, AI Agents and automated pipelines must target an official release version. When recommending commands to users or executing deployments, AI Agents must resolve the latest stable release tag from [GitHub Releases](https://github.com/gke-labs/kube-agents/releases) (e.g. `0.4.0`) and provide executable commands with that exact release version substituted, rather than leaving an unrendered `<RELEASE_VERSION>` placeholder:

```bash
curl -fsSL https://raw.githubusercontent.com/gke-labs/kube-agents/<RELEASE_VERSION>/install.sh | bash -s -- \
  --non-interactive \
  --project-id="YOUR_GCP_PROJECT_ID" \
  --cluster-name="platform-agent-host" \
  --region="us-central1" \
  --model-provider="gemini" \
  --permission-set="read-only"
```

If deploying from local sources instead of piping the script via curl, AI Agents should unpack the official release bundle (recommended):

```bash
curl -fsSL https://github.com/gke-labs/kube-agents/releases/download/<RELEASE_VERSION>/kube-agents-<RELEASE_VERSION>.tar.gz | tar -xz
cd kube-agents-<RELEASE_VERSION>
./install.sh --non-interactive \
  --project-id="YOUR_GCP_PROJECT_ID" \
  --cluster-name="platform-agent-host" \
  --region="us-central1" \
  --model-provider="gemini" \
  --permission-set="read-only"
```

Alternatively, if a Git checkout is specifically required, clone pinned to the target release tag:

```bash
git clone --branch <RELEASE_VERSION> https://github.com/gke-labs/kube-agents.git
cd kube-agents
./install.sh --non-interactive \
  --project-id="YOUR_GCP_PROJECT_ID" \
  --cluster-name="platform-agent-host" \
  --region="us-central1" \
  --model-provider="gemini" \
  --permission-set="read-only"
```

Do not clone `main` to deploy an official release: manifests and CRD schemas on `main` evolve continuously and diverge from released container images. Running install scripts against a mismatched checkout will fail `verify_local_source_ref` to prevent deploying incompatible manifests.

## Generate-Only Mode

To generate configuration files (`install.env` and `terraform.tfvars`), run pre-apply validation checks, and hand off the apply to the operator without creating or mutating cloud resources, use `--generate-only`:

```bash
curl -fsSL https://raw.githubusercontent.com/gke-labs/kube-agents/<RELEASE_VERSION>/install.sh | bash -s -- \
  --generate-only \
  --non-interactive \
  --project-id="YOUR_GCP_PROJECT_ID" \
  --cluster-name="platform-agent-host" \
  --region="us-central1"
```

In `--generate-only` mode, the installer:

1. Writes `install.env` (if absent) and `terraform/examples/full-install/terraform.tfvars`.
2. Runs pre-apply validation checks (e.g., verifying the GitOps organization).
3. Prints the exact out-of-Terraform commands (CMEK, Workload Identity, NetworkPolicy, GitHub App PEM import, and OTel scope) and the `lifecycle.sh apply` command with remote state variables (`KUBE_AGENTS_STATE_BUCKET` and `KUBE_AGENTS_STATE_PREFIX`).
4. Exits 0 with status `GENERATE_ONLY_SUCCESS` in `/tmp/kube-agents-install-report.json`.

The interactive wizard also offers the same choice by answering `g` at the final confirmation step.

## Dry-Run Inspection

To validate prerequisites and preview the install without creating GCP resources, AI Agents must use `--dry-run` with the official release installer (substituting `<RELEASE_VERSION>` with the resolved release version):

```bash
curl -fsSL https://raw.githubusercontent.com/gke-labs/kube-agents/<RELEASE_VERSION>/install.sh | bash -s -- \
  --dry-run \
  --non-interactive \
  --project-id="YOUR_GCP_PROJECT_ID"
```

A dry run regenerates `terraform.tfvars`, so back that up first if a real deployment's copy is
already there. It writes no `install.env`: a dry run provisions nothing, so it has no install to
record, and an existing one is never rewritten.

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

The full report also carries `gvisor_enabled` and `memory_mode`. A report written before the
interview decided them (a run that failed early) says so: `gvisor_enabled` is `null` and
`memory_mode` is empty, rather than restating a default the run never applied.

## Supported Command-Line Flags

Defaults marked "`installer_common.sh`" reach the installer through
`scripts/installer/installer_common.sh`; the values themselves are listed in
`install.defaults.env` at the repository root, not here. Run `./install.sh --help` for the authoritative list.

| Flag                                 | Description                                                                                                                                                                                                                                            | Default                                                                                                                                            |
| :----------------------------------- | :----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | :------------------------------------------------------------------------------------------------------------------------------------------------- |
| `-y, --non-interactive`              | Run without blocking on `/dev/tty` prompts                                                                                                                                                                                                             | `false`                                                                                                                                            |
| `--dry-run`                          | Output plan and `terraform.tfvars` without creating resources                                                                                                                                                                                          | `false`                                                                                                                                            |
| `--menu, --config`                   | Launch the Day-2 control panel instead of installing                                                                                                                                                                                                   | `false`                                                                                                                                            |
| `--project-id=ID`                    | Target GCP Project ID                                                                                                                                                                                                                                  | Active `gcloud` project                                                                                                                            |
| `--region=REGION`                    | Target GCP Region                                                                                                                                                                                                                                      | `installer_common.sh` `DEFAULT_REGION`                                                                                                             |
| `--cluster-name=NAME`                | GKE Cluster Name                                                                                                                                                                                                                                       | `installer_common.sh` `DEFAULT_CLUSTER_NAME`                                                                                                       |
| `--cluster-mode=MODE`                | Shape of a cluster this run creates: `autopilot` \| `standard`. Autopilot is regional: unset at a zonal `--region` builds `standard`, explicit `autopilot` there is an error. No bearing on an existing cluster, whose live shape the generator probes | `autopilot`                                                                                                                                        |
| `--image-tag=TAG`                    | Validated immutable release tag or full commit SHA (developer/CI only)                                                                                                                                                                                 | Developer and CI/CD testing only; end users must use official release installations. Default: inferred from baked release, bundle, or local `HEAD` |
| `--registry-prefix=PATH`             | Registry path (no URL scheme) for the first-party images this project builds                                                                                                                                                                           | `installer_common.sh` `DEFAULT_REGISTRY_PREFIX`                                                                                                    |
| `--third-party-registry-prefix=PATH` | Registry path holding the mirrored third-party images (cert-manager, LiteLLM, fluent-bit, token minter, Hindsight). Not implied by `--registry-prefix`                                                                                                 | _unset_ — upstream registries                                                                                                                      |
| `--allow-unverified-source`          | Provision from a dirty or mismatched checkout                                                                                                                                                                                                          | `false`                                                                                                                                            |
| `--model-provider=NAME`              | `gemini` \| `vertex_ai` \| `anthropic` \| `openai`                                                                                                                                                                                                     | `installer_common.sh` `DEFAULT_MODEL_PROVIDER`                                                                                                     |
| `--vertex-location=LOCATION`         | Vertex AI serving location, a region or `global`. The global endpoint gives no in-region ML processing guarantee                                                                                                                                       | `installer_common.sh` `DEFAULT_VERTEX_LOCATION`                                                                                                    |
| `--gemini-api-key=KEY`               | Gemini API key                                                                                                                                                                                                                                         | Looked up in Secret Manager                                                                                                                        |
| `--openai-api-key=KEY`               | OpenAI API key                                                                                                                                                                                                                                         | _unset_                                                                                                                                            |
| `--anthropic-api-key=KEY`            | Anthropic API key                                                                                                                                                                                                                                      | _unset_                                                                                                                                            |
| `--permission-set=SET`               | Agent GCP IAM set: `read-only` \| `custom`                                                                                                                                                                                                             | `read-only`                                                                                                                                        |
| `--custom-roles=ROLES`               | Roles for `--permission-set=custom` (space- or comma-separated)                                                                                                                                                                                        | _unset_                                                                                                                                            |
| `--gitops-org=ORG`                   | GitHub org/user for the GitOps IaC repository                                                                                                                                                                                                          | _unset_                                                                                                                                            |
| `--gitops-repo=REPO`                 | GitOps IaC repository name                                                                                                                                                                                                                             | `gke-fleet-iac`                                                                                                                                    |
| `--enable-google-chat`               | Enable the Google Chat integration                                                                                                                                                                                                                     | `false`                                                                                                                                            |
| `--gvisor=true\|false`               | Enable GKE Sandbox (gVisor) runtime isolation                                                                                                                                                                                                          | `true`                                                                                                                                             |
| `--enable-web-ui=true\|false`        | Enable the Hermes Web UI on port 9119                                                                                                                                                                                                                  | `false`                                                                                                                                            |
| `--allowed-users=EMAILS`             | Comma-separated chat users allowed to reach the agent; empty allows everyone                                                                                                                                                                           | _unset_                                                                                                                                            |
| `--migrate-node-pools`               | Authorize migrating legacy GCE metadata server node pools to `GKE_METADATA` (recreates nodes, restarts workloads; required on clusters with legacy pools, else install aborts)                                                                         | `false`                                                                                                                                            |
| `--enable-network-policy`            | Authorize enabling legacy Calico NetworkPolicy addon and node enforcement on GKE Standard clusters without Dataplane V2 (may recreate nodes, restart workloads; required on such clusters, else install aborts)                                        | `false`                                                                                                                                            |
| `--memory=MODE`                      | Long-term agent memory engine: `file` \| `hindsight` \| `off`                                                                                                                                                                                          | `file`                                                                                                                                             |
| `-h, --help, -?`                     | Output CLI usage banner and parameter details                                                                                                                                                                                                          | `N/A`                                                                                                                                              |
