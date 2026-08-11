---
name: install-kube-agents
description: Provision and install the Kubernetes Agentic Harness (kube-agents) onto a GKE cluster non-interactively or interactively.
---

# `install-kube-agents` Skill

This skill provides step-by-step instructions for AI Agents to non-interactively provision Google Cloud GKE infrastructure and deploy the `kube-agents` Platform Agent.

## Quick Execution for AI Agents

To run the installer non-interactively in automated subagent execution, pass `--non-interactive` along with explicit configuration flags:

```bash
curl -fsSL https://raw.githubusercontent.com/gke-labs/kube-agents/main/install.sh | bash -s -- \
  --non-interactive \
  --project-id="YOUR_GCP_PROJECT_ID" \
  --cluster-name="kube-agents-platform" \
  --region="us-central1" \
  --image-tag="<SEMVER_TAG_OR_FULL_COMMIT_SHA>" \
  --model-provider="gemini" \
  --permission-set="read-only"
```

## Dry-Run Inspection

To validate prerequisites and generate configuration state (`vars.sh`) without creating GCP resources, use `--dry-run`:

```bash
./install.sh --dry-run --non-interactive \
  --project-id="YOUR_GCP_PROJECT_ID" \
  --image-tag="<SEMVER_TAG_OR_FULL_COMMIT_SHA>"
```

## Machine-Readable Results

Upon completion, `install.sh` generates a machine-readable JSON status report at `/tmp/kube-agents-install-report.json`:

```json
{
  "status": "SUCCESS",
  "dry_run": false,
  "non_interactive": true,
  "project_id": "YOUR_GCP_PROJECT_ID",
  "cluster_name": "kube-agents-platform",
  "timestamp": "2026-08-05T03:35:00Z"
}
```

## Supported Command-Line Flags

| Flag                     | Description                                              | Default                        |
| :----------------------- | :------------------------------------------------------- | :----------------------------- |
| `-y, --non-interactive`  | Run without blocking on `/dev/tty` prompts               | `false`                        |
| `--dry-run`              | Output plan and `vars.sh` without creating resources     | `false`                        |
| `--project-id=ID`        | Target GCP Project ID                                    | Active `gcloud` project        |
| `--region=REGION`        | Target GCP Region                                        | `us-central1`                  |
| `--cluster-name=NAME`    | GKE Cluster Name                                         | `kube-agents-platform`         |
| `--image-tag=TAG`        | SemVer release tag or full 40-character commit SHA       | Required                       |
| `--registry-prefix=PATH` | Container registry path without a URL scheme             | `ghcr.io/gke-labs/kube-agents` |
| `--model-provider=NAME`  | LLM Model Provider (`gemini` \| `openai` \| `anthropic`) | `gemini`                       |
| `--permission-set=SET`   | Platform Agent RBAC scope (`read-only` \| `gke-admin`)   | `read-only`                    |
| `--gvisor=true\|false`   | Enable GKE Sandbox runtime isolation                     | `false`                        |
| `-h, --help, -?`         | Output CLI usage banner and parameter details            | `N/A`                          |
