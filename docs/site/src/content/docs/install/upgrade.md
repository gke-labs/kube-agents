---
title: Upgrade
description: Moving an existing install to a newer release with upgrade.sh — its modes, how the target version is resolved, and what a run refuses before any of the new release is applied.
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
is running from, then in the directory you run it from, and — when run from outside a checkout,
such as the piped release one-liner — last in the install checkout the installer left in
`$HOME/kube-agents`, so standing in one install's directory upgrades that install, not whichever
one the checkout in `$HOME` belongs to.

`--upgrade-mode=full`, the default, additionally needs the `terraform` CLI on `PATH`.

## Run the upgrade

Substitute `<RELEASE_VERSION>` with the release tag you are moving to, from
[GitHub Releases](https://github.com/gke-labs/kube-agents/releases):

```bash
curl -fsSL https://raw.githubusercontent.com/gke-labs/kube-agents/<RELEASE_VERSION>/upgrade.sh | bash -s -- \
  --non-interactive \
  --gcp-project-id="my-gcp-project" \
  --gke-cluster-name="platform-agent-host" \
  --gcp-region="us-central1"
```

The release-pinned script upgrades to its own version, so you pass no tag. It reuses the install
checkout in `$HOME/kube-agents`, moving it to the release you asked for, and reads the `install.env`
in it. A checkout with uncommitted changes is left alone and the run stops rather than upgrading
from sources that do not match the release. Only a release copy is flagless: a copy built from
`main` carries no version and asks for one, so name a release tag in the URL rather than a branch.

The release bundle is the other supported source, and the one to use on a machine with no install
checkout:

```bash
curl -fsSL https://github.com/gke-labs/kube-agents/releases/download/<RELEASE_VERSION>/kube-agents-<RELEASE_VERSION>.tar.gz | tar -xz
cd kube-agents-<RELEASE_VERSION>
cp /path/to/your/install/install.env .
./upgrade.sh --non-interactive --gcp-project-id="my-gcp-project"
```

## Upgrade modes

- `--upgrade-mode=harness` re-tags the Platform Agent image, the sandbox image it reaches over
  ssh, and every plugin image the release records — all are built from the same revision —
  through `helm upgrade --reset-then-reuse-values`.
- `--upgrade-mode=operator` applies the chart's CRDs with `kubectl` first — Helm never touches
  `crds/` on an upgrade — then re-tags the operator image the same way.
- `--upgrade-mode=full`, the default, applies the CRDs and then runs a full `terraform apply`
  through the install engine: every image tag moves, and every setting in `install.env` is
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

Neither moves the install checkout in `$HOME/kube-agents` or edits `install.env`: a preview that
needs sources the checkout does not have reads them from a temporary copy instead, so the checkout
is still on the release the install runs when the preview is over. When a run is configured from
`$(pwd)/install.env` or `KUBE_AGENTS_INSTALL_ENV` outside `$HOME/kube-agents`, it fetches a
temporary copy as well rather than touching `$HOME/kube-agents`. When `--plan` reuses the
install's own checkout because it is already on the target release (or runs from inside it),
`--plan` refreshes the generated `terraform/examples/full-install/terraform.tfvars` and
`.terraform/` working directory there so Terraform can plan against the live backend; `--dry-run`
exits before Terraform runs and writes nothing.

Neither is refused by an edited checkout either. A preview of what an uncommitted change would
apply is the one report that answers "what have I edited here", so both previews warn and continue
where a real upgrade stops. When the configuration the run loaded records a different install than
the flags name, only `--dry-run` warns and continues — `--plan` refuses alongside a real upgrade
because planning writes `terraform.tfvars` and reconfigures `.terraform/` in the working sources
(see [Naming the install](#naming-the-install)).

What a preview will not do is report on a run that could not happen: with no `install.env` anywhere
it stops with the same refusal a real upgrade gives, rather than printing a plan you could not
execute.

## Naming the install

The lookup order above is what keeps the configuration and the target install in step, because
standing in an install's directory upgrades that install. The piped one-liner has nowhere to stand,
so on a machine with two installs it loads `$HOME/kube-agents/install.env` whichever cluster the
flags name — and a full upgrade re-renders the `PlatformAgent` resource from that file, so the
mismatch writes one install's chat space, allowed users, model provider and namespace into the
other.

So the run compares them. When `--gcp-project-id`, `--gke-cluster-name` or `--gcp-region` names
something the loaded `install.env` records differently, both a real upgrade and `--plan` refuse,
while `--dry-run` reports the mismatch and goes on. Point `KUBE_AGENTS_INSTALL_ENV` at the
`install.env` of the install you are upgrading, run from its checkout, or drop the flag that
disagrees.

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

Every one of these stops the run before any of the new release is applied. The first three are
settled before the run touches the cluster at all. The last two need the cluster: they are settled
after `kubectl` has been pointed at it, and after a real run's pre-flight Secret backfills — a plan
skips those — but still before any CRD, chart or Terraform change of the new release.

- **The sources do not match the release.** The checkout's `HEAD` is not the release's commit, the
  tree has uncommitted changes, or an unpacked tree names a release other than the one asked for —
  either in its `.release-bundle` marker or in the version stamped into its root scripts. A release
  script carries its version with it, so standing in an older unpacked tree and running a newer one
  is refused rather than silently applying the older tree. The stamp is read from `install.sh` and
  `uninstall.sh` before `upgrade.sh`, because saving a newer `upgrade.sh` into an older tree
  overwrites the one file that would otherwise be asked. Start again from a clean checkout or from
  the bundle of the release you want.
- **`KUBE_AGENTS_INSTALL_ENV` names a file that is not there.** An explicit pointer is taken
  literally, not fallen back from, so a stale or mistyped value is reported with the path you gave
  rather than searched past. Fix the path or unset the variable.
- **No install configuration.** Neither `KUBE_AGENTS_INSTALL_ENV` nor an `install.env` was found in
  any of the configuration locations described at the top of this page. An install predating 0.4.0,
  which kept its settings in the retired `k8s-operator/scripts/vars.sh` and never gained an
  `install.env`, is refused here: copy those settings into an `install.env` first.
- **No Helm release.** The target namespace has no `kube-agents` release to upgrade.
- **The memory store cannot be checked and nothing named one.** When the configuration records no
  `MEMORY`, the upgrade asks the cluster whether it runs the Hindsight memory store, so that a store
  that is there is kept rather than planned away. If the cluster cannot be asked — `kubectl` is
  pointed elsewhere, the credentials have expired, the API server times out — the run stops instead
  of guessing. Record `MEMORY=hindsight|file|off` in `install.env`, or restore access to the cluster
  and re-run.

## Where to go next

- [Rolling back a release](/kube-agents/deploy/rollback/) — the reverse move, and what each mode
  leaves behind when it goes backwards.
- [Release versioning and promotion](/kube-agents/deploy/release-versioning/) — what a release tag
  guarantees about the artifacts it names.
- [Uninstall](/kube-agents/install/uninstall/) — removing the install instead of moving it.
