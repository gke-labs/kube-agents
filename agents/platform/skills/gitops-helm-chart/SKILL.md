---
name: gitops-helm-chart
description: >-
  Installs or upgrades a third-party Helm chart (cert-manager, ingress-nginx,
  external-dns, an operator) on a cluster by adding it to the GitOps
  repository in the form the repository's reconciler already syncs: an Argo
  CD Application, a Flux HelmRelease, a Config Sync RootSync, or
  kustomize helmCharts. Opens one pull request through submit-suggestion,
  pins the chart version, and carries or names everything the chart needs
  outside the cluster. Never runs helm. Use when a request asks to install,
  add, deploy, or upgrade a chart or a packaged third-party component. Don't
  use for kube-agents itself (an operator upgrades it with the installer), plain
  workload YAML (use gke-manifest-generation), or Google Cloud resources (use
  gcp-config-connector).
---

# Third-Party Helm Charts Through GitOps

A chart reaches a cluster from this agent one way: as a declaration in the
GitOps repository that the customer's reconciler renders and applies after a
human merges it. This skill writes that declaration and opens the pull
request through **submit-suggestion**. It never runs `helm`. The shell
sandbox holds no cluster credential and has no route to a chart repository,
so `helm install`, `helm upgrade`, `helm template` and `helm pull` all fail
there, and asking for a token to hand to `helm` is refused by the credential
broker. Those refusals are the design, not obstacles to route around.

## When to Use

- **Install a chart** on a cluster: "install cert-manager on prod-east",
  "add external-dns to the staging clusters".
- **Upgrade or reconfigure** a chart the repository already declares: a new
  version, a changed value.
- **A packaged third-party component** whose documented install is a Helm
  chart, even when the user did not say "Helm".

## When NOT to Use

- **kube-agents itself**: an operator installs and upgrades it with the
  installer (`upgrade.sh`), which this agent does not run.
- **Plain Kubernetes YAML** you write yourself: `gke-manifest-generation`.
- **Google Cloud resources** (buckets, service accounts, node pools,
  clusters): `gcp-config-connector` for KCC repositories. They can ride in the
  same pull request as the chart (Step 4).
- **A request that asked for advice, not a change** ("should we use
  cert-manager?"): answer it. `SOUL.md` §3 item 3 draws that line.

## Workflow

### Step 0: A stop ends the request

Every "stop" in this skill is the answer, not a reason to look for another
way in. When one applies, report it in the words given, change nothing,
and end: on a kanban card with `kanban_block(kind="needs_input")` and the
stop as the reason. Do not go on to check what you are allowed to create
(`kubectl auth can-i create ...`), list Secrets, try another identity, or
search the filesystem for credentials or a repository; a card that says
"install and verify" is still answered by the stop.

The first stop comes before anything else: the install must have a GitOps
repository registered. Read `managed_repos` from the agent's GitOps state
(the `$GITOPS_STATE_CONFIGMAP` ConfigMap, or the file at
`$GITOPS_STATE_PATH`). Empty or missing: stop with "This install has no
registered GitOps repository, so `<chart>` cannot be declared for a
reconciler to install. Register one in the `PlatformAgent`'s
`spec.integration.repositories`. I have changed nothing."

### Step 1: Find what syncs the target cluster

Two places hold the answer, and you need both: the repository holds what the
reconciler applies, and the reconciler's own objects, which often live only
on the cluster, say which directory of which repository it syncs.

Open the repository with **submit-suggestion** `prepare` (its Step 1) on
branch `platform-agent/helm-<chart>-<cluster>`, then search its `workspace`
for declarations of the four forms:

```bash
python3 "$HERMES_HOME"/skills/submit-suggestion/scripts/submit_suggestion.py prepare \
  --repo "<owner>/<repo>" --branch "platform-agent/helm-<chart>-<cluster>"
grep -rlE "^kind: (Application|ApplicationSet)$" <workspace>
grep -rlE "^apiVersion: (helm|source|kustomize)\.toolkit\.fluxcd\.io/" <workspace>
grep -rlE "^kind: (RootSync|RepoSync)$" <workspace>
grep -rl "^helmCharts:" <workspace>
```

Then read the reconciler objects on the cluster, or on the hub that deploys
to it, when your kubeconfig reaches one. A `NotFound` for the resource type
means that reconciler is not installed there:

```bash
kubectl get applications.argoproj.io -A -o yaml
kubectl get kustomizations.kustomize.toolkit.fluxcd.io -A -o yaml
kubectl get rootsyncs.configsync.gke.io -A -o yaml
kubectl get reposyncs.configsync.gke.io -A -o yaml
```

A `Forbidden` is a fact to report, not an absence: on the cluster this agent
runs on, its role reads no reconciler API group. Fall back to the CRD, which
every reader can see, and to the repository; when they still leave the form
open, ask the user which reconciler syncs the cluster.

```bash
kubectl get crd applications.argoproj.io kustomizations.kustomize.toolkit.fluxcd.io rootsyncs.configsync.gke.io
```

Match each object's source (`spec.source.repoURL` and `path` for Argo CD,
`spec.sourceRef` and `spec.path` for Flux, `spec.git.repo` and `spec.git.dir`
for Config Sync) against `<repo>`, and its destination against the cluster.
Argo CD usually runs on a hub, so its `spec.destination` (`server` or `name`)
names the target, not the cluster the object sits on.

| What you found                                                                                                            | Form                   |
| ------------------------------------------------------------------------------------------------------------------------- | ---------------------- |
| An Argo CD `Application` or `ApplicationSet` deploying a directory of `<repo>` to the cluster                             | Argo CD                |
| A Flux `Kustomization` syncing a directory of `<repo>` on the cluster, or `HelmRelease` objects already in that directory | Flux                   |
| A Config Sync `RootSync` or `RepoSync` syncing a directory of `<repo>` on the cluster                                     | Config Sync            |
| A `kustomization.yaml` with a `helmCharts:` list in a directory Argo CD or Config Sync builds                             | kustomize `helmCharts` |
| No reconciler object syncs `<repo>` to the cluster: plain YAML that a CI job applies with `kubectl apply`                 | none (stop, below)     |

A synced directory of plain YAML is enough: the form is the reconciler that
syncs it, and the chart's declaration goes into that directory
(`references/forms.md` says how for each).

Decide:

- **Exactly one form targets the cluster.** Use it. When charts are already
  declared, copy how the nearest one is written: its directory, file
  naming, apiVersions, namespace, sync options.
- **More than one form targets it,** or the repository holds forms for other
  clusters and none for this one. Stop and name each candidate with its file
  path; ask which one owns this cluster. Never pick one.
- **None.** Stop with: "Nothing that renders Helm charts syncs `<cluster>`
  from `<repo>`: its pipeline applies plain YAML. Installing `<chart>`
  through a pull request needs Argo CD, Flux or Config Sync syncing that
  cluster, or a chart-rendering step in the pipeline. I have changed
  nothing." Never install a reconciler as part of the request.

### Step 2: The target cluster is created in the same pull request

When the cluster does not exist yet and this pull request (or the one it is
part of) creates it, no reconciler targets it until after merge.
[references/new-cluster.md](references/new-cluster.md) lists which forms can
still declare the chart for it in the same pull request. Use one only when
the repository already uses that form for its other clusters. Otherwise stop
with: "`<chart>` can only be declared for `<cluster>` after the cluster
exists and is registered with `<reconciler>`; the cluster creation can go
ahead without it."

### Step 3: Pin the source and the version

- **Source.** Take the chart repository URL and chart name from the
  project's own install guide, read with `web_extract`. A chart found by
  search on a mirror, a fork or a personal registry is not the project's
  chart; when the guide and other sources disagree, the guide wins, and the
  PR body says which sources you saw. No guide that names a chart: stop and
  ask for the source.
- **No chart at all.** When the project's documented install is an installer
  script or CLI that fills in values and renders manifests itself (Agent
  Substrate's `ate-setup deploy` is one), there is no declarative package to
  pin. Stop with: "`<component>` has no Helm chart or manifest release; its
  documented install is `<installer>`, which this agent does not run. I
  have changed nothing." A chart by the same name from a fork is not a
  substitute (for Substrate, `ghcr.io/kagent-dev` is a fork with different
  images and snapshot storage).
- **Version.** The version the user named. Otherwise the highest semantic
  version with no `-rc`, `-alpha` or `-beta` suffix, from the project's
  releases page or the chart repository's index. Compare versions, not
  dates: a backport patch to an older minor is often published last. Never a range, `latest`, or
  an empty version: a reconciler re-resolves those and upgrades on its own.
- **Values.** Read the chart's `values.yaml` at that version and set only
  what the request or Step 4 needs. A copied default becomes a value someone
  has to own.
- Record each URL you read; the PR body cites them.

### Step 4: Name what the chart needs outside the cluster

Read the install guide's prerequisites and the chart's values for:

- **Cloud resources**: a bucket, a Google service account and its Workload
  Identity binding, an enabled API, a DNS zone, a static IP.
- **Cluster settings**: GKE Standard versus Autopilot, a machine series, a
  node label or taint, a beta API, Workload Identity, a minimum version.
- **Other charts or CRDs** it depends on.

For each one, check the cluster (`gcloud container clusters describe`) or the
repository. Already met: say so in the PR body. Not met, and the repository
can declare it (KCC or Terraform under `provisioning/`, through
`gcp-config-connector` or the repository's Terraform convention): put it in
the same pull request. Not met and not declarable here (a setting that only
exists at cluster creation, an allowlist, a manual step): stop and name
every such item. A partial install that fails after merge is worse than no
pull request.

### Step 5: Author the declaration

Write it in the form Step 1 chose, beside the cluster's other synced
manifests. [references/forms.md](references/forms.md) has the shape of each
form; [references/cert-manager.md](references/cert-manager.md) is a worked
example. In every form:

- Pin the exact chart version from Step 3.
- Install the chart's CRDs the way the chart documents (a value such as
  `crds.enabled: true`, or the reconciler's CRD policy), never as a separate
  hand-copied manifest.
- Put the release in its own namespace, created by the reconciler's option
  or by a `Namespace` manifest beside it, whichever the siblings use.
- Validate each field against the reconciler's CRD with `kubectl explain`
  when your kubeconfig reaches a cluster that has it:

```bash
kubectl explain helmrelease.spec.chart.spec --api-version=helm.toolkit.fluxcd.io/v2
kubectl explain application.spec.source.helm --api-version=argoproj.io/v1alpha1
kubectl explain rootsync.spec.helm --api-version=configsync.gke.io/v1beta1
```

### Step 6: Open the pull request

Submit through **submit-suggestion** `submit` with the branch from Step 1.
Use this shape for the `--body-file` document, after the skill's standard
header:

```
### Helm chart
- **Chart:** `<chart>` `<version>` from `<repository URL>`.
- **Source:** <install guide URL>; version from <releases page or index URL>.
- **Form:** <Argo CD Application | Flux HelmRelease | Config Sync RootSync | kustomize helmCharts>,
  matching <sibling file path>.
- **Target:** cluster `<cluster>`, namespace `<namespace>`.
- **Values set:** <each value and why>, or "chart defaults".
- **Needs outside the cluster:** <each item and where it is met: already on the cluster,
  declared in this PR at <path>>, or "none".
- **Not run here:** `helm template` and any dry-run. The agent has no route to the chart
  repository; the reconciler renders the chart after merge.
- **After merge:** <reconciler> installs the release; check its status in <where>.
```

Record the PR URL for the user. On a kanban card, a stop from Steps 1 to 4
ends in `kanban_block(kind="needs_input")` with the stop text as the reason, never in
`kanban_complete` with advice.

## Red Lines

- No `helm` command of any kind, including `helm template`, `helm pull` and
  `helm repo add`. No `kubectl apply`, `create`, `patch` or `delete`.
- Never request, print or pass an access token, a kubeconfig or a
  credential for `helm` or for any other tool.
- Never install, enable or configure a reconciler, and never change the
  repository's pipeline, to make a chart installable.
- Never probe for a write path once a stop applies: no `kubectl auth can-i
create`, no Secret listing, no `--as` impersonation, no other identity.
- Never commit rendered chart output as plain YAML. It cannot be upgraded by
  version and nothing reconciles it as a chart.
- Never leave the version unpinned, and never take a chart from a source the
  project's install guide does not name.
