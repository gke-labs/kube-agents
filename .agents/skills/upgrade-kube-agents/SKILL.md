---
name: upgrade-kube-agents
description: Perform non-interactive or interactive Day-2 upgrades of the Kubernetes Agentic Harness (kube-agents), operator CRDs, and agent skills on GKE clusters.
---

# Upgrade Kubernetes Agentic Harness (kube-agents)

Use this skill when asked to upgrade the `kube-agents` platform agent, operator CRDs, or hot-reload repository skills on an active GKE cluster.

## One-Liner Execution Mode (Non-Interactive)

To non-interactively upgrade `kube-agents` on a GKE cluster:

```bash
curl -fsSL https://gke-labs.github.io/kube-agents/upgrade.sh | bash -s -- \
  --upgrade-mode="full" \
  --non-interactive \
  --project-id="<PROJECT_ID>" \
  --cluster-name="<CLUSTER_NAME>" \
  --region="<REGION>"
```

## Upgrade Modes

- `--upgrade-mode=skills`: Performs hot-reloading of agent skills without restarting running pods (zero downtime).
- `--upgrade-mode=harness`: Upgrades Platform Agent deployment and controller container images.
- `--upgrade-mode=operator`: Upgrades Kubernetes Operator CRDs and controller manager.
- `--upgrade-mode=full` (Default): Performs full atomic upgrade across operator, harness, and skills.

## Dry-Run Mode

To preview the upgrade plan and output a JSON status report without modifying cloud resources:

```bash
./upgrade.sh --dry-run --upgrade-mode=full
```

Machine-readable JSON status reports are generated at `/tmp/kube-agents-upgrade-report.json`.
