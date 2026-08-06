---
name: uninstall-kube-agents
description: Discovers and removes all provisioned GCP/GKE infrastructure elements or resets kube-agents to a clean factory release state.
---

# Uninstall / Reset Kubernetes Agentic Harness (kube-agents)

Use this skill when asked to remove, uninstall, or factory-reset `kube-agents` infrastructure from a GCP project or GKE cluster.

## One-Liner Uninstall Command (Non-Interactive)

To safely discover and delete all `kube-agents` elements (GKE cluster, IAM service accounts, Pub/Sub topics, Secret Manager secrets, operator CRDs, and namespaces):

```bash
curl -fsSL https://gke-labs.github.io/kube-agents/uninstall.sh | bash -s -- \
  --non-interactive \
  --fleet \
  --purge-storage \
  --clean-gitops \
  --project-id="<PROJECT_ID>" \
  --cluster-name="<CLUSTER_NAME>" \
  --region="<REGION>"
```

Or via the installer CLI:

```bash
./install.sh --uninstall --non-interactive --fleet --purge-storage --clean-gitops --project-id="<PROJECT_ID>"
```

## Factory Reset Command

To purge existing infrastructure, wipe cached local configuration state, sync the repository to the latest release on `origin/main`, and re-trigger a clean installation:

```bash
./install.sh --reset --non-interactive
```

## Dry-Run Preview Mode

To preview elements that will be deleted without touching cloud resources:

```bash
./uninstall.sh --dry-run
```

Machine-readable JSON status reports are generated at `/tmp/kube-agents-uninstall-report.json`.
