---
name: uninstall-kube-agents
description: Uninstall kube-agents (the Kubernetes Agentic Harness) — discover and remove the GCP/GKE infrastructure an install provisioned. Use when asked to uninstall, remove, or tear down kube-agents from a project or cluster.
---

# Uninstall Kubernetes Agentic Harness (kube-agents)

Use this skill when asked to remove or uninstall `kube-agents` infrastructure from a GCP project or GKE cluster.

## One-Liner Uninstall Command (Non-Interactive)

To run the project teardown non-interactively, use the `uninstall.sh` published for the release
the install runs, substituting `<RELEASE_VERSION>` with a release tag from
[GitHub Releases](https://github.com/gke-labs/kube-agents/releases). The release-pinned script
tears the install down with its own release's engine; a copy carrying no baked version falls back
to the engine on `main`, which is not the one that built the install:

```bash
curl -fsSL https://raw.githubusercontent.com/gke-labs/kube-agents/<RELEASE_VERSION>/uninstall.sh | bash -s -- \
  --non-interactive \
  --gcp-project-id="<PROJECT_ID>" \
  --gke-cluster-name="<CLUSTER_NAME>" \
  --gcp-region="<REGION>"
```

The engine is `lifecycle.sh destroy` in `terraform/examples/full-install`, run against the
install's Terraform state in GCS (bucket `<project>-kube-agents-tfstate`, prefix
`kube-agents/<cluster>` — derived from the coordinates, so a fresh clone finds it). Before
`terraform destroy` it handles the four asymmetries a bare destroy trips over: it forgets the
undeletable KMS resources from state (kept usable in GCP, re-adopted on the next apply), deletes
the `PlatformAgent` CR and force-clears its finalizer if the operator is wedged, purges every
backup the GKE BackupPlan owns, and clears the cluster's deletion protection.

When the command does not run from a local `kube-agents` checkout, the teardown engine is fetched:
at the script's own baked release when it has one, and from `main` when it does not.
`--source-ref="<SEMVER_TAG_OR_FULL_COMMIT_SHA>"` names a revision instead, which is how an
unstamped copy is pointed at the release that was installed.

`terraform` must be on `PATH` — the teardown engine, which this script never installs for you.
See the site's [uninstall page](../../../docs/site/src/content/docs/install/uninstall.md).

**No Terraform state anywhere** (none in GCS, none locally) means one of two things, and the
uninstaller exits **3** without touching anything either way: nothing is installed against
these coordinates, or the install was made by a pre-Terraform release. Only the second is
recoverable here — re-run with `--source-ref=<that release>` so that release's own teardown
runs instead. Check whether the cluster exists before reaching for a release tag.

Exit 3 is the one non-zero exit that is not a failure; exit 1 means the teardown could not
start or started and did not finish. `./uninstall.sh --help` is the contract.

Machine-readable JSON status reports are generated at `/tmp/kube-agents-uninstall-report.json`.
