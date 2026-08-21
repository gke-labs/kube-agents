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

The engine is `lifecycle.sh destroy` in `terraform/examples/full-install`, run against the
install's Terraform state in GCS (bucket `<project>-kube-agents-tfstate`, prefix
`kube-agents/<cluster>` — derived from the coordinates, so a fresh clone finds it). Before
`terraform destroy` it handles the four asymmetries a bare destroy trips over: it forgets the
undeletable KMS resources from state (kept usable in GCP, re-adopted on the next apply), deletes
the `PlatformAgent` CR and force-clears its finalizer if the operator is wedged, purges every
backup the GKE BackupPlan owns, and clears the cluster's deletion protection.

When the command does not run from a local `kube-agents` checkout, pass
`--source-ref="<SEMVER_TAG_OR_FULL_COMMIT_SHA>"` so the teardown engine is fetched at the same
revision that was installed; otherwise it is fetched from `main`.

**Installs with no Terraform state** (in GCS or locally) were created by a pre-Terraform
release. This uninstaller exits without touching them — re-run it with
`--source-ref=<that release>` so that release's own teardown runs instead.

Machine-readable JSON status reports are generated at `/tmp/kube-agents-uninstall-report.json`.
