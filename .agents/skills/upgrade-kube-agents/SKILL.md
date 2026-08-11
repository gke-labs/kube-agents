---
name: upgrade-kube-agents
description: Perform non-interactive or interactive Day-2 upgrades of the Kubernetes Agentic Harness and operator on GKE clusters.
---

# Upgrade Kubernetes Agentic Harness (kube-agents)

Use this skill when asked to upgrade the `kube-agents` Platform Agent or operator on an active GKE cluster.

## One-Liner Execution Mode (Non-Interactive)

To non-interactively upgrade `kube-agents` on a GKE cluster, run the one-liner **from the
directory holding the original install checkout** — the upgrade refuses to proceed without the
saved `k8s-operator/scripts/vars.sh` configuration state, because the provisioning scripts
re-render the PlatformAgent Custom Resource from it:

```bash
curl -fsSL https://gke-labs.github.io/kube-agents/upgrade.sh | bash -s -- \
  --upgrade-mode="full" \
  --non-interactive \
  --project-id="<PROJECT_ID>" \
  --cluster-name="<CLUSTER_NAME>" \
  --region="<REGION>" \
  --image-tag="<SEMVER_TAG_OR_FULL_COMMIT_SHA>"
```

## Upgrade Modes

- `--upgrade-mode=harness`: Upgrades Platform Agent deployment and controller container images.
- `--upgrade-mode=operator`: Upgrades Kubernetes Operator CRDs and controller manager.
- `--upgrade-mode=full` (Default): Upgrades both the operator and Platform Agent harness.

## Dry-Run Mode

To preview the upgrade plan and output a JSON status report without modifying cloud resources:

```bash
./upgrade.sh --dry-run --upgrade-mode=full \
  --project-id="<PROJECT_ID>" \
  --image-tag="<SEMVER_TAG_OR_FULL_COMMIT_SHA>"
```

Machine-readable JSON status reports are generated at `/tmp/kube-agents-upgrade-report.json`.

`--image-tag` is required in every mode. Use a SemVer release tag or the full 40-character commit
SHA behind a validated RC tag; mutable refs such as `latest` and `main` are rejected so the upgrade
scripts and container images stay on the same revision.
