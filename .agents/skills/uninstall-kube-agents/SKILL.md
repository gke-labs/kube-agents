---
name: uninstall-kube-agents
description: Discovers and removes provisioned kube-agents GCP/GKE infrastructure.
---

# Uninstall Kubernetes Agentic Harness (kube-agents)

Use this skill when asked to remove or uninstall `kube-agents` infrastructure from a GCP project or GKE cluster.

## One-Liner Uninstall Command (Non-Interactive)

To run the project teardown non-interactively:

```bash
curl -fsSL https://gke-labs.github.io/kube-agents/uninstall.sh | bash -s -- \
  --non-interactive \
  --project-id="<PROJECT_ID>" \
  --cluster-name="<CLUSTER_NAME>" \
  --region="<REGION>"
```

When the command does not run from a local `kube-agents` checkout, pass
`--source-ref="<SEMVER_TAG_OR_FULL_COMMIT_SHA>"` so the teardown scripts are fetched at the same
revision that was installed; otherwise they are fetched from `main`.

Machine-readable JSON status reports are generated at `/tmp/kube-agents-uninstall-report.json`.
