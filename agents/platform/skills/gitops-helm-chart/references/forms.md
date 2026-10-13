# Declaring a chart, by reconciler

Each block is the shape, not the file to paste. Where the repository already
declares a chart, its file wins on every detail: apiVersion, directory,
namespace, sync options, labels. Placeholders are in angle brackets.

## Repository transport

A chart is served from an HTTP repository (an `index.yaml` at a URL) or an
OCI registry (`oci://...`). Use the transport the repository's other charts
use. With none to copy, use the HTTP repository when the project's install
guide offers one: every form below accepts it without extra registration,
while Argo CD releases before 3.1 need an OCI repository registered first (a
repository Secret with `enableOCI: "true"`), and that Secret is not
something this skill writes. Write the version string exactly as the repository's index lists it,
including any leading `v`.

## Argo CD `Application`

Lives where the repository keeps its Applications, usually in the Argo CD
namespace on the hub. `destination` names the target cluster the way the
sibling Applications do (`server` URL or registered `name`, and
`https://kubernetes.default.svc` when Argo CD runs on the target itself).

When the repository holds no Applications and an Application created
outside it syncs a directory of plain YAML, the new Application goes into
that directory and the parent creates it on merge ("app of apps"). That works
only when the parent's own destination is the cluster Argo CD runs on, and
its AppProject permits `Application` objects in the Argo CD namespace (the
`default` project does): an Application object applied to any other cluster
is never read. When the
parent deploys the directory to a remote cluster, stop and ask where the
repository's Applications should live.

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: <chart>-<cluster>
  namespace: argocd
spec:
  project: <project the siblings use>
  source:
    repoURL: <https chart repository URL>
    chart: <chart>
    targetRevision: <exact version>
    helm:
      releaseName: <chart>
      valuesObject:
        <key>: <value>
  destination:
    name: <cluster>
    namespace: <release namespace>
  syncPolicy:
    syncOptions:
      - CreateNamespace=true
```

An `ApplicationSet` that already generates one Application per cluster is
extended instead: add the chart as its own ApplicationSet with the same
generator, or add the cluster to the generator's selection, whichever the
repository does for its other charts.

## Flux `HelmRelease`

A source object plus the release, in the directory the cluster's Flux
`Kustomization` syncs. Reuse an existing `HelmRepository` for the same URL
rather than declaring a second one.

```yaml
apiVersion: source.toolkit.fluxcd.io/v1
kind: HelmRepository
metadata:
  name: <repository name>
  namespace: flux-system
spec:
  interval: 1h
  url: <https chart repository URL>
---
apiVersion: helm.toolkit.fluxcd.io/v2
kind: HelmRelease
metadata:
  name: <chart>
  namespace: flux-system
spec:
  interval: 1h
  targetNamespace: <release namespace>
  releaseName: <chart>
  chart:
    spec:
      chart: <chart>
      version: <exact version>
      sourceRef:
        kind: HelmRepository
        name: <repository name>
        namespace: flux-system
  install:
    createNamespace: true
  values:
    <key>: <value>
```

Older Flux installs serve `helm.toolkit.fluxcd.io/v2beta1` or `v2beta2` and
`source.toolkit.fluxcd.io/v1beta2`; use the apiVersion the siblings use and
check fields with `kubectl explain`. A Flux `Kustomization` does not render
kustomize `helmCharts`; on a Flux repository the chart is a `HelmRelease`.

## Config Sync `RootSync`

A Helm-sourced `RootSync` syncs one chart. It is declared in the repository
the cluster's root `RootSync` already syncs, so merging it creates the new
sync.

```yaml
apiVersion: configsync.gke.io/v1beta1
kind: RootSync
metadata:
  name: <chart>
  namespace: config-management-system
spec:
  sourceType: helm
  sourceFormat: unstructured
  helm:
    repo: <chart repository URL>
    chart: <chart>
    version: <exact version>
    releaseName: <chart>
    namespace: <release namespace>
    includeCRDs: true
    auth: none
    values:
      <key>: <value>
```

`includeCRDs: true` lets through the CRDs in a chart's `crds/` directory;
without it Config Sync drops them. A chart that templates its CRDs behind a
value (cert-manager's `crds.enabled`) needs that value as well, and
`includeCRDs` alone installs none of them. Config Sync renders the chart
without skipping hooks, so a chart's hook manifests (a post-install test
Job, say) are applied as ordinary objects; read the chart's `templates/` for
`helm.sh/hook` annotations and say in the PR body what they will do. Add a `Namespace` manifest for the release namespace
in the root repository when the siblings do.

A `RepoSync` is not a smaller `RootSync`. It applies only objects in its own
namespace, so it cannot install CRDs or anything cluster-scoped, which rules
out most operators; it has no `helm.namespace`; and its reconciler needs a
`RoleBinding` in that namespace. Use one only for a chart with no CRDs and
nothing cluster-scoped, and only where the siblings already use `RepoSync`.

## kustomize `helmCharts`

Only where the repository already uses `helmCharts`: the build that reads it
needs Helm inflation turned on (Argo CD's kustomize build options, or Config
Sync, which renders it), and plain `kubectl apply -k` cannot render it.

```yaml
# kustomization.yaml in the cluster's synced directory
helmCharts:
  - name: <chart>
    repo: <https chart repository URL>
    version: <exact version>
    releaseName: <chart>
    namespace: <release namespace>
    includeCRDs: true
    valuesInline:
      <key>: <value>
```
