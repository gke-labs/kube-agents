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
  --model-provider="gemini" \
  --permission-set="sre"
```

## Dry-Run Inspection

To validate prerequisites and generate configuration state (`vars.sh`) without creating GCP resources, use `--dry-run`:

```bash
./install.sh --dry-run --non-interactive --project-id="YOUR_GCP_PROJECT_ID"
```

## Installation Validation & Success Criteria Tiers

When validating an installation test run, evaluate success using the following three tiers:

| Tier | Evaluation Criteria | Description |
| :--- | :--- | :--- |
| **🏆 Perfect Success** | **Issue Detected & PR Logged** | The Platform Agent detects an active cluster failure within the first 10 minutes and automatically formulates a remediation PR or action plan. |
| **🥇 Good Success** | **Clean Fleet Triage** | Fleet-wide event triage takes place across all watched clusters, activeinformers stream telemetry, and no actionable errors are found. |
| **🥈 OK Success** | **Control Plane Deployment** | The control plane installs cleanly, rolls out workloads, and has full visibility/connectivity across all target GCP project clusters. |

## Machine-Readable Results

Upon completion, `install.sh` generates a machine-readable JSON status report at `/tmp/kube-agents-install-report.json`:

```json
{
  "status": "SUCCESS",
  "dry_run": false,
  "non_interactive": true,
  "project_id": "gca-gke-2025",
  "cluster_name": "kube-agents-platform",
  "timestamp": "2026-08-05T03:35:00Z"
}
```

## Supported Command-Line Flags

| Flag | Description | Default |
| :--- | :--- | :--- |
| `-y, --non-interactive` | Run without blocking on `/dev/tty` prompts | `false` |
| `--dry-run` | Output plan and `vars.sh` without creating resources | `false` |
| `--project-id=ID` | Target GCP Project ID | Active `gcloud` project |
| `--region=REGION` | Target GCP Region | `us-central1` |
| `--cluster-name=NAME` | GKE Cluster Name | `kube-agents-platform` |
| `--model-provider=NAME` | LLM Model Provider (`gemini` \| `openai` \| `anthropic`) | `gemini` |
| `--permission-set=SET` | Platform Agent RBAC scope (`sre` \| `read-only`) | `sre` |
| `--gvisor=true\|false` | Enable GKE Sandbox runtime isolation | `false` |
| `-h, --help, -?` | Output CLI usage banner and parameter details | `N/A` |
