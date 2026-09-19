---
title: Upgrade
description: Moving an existing install to a newer release with upgrade.sh — its modes, how the target version is resolved, and what a run refuses before it changes anything.
---

`upgrade.sh` is the Day-2 engine for an install `install.sh` created. It re-applies the same
Terraform composition and the same Helm chart at a newer revision, re-rendering the install from
the configuration it already has. A release copy of the script carries the version it upgrades to,
so the ordinary upgrade names no version at all.

## Before you start

Record what the install runs now, so you can tell afterwards what moved:

```bash
kubectl get deployment platform-agent-gateway -n kubeagents-system \
  -o jsonpath='{.spec.template.spec.containers[?(@.name=="platform-agent")].image}{"\n"}'
kubectl get deployment kube-agents-controller-manager -n kubeagents-system \
  -o jsonpath='{.spec.template.spec.containers[*].image}{"\n"}'
helm history kube-agents -n kubeagents-system
```

Read the release notes for every release between the one you run and the one you are moving to.

The upgrade refuses to run without the install's own configuration, because a full upgrade
re-renders the `PlatformAgent` resource from it: a file written from memory re-renders the install
with whatever it forgets. `KUBE_AGENTS_INSTALL_ENV` names the file outright, which is how an
ephemeral CI runner supplies one. Otherwise the script looks for `install.env` in the checkout it
is running from, then in the directory you run it from, and last in the install checkout the
installer left in `$HOME/kube-agents` — so standing in one install's directory upgrades that
install, not whichever one the checkout in `$HOME` belongs to. A legacy
`k8s-operator/scripts/vars.sh` from an install that predates `install.env` also satisfies the
requirement.

`--upgrade-mode=full`, the default, additionally needs the `terraform` CLI on `PATH`.

## Run the upgrade

Substitute `<RELEASE_VERSION>` with the release tag you are moving to, from
[GitHub Releases](https://github.com/gke-labs/kube-agents/releases):

```bash
curl -fsSL https://raw.githubusercontent.com/gke-labs/kube-agents/<RELEASE_VERSION>/upgrade.sh | bash -s -- \
  --non-interactive \
  --project-id="my-gcp-project" \
  --cluster-name="platform-agent-host" \
  --region="us-central1"
```

The release-pinned script upgrades to its own version, so you pass no tag. It reuses the install
checkout in `$HOME/kube-agents`, moving it to the release you asked for, and reads the `install.env`
in it. A checkout with uncommitted changes is left alone and the run stops rather than upgrading
from sources that do not match the release.

The release bundle is the other supported source, and the one to use on a machine with no install
checkout:

```bash
curl -fsSL https://github.com/gke-labs/kube-agents/releases/download/<RELEASE_VERSION>/kube-agents-<RELEASE_VERSION>.tar.gz | tar -xz
cd kube-agents-<RELEASE_VERSION>
cp /path/to/your/install/install.env .
./upgrade.sh --non-interactive --project-id="my-gcp-project"
```

## Upgrade modes

- `--upgrade-mode=harness` re-tags the Platform Agent image and the sandbox image it reaches over
  ssh — both are built from the same revision — through `helm upgrade --reset-then-reuse-values`.
- `--upgrade-mode=operator` applies the chart's CRDs with `kubectl` first — Helm never touches
  `crds/` on an upgrade — then re-tags the operator image the same way.
- `--upgrade-mode=full`, the default, applies the CRDs and then runs a full `terraform apply`
  through the install engine: both image tags move, and every setting in `install.env` is
  re-rendered.

The operator moves before the harness because the operator owns the resources the harness runs as.
Every mode needs the `kube-agents` Helm release to exist in the target namespace; an install
without one predates the Terraform and Helm engine, and has to be re-installed to adopt it.

## Choosing what the run targets

A release copy of the script carries the version it upgrades to, which is what makes the one-liner
above flagless. That baked version is the run's target from the moment it starts, before any flag
is read, so two of the flags below behave differently depending on which copy you are holding.

- `--image-tag` names a revision to move to instead: a release tag or a full commit SHA. It
  overrides the baked version, and it exists for development and CI/CD testing — a candidate
  commit, or a release the script does not itself carry. It is not part of upgrading to a published
  release. Mutable refs such as `latest`, `main` and `HEAD` are rejected, so the scripts and the
  container images always name the same revision.
- `--keep-image-tag` upgrades everything except the images, leaving them on the tag the install
  already serves. It refuses `--image-tag`, because the two ask for opposite things — and since a
  release copy already carries a version, it refuses this flag as well. Run it from a checkout,
  which is where the scheduled reconciles that need it run.
- `--plan` changes nothing and reports what an upgrade would do. From a release copy it plans at
  that release: the question it answers is whether moving to that version would change anything.
  From a copy with no baked version and no `--image-tag`, it plans at the tag the install's
  Terraform state records, so the report is composition drift rather than image lag. Either way it
  exits 0 when the install is in sync, 2 when there are changes, and 1 when the plan itself failed.

Given no tag and no flag, a copy of the script built from `main` has no version to default to, and
asks for one.

## Previewing

`--dry-run` and `--plan` are both previews and are deliberately not the same one. `--dry-run`
answers offline, from configuration alone, and never contacts the install. `--plan` answers from
the install's real Terraform state, so it needs credentials, and it is the only one of the two that
can report drift. The two are refused together.

Neither moves the install checkout in `$HOME/kube-agents`: a preview that needs sources the
checkout does not have reads them from a temporary copy instead, so the checkout is still on the
release the install runs when the preview is over.

## Checking the result

```bash
kubectl get deployment platform-agent-gateway -n kubeagents-system \
  -o jsonpath='{.spec.template.spec.containers[?(@.name=="platform-agent")].image}{"\n"}'
kubectl get deployment kube-agents-controller-manager -n kubeagents-system \
  -o jsonpath='{.spec.template.spec.containers[*].image}{"\n"}'
kubectl get platformagent platform-agent -n kubeagents-system \
  -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}{"\n"}'
kubectl get pods -n kubeagents-system
helm history kube-agents -n kubeagents-system
```

Both images end in `:<RELEASE_VERSION>`, the `Ready` condition reads `True`, the gateway pod is
`Running`, and the newest Helm revision is `deployed`. `kubeagents-system` is the default
namespace; an install that set `NAMESPACE` in `install.env` uses that one. `platform-agent` is the
chart's default `platformAgent.name` value.

The run also writes a machine-readable report to `/tmp/kube-agents-upgrade-report.json`.

## When an upgrade is refused

These refusals happen before anything on the cluster moves.

- **The sources do not match the release.** The checkout's `HEAD` is not the release's commit, the
  tree has uncommitted changes, or a bundle's baked version is not the version asked for. Start
  again from a clean checkout or bundle.
- **No install configuration.** Neither `KUBE_AGENTS_INSTALL_ENV`, nor an `install.env` in the
  checkout the script runs from, the directory you are standing in, or the install checkout in
  `$HOME/kube-agents`, nor a legacy `k8s-operator/scripts/vars.sh` was found.
- **No Helm release.** The target namespace has no `kube-agents` release to upgrade.

## Where to go next

- [Rolling back a release](/kube-agents/deploy/rollback/) — the reverse move, and what each mode
  leaves behind when it goes backwards.
- [Release versioning and promotion](/kube-agents/deploy/release-versioning/) — what a release tag
  guarantees about the artifacts it names.
- [Uninstall](/kube-agents/install/uninstall/) — removing the install instead of moving it.
