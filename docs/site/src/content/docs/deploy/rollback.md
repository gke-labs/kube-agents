---
title: Rolling back a release
description: Moving an install from GA release N back to N-1 with the N-1 checkout's upgrade.sh — the two commands, what they change, what they leave, and what they refuse.
sidebar:
  order: 7
---

A rollback is an upgrade run from the older release's own checkout. There is no rollback flag:
`upgrade.sh` refuses to run unless the sources it runs from are the exact commit `--image-tag`
names, so a move to `N-1` from a checkout or a release bundle runs `N-1`'s copy of the script,
`N-1`'s chart and `N-1`'s CRDs against your install. Two of its three modes do that by Helm alone
and never run Terraform; the third re-applies `N-1`'s Terraform composition, Helm release
included. The supported rollback is the Helm-only pair below. The full mode is the GCP-level
revert, and it needs a plan read first.

Because the script that runs is `N-1`'s, its timeouts and Helm flags are `N-1`'s too, and this
page says where the published releases differ. The `curl | bash` one-liner is the exception: it
runs the published script and fetches only `N-1`'s chart, CRDs and installer library, so a
rollback done that way has the current script's behaviour against `N-1`'s chart. This page
describes the checkout.

## Before you start

Record what the install runs now, so you can tell afterwards what moved:

```bash
kubectl get deployment platform-agent-gateway -n kubeagents-system \
  -o jsonpath='{.spec.template.spec.containers[?(@.name=="platform-agent")].image}{"\n"}'
kubectl get deployment kube-agents-controller-manager -n kubeagents-system \
  -o jsonpath='{.spec.template.spec.containers[*].image}{"\n"}'
kubectl get deployment platform-agent-gateway -n kubeagents-system \
  -o jsonpath='{range .spec.template.spec.initContainers[*]}{.image}{"\n"}{end}{range .spec.template.spec.volumes[*]}{.image.reference}{"\n"}{end}'
helm history kube-agents -n kubeagents-system
```

The third command lists the gateway's init container and image volume images; the plugin ones are
the lines naming `pubsub-platform` or `gke-stockout-investigator` (a `stage-<plugin>` init
container or a `plugin-<name>` image volume, depending on the cluster). None means no plugin is
enabled.

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

From the `N-1` checkout, a dry run, then operator and harness back to back:

```bash
./upgrade.sh --dry-run --upgrade-mode=operator --image-tag <N-1>
./upgrade.sh --upgrade-mode=operator --image-tag <N-1>
./upgrade.sh --upgrade-mode=harness --image-tag <N-1>
```

`--dry-run` prints the target and the image references the step would apply, from the
configuration alone, and contacts nothing. It refuses a checkout at the wrong commit, as the real
run does, and only warns about uncommitted changes, which the real run refuses.

The operator step applies `N-1`'s CRDs to the cluster with a server-side apply, then runs one
`helm upgrade` on the existing release with `N-1`'s chart, re-tagging the operator image. `N-1`'s
operator then reconciles the `PlatformAgent` from `N-1`'s schema, and its first pass re-renders
the agent Deployment with `N-1`'s spec, rolling the pod wherever that spec differs, still on `N`'s
image, because the agent tag in the release's values has not moved yet. The harness step is a
second `helm upgrade` with the same chart re-tagging the agent image (on releases that ship the
shell sandbox, the sandbox image with it, and from the first release after `0.6.0` the plugin
images the release records), followed by a wait for the rollout. Operator first
because the CRD schema and the controller have to agree before the agent the controller renders
is replaced. Between the two commands `N`'s agent image runs under `N-1`'s Deployment spec,
without whatever `N`'s operator had added to it, which is why the pair is run back to back rather
than a step at a time.

Each step's `helm upgrade` waits up to ten minutes for the objects the chart renders, the
controller Deployment among them. A timeout there leaves the release `failed`, which the next run
does not un-stick on its own: the current script un-sticks a `pending-*` release alone, and
`0.4.0`'s un-sticks nothing. After Helm returns, the script first reads the gateway Deployment's
release images back against the tag and fails the step on any still on `N` (from `0.5.0`; the
current script reads plugin image volumes too), then waits with `kubectl rollout status`
for the rollouts the chart does not cover, the agent Deployment first, and those waits are
`N-1`'s: the current script gives the agent fifteen minutes in the namespace `install.env` names,
while `0.4.0`'s gives it two minutes in `kubeagents-system` whatever `NAMESPACE` says. So on a
slow image pull, or on an install in another namespace, `0.4.0`'s harness step can report a
failure after both Helm moves have succeeded. A timeout is not a signal to run the step again.
Read the rollout yourself first:

```bash
kubectl get pods -n kubeagents-system
kubectl rollout status deployment/platform-agent-gateway -n kubeagents-system
kubectl describe pod -n kubeagents-system -l app=platform-agent-gateway
```

## What the two steps change

- The Helm release: `N-1`'s chart version, two new revisions in `helm history`.
- The operator and agent images, at tag `N-1`; the sandbox image too when `N-1`'s chart has it,
  and the plugin images when `N-1`'s script is from after `0.6.0`. `0.4.0`, `0.5.0` and `0.6.0`
  all leave the plugin images on `N`'s tag. With a plugin enabled, `0.5.0`'s and `0.6.0`'s image
  check then refuses where the plugin is staged by an init container (GKE Autopilot, and Standard
  below 1.35), with both Helm moves already made; where it is mounted as an image volume the check
  does not read it and passes, and `0.4.0` has no check. In every case, finish that rollback from
  the `N-1` checkout by re-tagging the two plugin keys by hand (`--reuse-values` for `0.4.0`, to
  match its script):

  ```bash
  helm upgrade kube-agents charts/kube-agents -n kubeagents-system --reset-then-reuse-values \
    --set plugins.pubsubPlatform.image.tag=<N-1> \
    --set plugins.stockoutInvestigator.image.tag=<N-1>
  kubectl rollout status deployment/platform-agent-gateway -n kubeagents-system
  ```

- The CRD schema, now `N-1`'s.
- Every object the chart renders, including the `PlatformAgent` resource, re-rendered from
  `N-1`'s templates. Objects `N`'s chart rendered and `N-1`'s does not are deleted by the upgrade;
  that is Helm's ordinary behaviour.
- Which values those templates are rendered with depends on the script. From `0.5.0` on the
  re-tag is `helm upgrade --reset-then-reuse-values`: `N-1`'s chart defaults, with the values the
  install set on top. `0.4.0` and earlier use `--reuse-values`, which keeps every value `N`'s
  release computed, defaults included, so a chart default `N` changed stays at `N`'s value after a
  rollback to `0.4.0` even though the chart version reads `0.4.0`.

## What they leave as it is

Terraform state and every GCP resource. The Helm-only modes write nothing to state, so it keeps
recording `N` as the installed tag. `./upgrade.sh --plan` with no `--image-tag` plans at the tag
state records, so the re-tag is not in its report; run from the `N-1` checkout it still lists
every composition difference between `N-1` and `N`, which is the next section's subject. The state
and the cluster disagree on the tag until the next `--upgrade-mode=full`, which re-applies
whatever tag it is given. The `terraform.tfvars` in the `N-1` checkout is regenerated on every run
and is not a record of anything.

Secrets. The script never rewrites a Secret value that exists. A key that `N-1`'s script knows and
finds missing is generated and added, which is what a forward upgrade does too.

Objects `N`'s operator created that `N-1`'s operator does not know. The operator only reconciles
what its own release renders, so an object introduced by a later release stays in place, on `N`'s
image, unmanaged, until the next forward upgrade re-adopts it. The shell sandbox is the current
example: rolling back to a release whose chart has no `agentSandbox` values (`0.4.0` and earlier)
leaves the `platform-agent-shell` StatefulSet behind, and that release's harness step re-tags the
agent alone. It keeps running until its pod next restarts: the same Helm move deleted the
`platform-agent-shell-authorized-keys` Secret it mounts, because `0.4.0`'s chart does not render
it, so the replacement pod stays in `ContainerCreating`. That is harmless to the `0.4.0` agent,
which has no sandbox, and the next forward upgrade renders the Secret again.

Fields `N` added to the `PlatformAgent` schema. Once `N-1`'s CRD is applied, the API server prunes
them from the stored object, and `N-1`'s chart does not render them. The Helm values that produced
them stay in the release's recorded values: a later forward re-tag renders them again, the schema
refusal below turns on them, and the full mode discards them.

The agent's persistent volume, apart from what the entrypoint re-syncs from the image. The
harness step rolls the pod, and the volume follows it; what the next start does to it is `N-1`'s
entrypoint's doing, and every published release's entrypoint does the same. Skills are not kept:
on every start the entrypoint replaces the platform profile's `skills/`, and every cluster
profile's, whole from the image, so a skill `N` added is gone at the first start after the
harness step rather than staying and failing on use
([Skills](/kube-agents/concepts/skills/#importing-external-skills) says the same of an injected
skill). The platform profile's persona files, governance and hindsight configuration are
overwritten with `N-1`'s copies too, though a governance file only `N` shipped stays, because
that overlay prunes nothing; the default profile's persona files and the shared scripts are
overwritten the same way. Cron definitions are merged per job id: `N-1`'s image wins every key
it ships, the scheduler's own state stays, and a job only `N` shipped keeps its entry and keeps
firing, now against `N-1`'s tools. The rest of what the image seeds is copied with `cp -ru`,
which skips any file the volume holds that is newer, so those files stay at `N`'s copy, and the
state the agent itself wrote is kept.

## Reverting GCP resources too

```bash
./upgrade.sh --plan --image-tag <N-1>
./upgrade.sh --upgrade-mode=full --image-tag <N-1>
```

From the `N-1` checkout, the full mode applies `N-1`'s CRDs, then runs `terraform apply` on
`N-1`'s composition on top of the state `N` wrote. Its Helm half is not the re-tag: the
composition's own `helm_release` upgrades the release with `N-1`'s chart and the values `N-1`'s
composition computes from `install.env`, and it sets neither `reuse_values` nor `reset_values`,
so the values the release recorded are discarded rather than reused. Anything in them that the
composition does not render goes with them: a value set by hand with `helm upgrade --set`, a key
`N`'s composition added, and on a `0.4.0` rollback the `N` defaults the pair's `--reuse-values`
kept. The plan lists that as the `helm_release` change, next to the GCP resources. This is the
GCP-level revert: a resource `N`'s composition added is planned for destruction because `N-1`'s
composition does not declare it, and a setting `N` changed goes back. Read the whole plan first.
A plan that destroys a bucket, a KMS key or the cluster is a decision to take with what those
resources hold in front of you, not a rollback step; the composition on `N` may have added a
resource that now carries data. The plan's exit code follows Terraform's: `0` for no changes,
`2` for changes, `1` for an error.

The composition refuses some of those destructions itself: its `lifecycle.sh` exits before
`terraform apply` when the regenerated configuration disagrees with state on the cluster, the
agent's service account, the CMEK key, the release namespace, the Pub/Sub subscription or the
minter key. That refusal comes after the full mode's CRD apply, so it leaves `N-1`'s CRDs in
place, like the schema refusal below. How to read a `destroy` line in the plan, as missing
configuration first and real drift second, is in the
[installer README](https://github.com/gke-labs/kube-agents/blob/main/scripts/installer/README.md).

## Checking the result

```bash
kubectl get deployment platform-agent-gateway -n kubeagents-system \
  -o jsonpath='{.spec.template.spec.containers[?(@.name=="platform-agent")].image}{"\n"}'
kubectl get deployment kube-agents-controller-manager -n kubeagents-system \
  -o jsonpath='{.spec.template.spec.containers[*].image}{"\n"}'
kubectl get platformagent platform-agent -n kubeagents-system \
  -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}{"\n"}'
kubectl get deployment platform-agent-gateway -n kubeagents-system \
  -o jsonpath='{range .spec.template.spec.initContainers[*]}{.image}{"\n"}{end}{range .spec.template.spec.volumes[*]}{.image.reference}{"\n"}{end}'
kubectl get pods -n kubeagents-system
helm history kube-agents -n kubeagents-system
```

Both images end in `:<N-1>`, so does every plugin image the fourth command lists (the lines naming
`pubsub-platform` or `gke-stockout-investigator`), the `Ready` condition reads `True`, the gateway
pod is `Running`,
and the newest Helm revision is `deployed` at chart version `N-1`, with the operator step's
revision `superseded` just before it. `kubeagents-system` is the default namespace; an install
that set `NAMESPACE` in `install.env` uses that one. `platform-agent` is the chart's default
`platformAgent.name` value.

## When a rollback is refused

The first two refusals happen before anything on the cluster moves. The other two land in the
operator step after `N-1`'s CRDs are applied; Helm checks before it renders or applies anything,
so the release itself keeps its last revision.

- **The sources do not match the tag.** The checkout's `HEAD` is not the tag's commit, the tree
  has uncommitted changes, or the bundle's baked version is not the `--image-tag` given. Start
  again from a clean checkout or bundle of `N-1`.
- **No install configuration.** Neither `install.env` beside the script, nor
  `KUBE_AGENTS_INSTALL_ENV`, nor a legacy `k8s-operator/scripts/vars.sh` was found. Supply the
  install's own file; a fresh one written from memory re-renders the `PlatformAgent` with whatever
  it forgets.
- **`N-1`'s chart carries a values schema and `N` added a chart value.** Every release after
  `0.5.0` ships a `values.schema.json` that closes each level of the chart's values, and the
  re-tag reuses the values the release recorded, so a key `N`'s install set that `N-1`'s chart
  does not declare fails Helm's schema check. Helm checks before it renders, so the release keeps
  its last revision, but in the operator step the CRD apply has already run: put `N`'s CRDs back
  from the `N` checkout with the same command the script uses,
  `kubectl apply --server-side --force-conflicts -f charts/kube-agents/crds/`. The re-tag has no
  flag that drops a reused key, so for such a pair the Helm-only rollback does not complete. The
  full mode does, because its Helm release renders from the composition's values rather than the
  recorded ones (the section above), at the price of a GCP-level apply and the plan read that
  goes before it. Neither pair published today is affected: neither `0.4.0`'s nor `0.5.0`'s
  chart has a schema.
- **`N`'s operator owns an object that `N-1`'s chart renders.** Helm refuses to adopt an object
  that carries another manager's ownership labels (`exists and cannot be imported into the
current release: invalid ownership metadata`). The `litellm-policy` NetworkPolicy is the case
  in hand: every chart through `0.5.0` renders it, and from the first release after `0.5.0` the
  operator creates and labels it instead, so a rollback from that release to `0.5.0` or earlier
  is refused at the operator step's `helm upgrade`, with `N-1`'s CRDs already applied and the
  release still at its last revision. The way through is the chart README's
  [handoff](https://github.com/gke-labs/kube-agents/blob/main/charts/kube-agents/README.md#handing-litellm-policy-back-to-helm):
  scale `N`'s operator to zero, relabel and annotate the object for Helm, then run the two
  commands; the operator step adopts it. Put `N`'s CRDs back first if the refusal has already
  happened, as for the schema case.

A further refusal is a future one. The API server rejects a CRD whose `spec.versions` drops a
version still listed in `status.storedVersions`, so if `N` introduced a new API version of the
`PlatformAgent` CRD and objects were stored in it, the operator step fails at the CRD apply,
before anything moves. Every published release serves `v1alpha1` alone; it is here so that the
day it happens the error is recognised as the rollback boundary it is.

Two things this page does not do. `helm rollback kube-agents <revision>` reverts the chart and
values to an earlier revision without applying that revision's CRDs and without the source check,
which is why the runbook goes through `upgrade.sh`; `upgrade.sh` itself uses `helm rollback` only
to un-stick a release left in a `pending-*` state. And nothing here restores data: the agent's
volume, the Terraform state bucket and anything `N`'s agent wrote to a repository or a cluster are
where `N` left them.
