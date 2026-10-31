# Worked example: cert-manager

cert-manager needs nothing outside the cluster on a public GKE Standard
cluster, which makes it the first chart to try this skill on.

## Source and version

- Install guide: `https://cert-manager.io/docs/installation/helm/`. It names
  two repositories for the same chart: the OCI registry
  `oci://quay.io/jetstack/charts/cert-manager` (published first) and the HTTP
  repository `https://charts.jetstack.io` (updated a few hours later). Both
  are the project's own; follow `forms.md` "Repository transport" to choose.
- Version: the newest release without a pre-release suffix, from the
  cert-manager install guide or the `release-<minor>` branch's chart
  (**inspect-repository** on `cert-manager/cert-manager`). Chart versions
  carry the `v`: `version: v1.21.2`, not `1.21.2`.
- Values: the chart's `values.yaml`, under the deploy/charts directory of the
  `cert-manager/cert-manager` repository at that release branch, read with
  **inspect-repository**.

## Values

- `crds.enabled: true`. Without it the chart installs no CRDs and the
  controller has nothing to serve. The CRDs are templates behind this value,
  not files in the chart's `crds/` directory, so a Config Sync `RootSync`
  needs the value too; its `includeCRDs` alone installs none of them.
  `installCRDs` is the deprecated spelling; never set both.
- `startupapicheck` is a `post-install` hook Job (`startupapicheck.jobAnnotations`).
  Argo CD runs it as a hook and Flux through Helm. Config Sync applies it as
  an ordinary Job that it then keeps in sync. On a Config Sync repository,
  say so in the PR body, or set `startupapicheck.enabled: false` when the
  repository's other charts turn their hook Jobs off.
- `crds.keep` stays at its default (`true`). It marks the CRDs with Helm's
  `helm.sh/resource-policy: keep`, so removing the release later does not
  take every Certificate in the cluster with them, on a reconciler that
  honours the annotation.

## Needs outside the cluster (Step 4)

| Check                       | Where to read it                                               | When it applies                                                                                                                                                                           |
| --------------------------- | -------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Autopilot                   | `gcloud container clusters describe` field `autopilot.enabled` | Autopilot does not allow writes to `kube-system`, where the chart's leader election lease defaults. Set `global.leaderElection.namespace: <release namespace>`; no cloud change.          |
| Private cluster webhook     | `privateClusterConfig` in the same `describe`                  | The control plane reaches nodes on 443 and 10250 by default, and the chart's webhook listens on 10250 (`webhook.securePort`). Leave that value alone and no firewall rule is needed.      |
| DNS01 solver with Cloud DNS | the request                                                    | Only when the user asks for an issuer that solves DNS01 on Cloud DNS: a Google service account with `roles/dns.admin` and a Workload Identity binding. Out of scope for the bare install. |

```bash
gcloud container clusters describe <cluster> --location <location> --project <project> --format=json
```

## Flux example

```yaml
apiVersion: source.toolkit.fluxcd.io/v1
kind: HelmRepository
metadata:
  name: jetstack
  namespace: flux-system
spec:
  interval: 1h
  url: https://charts.jetstack.io
---
apiVersion: helm.toolkit.fluxcd.io/v2
kind: HelmRelease
metadata:
  name: cert-manager
  namespace: flux-system
spec:
  interval: 1h
  targetNamespace: cert-manager
  releaseName: cert-manager
  chart:
    spec:
      chart: cert-manager
      version: v1.21.2
      sourceRef:
        kind: HelmRepository
        name: jetstack
        namespace: flux-system
  install:
    createNamespace: true
  values:
    crds:
      enabled: true
```

The version above is the one current when this example was written; Step 3
looks it up again for every pull request.
