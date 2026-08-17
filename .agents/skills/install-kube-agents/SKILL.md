---
name: install-kube-agents
description: Provision and install the Kubernetes Agentic Harness (kube-agents) onto a GKE cluster non-interactively or interactively.
---

# `install-kube-agents` Skill

This skill provides step-by-step instructions for AI Agents to non-interactively provision Google Cloud GKE infrastructure and deploy the `kube-agents` Platform Agent.

## What `install.sh` actually does

It is a front-end, not a second provisioner. It collects configuration, writes
`k8s-operator/scripts/vars.sh`, and then runs `make gcp-provision` — the pipeline in
[`k8s-operator/scripts/`](../../../k8s-operator/scripts/README.md) does every GCP and GKE
operation. The installer sources `k8s-operator/scripts/common.sh` before its first prompt, so its
defaults and accepted values are the ones defined there; that file is where a default changes.

Order of operations: resolve the image/source ref → check CLI prerequisites → put the provisioning
scripts on disk and verify them against that ref → interview → write `vars.sh` → run the pipeline.
The source check happens **before** the interview, so a bad ref fails in seconds rather than after
a dozen answers.

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

To validate prerequisites and generate configuration state (`vars.sh`) without creating GCP resources, use `--dry-run`:

```bash
./install.sh --dry-run --non-interactive \
  --project-id="YOUR_GCP_PROJECT_ID" \
  --image-tag="<SEMVER_TAG_OR_FULL_COMMIT_SHA>"
```

A dry run overwrites `k8s-operator/scripts/vars.sh`. Back it up first if a real deployment's state
is already there.

## Source verification

Before provisioning, the installer requires the checkout holding the provisioning scripts to be at
the same commit as `--image-tag` and to have no uncommitted changes — the scripts and the container
image must come from one revision. A dirty or mismatched checkout aborts with instructions.
`--allow-unverified-source` (or `ALLOW_UNVERIFIED_SOURCE=true`) downgrades that to a warning; use it
when iterating on the installer itself, not for a deployment you intend to keep. `--dry-run` is
lenient already.

## GCP IAM permission sets

`--permission-set` chooses which GCP IAM role bundle `provision_04_gcp_iam.sh` grants the agent's
GSA. It does **not** affect Kubernetes RBAC, which is read-only in every set, and it does not gate
the GitOps pull-request path, which works in every set. See the site's
[security and IAM reference](../../../docs/site/src/content/docs/reference/security-and-iam.md).

| Set         | Grants                                                             |
| ----------- | ------------------------------------------------------------------ |
| `read-only` | Viewer roles only — no GCP write capability. **Default.**          |
| `gke-admin` | `container.clusterAdmin`, `container.admin`, `monitoring.admin`, … |
| `custom`    | Exactly the roles passed in `--custom-roles`; no built-in bundle.  |

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

Defaults marked "`common.sh`" come from `k8s-operator/scripts/common.sh` and are listed there, not
here. Run `./install.sh --help` for the authoritative list.

| Flag                          | Description                                                     | Default                                      |
| :---------------------------- | :-------------------------------------------------------------- | :------------------------------------------- |
| `-y, --non-interactive`       | Run without blocking on `/dev/tty` prompts                      | `false`                                      |
| `--dry-run`                   | Output plan and `vars.sh` without creating resources            | `false`                                      |
| `--menu, --config`            | Launch the Day-2 control panel instead of installing            | `false`                                      |
| `--project-id=ID`             | Target GCP Project ID                                           | Active `gcloud` project                      |
| `--region=REGION`             | Target GCP Region                                               | `common.sh` `DEFAULT_REGION`                 |
| `--cluster-name=NAME`         | GKE Cluster Name                                                | `common.sh` `DEFAULT_CLUSTER_NAME`           |
| `--image-tag=TAG`             | SemVer release tag or full 40-character commit SHA              | Checkout `HEAD`; required via `curl \| bash` |
| `--registry-prefix=PATH`      | Container registry path without a URL scheme                    | `common.sh` `DEFAULT_REGISTRY_PREFIX`        |
| `--allow-unverified-source`   | Provision from a dirty or mismatched checkout                   | `false`                                      |
| `--model-provider=NAME`       | `gemini` \| `anthropic` \| `chatgpt` \| `openai`                | `common.sh` `DEFAULT_MODEL_PROVIDER`         |
| `--gemini-api-key=KEY`        | Gemini API key                                                  | Looked up in Secret Manager                  |
| `--openai-api-key=KEY`        | OpenAI API key                                                  | _unset_                                      |
| `--anthropic-api-key=KEY`     | Anthropic API key                                               | _unset_                                      |
| `--permission-set=SET`        | Agent GCP IAM set: `read-only` \| `gke-admin` \| `custom`       | `read-only`                                  |
| `--custom-roles=ROLES`        | Roles for `--permission-set=custom` (space- or comma-separated) | _unset_                                      |
| `--gitops-org=ORG`            | GitHub org/user for the GitOps IaC repository                   | _unset_                                      |
| `--gitops-repo=REPO`          | GitOps IaC repository name                                      | `gke-fleet-iac`                              |
| `--enable-google-chat`        | Enable the Google Chat integration                              | `false`                                      |
| `--gvisor=true\|false`        | Enable GKE Sandbox (gVisor) runtime isolation                   | `false`                                      |
| `--enable-web-ui=true\|false` | Enable the Hermes Web UI on port 9119                           | `false`                                      |
| `-h, --help, -?`              | Output CLI usage banner and parameter details                   | `N/A`                                        |
