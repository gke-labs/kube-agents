---
title: Rolling back a release
description: Moving an install from GA release N back to N-1 with the N-1 checkout's upgrade.sh — the two commands, what they change, what they leave, and what they refuse.
sidebar:
  order: 7
---

A rollback is an upgrade run from the older release's own checkout. There is no rollback flag:
`upgrade.sh` refuses to run unless the sources it runs from are the exact commit `--image-tag`
names, so a move to `N-1` always runs `N-1`'s copy of the script, `N-1`'s chart and `N-1`'s CRDs
against your install. Two of its three modes do that by Helm alone and never run Terraform; the
third re-applies `N-1`'s Terraform composition. The supported rollback is the Helm-only pair below.
The full mode is the GCP-level revert, and it needs a plan read first.

## Before you start

Record what the install runs now, so you can tell afterwards what moved:

```bash
kubectl get deployment platform-agent-gateway -n kubeagents-system \
  -o jsonpath='{.spec.template.spec.containers[?(@.name=="platform-agent")].image}{"\n"}'
kubectl get deployment kube-agents-controller-manager -n kubeagents-system \
  -o jsonpath='{.spec.template.spec.containers[*].image}{"\n"}'
helm history kube-agents -n kubeagents-system
```

Get `N-1`'s sources. Either a clean checkout of the tag or the release bundle passes the
source check:

```bash
git clone --branch <N-1> --depth 1 https://github.com/gke-labs/kube-agents.git kube-agents-<N-1>
cd kube-agents-<N-1>
```

or unpack `kube-agents-<N-1>.tar.gz` from
[the release](https://github.com/gke-labs/kube-agents/releases), whose scripts carry `N-1` as
their baked version. A checkout at any other commit, or one with uncommitted changes, is refused
before anything is touched.

The script also refuses to run without the install's configuration, because it re-renders the
`PlatformAgent` resource from it. Copy the install's `install.env` into the new checkout, or point
`KUBE_AGENTS_INSTALL_ENV` at where it lives:

```bash
export KUBE_AGENTS_INSTALL_ENV=/path/to/your/install/install.env
```

Read `N`'s release notes before you go. Everything they list is something `N-1` does not know
about, and the next two sections say what happens to each kind of thing.

## The rollback

From the `N-1` checkout, operator first, then harness:

```bash
./upgrade.sh --dry-run --upgrade-mode=operator --image-tag <N-1>
./upgrade.sh --upgrade-mode=operator --image-tag <N-1>
./upgrade.sh --upgrade-mode=harness --image-tag <N-1>
```

The operator step applies `N-1`'s CRDs to the cluster with a server-side apply, then runs one
`helm upgrade` on the existing release with `N-1`'s chart, re-tagging the operator image. From
that point `N-1`'s operator, working from `N-1`'s schema, is reconciling the `PlatformAgent`. The
harness step is a second `helm upgrade` with the same chart re-tagging the agent image (on
releases that ship the shell sandbox, the sandbox image with it), followed by a wait for the
rollout. Operator first because the CRD schema and the controller have to agree before the agent
that the controller renders is replaced; harness first would put `N-1`'s agent under `N`'s
operator for the length of a rollout.

Each step waits for its rollout and fails when the wait times out: two minutes for the operator,
fifteen for the agent. A timeout is not a signal to run the step again. Read the pod first:

```bash
kubectl get pods -n kubeagents-system
kubectl describe pod -n kubeagents-system -l app=platform-agent-gateway
```

`--dry-run` prints the target and the image references the step would apply, from the
configuration alone, and contacts nothing. It runs from the same checkout, so it also exercises
the source check.

## What the two steps change

- The Helm release: `N-1`'s chart version, two new revisions in `helm history`.
- The operator and agent images, at tag `N-1`; the sandbox image too when `N-1`'s chart has it.
- The CRD schema, now `N-1`'s.
- Every object the chart renders, including the `PlatformAgent` resource. Objects `N`'s chart
  rendered and `N-1`'s does not are deleted by the upgrade; that is Helm's ordinary behaviour.

## What they leave as it is

Terraform state and every GCP resource. The Helm-only modes write nothing to state, so it keeps
recording `N` as the installed tag. `./upgrade.sh --plan` with no `--image-tag` plans at the tag
state records and therefore reports the rollback as no drift; the state and the cluster disagree
until the next `--upgrade-mode=full`, which re-applies whatever tag it is given. The
`terraform.tfvars` in the `N-1` checkout is regenerated on every run and is not a record of
anything.

Secrets. The script never rewrites a Secret value that exists. A key that `N-1`'s script knows and
finds missing is generated and added, which is what a forward upgrade does too.

Objects `N`'s operator created that `N-1`'s operator does not know. The operator only reconciles
what its own release renders, so an object introduced by a later release keeps running, on `N`'s
image, unmanaged, until the next forward upgrade re-adopts it. The shell sandbox is the current
example: rolling back to a release whose chart has no `agentSandbox` values (0.4.0 and earlier)
leaves the `platform-agent-shell` StatefulSet in place on `N`'s image, and that release's harness
step re-tags the agent alone.

Fields `N` added to the `PlatformAgent` schema. Once `N-1`'s CRD is applied, the API server prunes
them from the stored object, and `N-1`'s chart does not render them. The Helm values that produced
them survive in the release, so a later forward upgrade renders them again.

The agent's persistent volume. The harness step rolls the pod, and the volume follows it. Files
`N`'s agent wrote there stay, and `N-1`'s image syncs its own defaults over the paths it owns at
start-up, as on any restart.

## Reverting GCP resources too

```bash
./upgrade.sh --plan --image-tag <N-1>
./upgrade.sh --upgrade-mode=full --image-tag <N-1>
```

From the `N-1` checkout, the full mode re-applies `N-1`'s Terraform composition on top of the
state `N` wrote, and its Helm half is the re-tag the pair above already did. This is the
GCP-level revert: a resource `N`'s composition added is planned for destruction because `N-1`'s
composition does not declare it, and a setting `N` changed goes back. Read the whole plan first.
A plan that destroys a bucket, a KMS key or the cluster is a decision to take with what those
resources hold in front of you, not a rollback step; the composition on `N` may have added a
resource that now carries data. The plan's exit code follows Terraform's: `0` for no changes,
`2` for changes, `1` for an error.

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

Both images end in `:<N-1>`, the `Ready` condition reads `True`, the gateway pod is `Running`,
and the newest Helm revision is `deployed` at chart version `N-1`, with the operator step's
revision `superseded` just before it. `kubeagents-system` is
the default namespace; an install that set `NAMESPACE` in `install.env` uses that one.
`platform-agent` is the chart's default `platformAgent.name` value.

## When a rollback is refused

Each refusal happens before anything on the cluster moves.

- **The sources do not match the tag.** The checkout's `HEAD` is not the tag's commit, the tree
  has uncommitted changes, or the bundle's baked version is not the `--image-tag` given. Start
  again from a clean checkout or bundle of `N-1`.
- **No install configuration.** Neither `install.env` beside the script nor
  `KUBE_AGENTS_INSTALL_ENV` was found. Supply the install's own file; a fresh one written from
  memory re-renders the `PlatformAgent` with whatever it forgets.
- **`N` introduced a new API version of the CRD and objects were stored in it.** The API server
  rejects a CRD whose `spec.versions` drops a version that is still listed in
  `status.storedVersions`, so the operator step fails at the CRD apply. Every published release
  serves `v1alpha1` alone, so this refusal is a future one; it is here so that the day it happens
  the error is recognised as the rollback boundary it is.

Two things this page does not do. `helm rollback kube-agents <revision>` reverts the chart and
values to an earlier revision without applying that revision's CRDs and without the source check,
which is why the runbook goes through `upgrade.sh`; `upgrade.sh` itself uses `helm rollback` only
to un-stick a release left in a `pending-*` state. And nothing here restores data: the agent's
volume, the Terraform state bucket and anything `N`'s agent wrote to a repository or a cluster are
where `N` left them.
