---
name: upgrade-kube-agents
description: Perform non-interactive or interactive Day-2 upgrades of the Kubernetes Agentic Harness and operator on GKE clusters.
---

# Upgrade Kubernetes Agentic Harness (kube-agents)

Use this skill when asked to upgrade the `kube-agents` Platform Agent or operator on an active GKE cluster.

## One-Liner Execution Mode (Non-Interactive)

Upgrade an install with the `upgrade.sh` published for the release you are moving to, substituting
`<RELEASE_VERSION>` with a release tag from
[GitHub Releases](https://github.com/gke-labs/kube-agents/releases). The release-pinned script
carries its own version, so no image tag is passed:

```bash
curl -fsSL https://raw.githubusercontent.com/gke-labs/kube-agents/<RELEASE_VERSION>/upgrade.sh | bash -s -- \
  --upgrade-mode="full" \
  --non-interactive \
  --project-id="<PROJECT_ID>" \
  --cluster-name="<CLUSTER_NAME>" \
  --region="<REGION>"
```

A full upgrade re-renders the whole install (the `PlatformAgent` CR included) from the install's
`install.env`, and refuses to proceed without it (a legacy `k8s-operator/scripts/vars.sh` also
satisfies it). `KUBE_AGENTS_INSTALL_ENV` names one outright, which is how an ephemeral CI runner
supplies it. Otherwise the script finds that configuration on its own: in the checkout it runs
from, then in the directory you run it from, and last in the install checkout the installer left in
`$HOME/kube-agents`.

The release bundle is the other supported source, and the one to use when the machine has no
install checkout. A bundle carries sources and no configuration, so give the run the install's
`install.env`:

```bash
curl -fsSLO https://github.com/gke-labs/kube-agents/releases/download/<RELEASE_VERSION>/kube-agents-<RELEASE_VERSION>.tar.gz
tar -xzf kube-agents-<RELEASE_VERSION>.tar.gz
cd kube-agents-<RELEASE_VERSION>
cp /path/to/the/install/install.env .
./upgrade.sh --upgrade-mode="full" --non-interactive --project-id="<PROJECT_ID>"
```

## Upgrade Modes

- `--upgrade-mode=harness`: `helm upgrade --reset-then-reuse-values` re-tagging the Platform Agent image (`platformAgent.deployment.image.tag`) and the sandbox it reaches over ssh (`agentSandbox.image.tag`), which is built from the same revision.
- `--upgrade-mode=operator`: applies the chart's CRDs with `kubectl` first (Helm never touches `crds/` on upgrade), then `helm upgrade --reset-then-reuse-values` re-tagging only the operator image.
- `--upgrade-mode=full` (Default): applies the CRDs, then runs a full `terraform apply` at the new `--image-tag` through the install engine — both image tags move and every setting in `install.env` is re-rendered. This mode additionally requires the `terraform` CLI.

Every mode requires the `kube-agents` Helm release to exist in the target namespace. An install
without one predates the Terraform + Helm engine: upgrade it with the release that installed it
(curl the matching versioned `upgrade.sh`), or re-install with `install.sh` to adopt the new
engine.

## Dry-Run Mode

To preview the upgrade plan and output a JSON status report without modifying cloud resources:

```bash
./upgrade.sh --dry-run --upgrade-mode=full --project-id="<PROJECT_ID>"
```

Machine-readable JSON status reports are generated at `/tmp/kube-agents-upgrade-report.json`.

## Targeting a Revision Other Than the Script's Own

A release copy of `upgrade.sh` already knows the version it upgrades to, so an upgrade to a
published release passes no tag at all. `--image-tag` overrides that default, and exists for
development and CI/CD testing — a candidate commit SHA, or a release other than the script's own:

```bash
# CI / testing override, not the path an install takes to a published release.
./upgrade.sh --non-interactive --upgrade-mode=full \
  --project-id="<PROJECT_ID>" \
  --image-tag="<SEMVER_TAG_OR_FULL_COMMIT_SHA>"
```

Use a SemVer release tag or the full 40-character commit SHA behind a validated RC tag; mutable
refs such as `latest` and `main` are rejected so the upgrade scripts and container images stay on
the same revision. A copy of the script carrying no baked version — one built from `main` — has no
default, and there the flag is the only way to name a revision.

Two flags change what the run targets, and both read differently depending on whether the copy of
the script carries a baked version. A release copy's version is in place before any flag is parsed:

- `--plan` reports what a full upgrade would change against the install's real Terraform state, and
  changes nothing. Exit 0 means in sync, 2 means there are changes, 1 means the plan failed. This is
  the only preview that can see drift; `--dry-run` above answers offline from configuration alone
  and plans against empty local state, so the two are refused together. `--image-tag` **is** accepted
  alongside it, and plans at that tag — which is what a drift check of a specific candidate wants.
  A release copy plans at its own baked release; a copy with no baked version and no `--image-tag`
  plans at the tag the install's Terraform state records.
- `--keep-image-tag` upgrades everything except the images, leaving them on the tag the install
  already serves. It refuses `--image-tag`, because the two ask for opposite things — and a release
  copy carries a version, so it refuses this flag too. It is what a scheduled reconcile of an
  environment that tracks `main` uses, from a checkout.

Given no tag at all, both read the running one off the agent Deployment and validate it exactly as a
passed one, so an install serving a mutable ref stops the run rather than writing that ref into the
composition.
