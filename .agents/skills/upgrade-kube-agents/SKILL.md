---
name: upgrade-kube-agents
description: Perform non-interactive or interactive Day-2 upgrades of the Kubernetes Agentic Harness and operator on GKE clusters.
---

# Upgrade Kubernetes Agentic Harness (kube-agents)

Use this skill when asked to upgrade the `kube-agents` Platform Agent or operator on an active GKE cluster.

## One-Liner Execution Mode (Non-Interactive)

To non-interactively upgrade `kube-agents` on a GKE cluster, run the one-liner **from the
directory holding the original install checkout** — the upgrade refuses to proceed without the
install's `install.env` configuration (a legacy `k8s-operator/scripts/vars.sh` also satisfies it),
because a full upgrade re-renders the whole install (the `PlatformAgent` CR included) from it.
`KUBE_AGENTS_INSTALL_ENV` points at a configuration held somewhere else, which is how an ephemeral
CI runner supplies one:

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

- `--upgrade-mode=harness`: `helm upgrade --reuse-values` re-tagging only the Platform Agent image (`platformAgent.deployment.image.tag`).
- `--upgrade-mode=operator`: applies the chart's CRDs with `kubectl` first (Helm never touches `crds/` on upgrade), then `helm upgrade --reuse-values` re-tagging only the operator image.
- `--upgrade-mode=full` (Default): applies the CRDs, then runs a full `terraform apply` at the new `--image-tag` through the install engine — both image tags move and every setting in `install.env` is re-rendered. This mode additionally requires the `terraform` CLI.

Every mode requires the `kube-agents` Helm release to exist in the target namespace. An install
without one predates the Terraform + Helm engine: upgrade it with the release that installed it
(curl the matching versioned `upgrade.sh`), or re-install with `install.sh` to adopt the new
engine.

## Dry-Run Mode

To preview the upgrade plan and output a JSON status report without modifying cloud resources:

```bash
./upgrade.sh --dry-run --upgrade-mode=full \
  --project-id="<PROJECT_ID>" \
  --image-tag="<SEMVER_TAG_OR_FULL_COMMIT_SHA>"
```

Machine-readable JSON status reports are generated at `/tmp/kube-agents-upgrade-report.json`.

`--image-tag` is required for an upgrade. Use a SemVer release tag or the full 40-character commit
SHA behind a validated RC tag; mutable refs such as `latest` and `main` are rejected so the upgrade
scripts and container images stay on the same revision.

Two flags make it optional, and they differ on whether it may be passed anyway:

- `--plan` reports what a full upgrade would change against the install's real Terraform state, and
  changes nothing. Exit 0 means in sync, 2 means there are changes, 1 means the plan failed. This is
  the only preview that can see drift; `--dry-run` above answers offline from configuration alone
  and plans against empty local state, so the two are refused together. `--image-tag` **is** accepted
  alongside it, and plans at that tag — which is what a drift check of a specific candidate wants.
- `--keep-image-tag` upgrades everything except the images, leaving them on the tag the install
  already serves. This one refuses `--image-tag`, because the two ask for opposite things. It is what
  a scheduled reconcile of an environment that tracks `main` uses.

Given no tag, both read the running one off the agent Deployment and validate it exactly as a passed
one, so an install serving a mutable ref stops the run rather than writing that ref into the
composition.
