# Reference GitOps repository layout

This is the **template customers fork** as their kube-agents GitOps repo — the single source of truth
for desired state (`05` C13). It is separate from the kube-agents source tree; agents check it out
dynamically into the agent workspace and `submit-suggestion` opens PRs against it. Layout defined in
[`docs/architecture/06-api-and-data-contracts.md` §3](../../docs/architecture/06-api-and-data-contracts.md).

```
gitops-repo/
├── clusters/<cluster>/            # per-cluster desired state (applied by that target's pipeline)
│   ├── provisioning/              # cloud/cluster resources: KCC YAML or Terraform HCL
│   ├── namespaces/<ns>/           # Namespace, RBAC, NetworkPolicy, ResourceQuota, workloads
│   └── agents/                    # Agent CRs + per-agent identity (KSA/RBAC/WI) manifests
├── fleet/                         # project-level policy; platform-tier Agent CR + identity
├── knowledge/                     # OKF base (§5) — never applied to a cluster
├── policy/                        # admission policies (ValidatingAdmissionPolicy; Gatekeeper/Kyverno)
├── .kube-agents/intent.yaml       # where the declaring audits look for declared-intent notes
└── .github/workflows/             # the actuation pipeline config (customer's CI/CD)
```

## Contracts

- **Propose** (`submit-suggestion`): branch `<tier>-agent/<change_type>-<target>` → stage only
  targeted files (never `git add .`) → Conventional Commit → PR.
- **Apply:** on merge, the **customer's CI/CD** applies changed paths — `kubectl apply` for K8s/KCC
  YAML, `terraform apply` for HCL. kube-agents never calls cluster/cloud APIs directly.
- **Third-party Helm charts** are declared, never installed: the Platform Agent's
  [`gitops-helm-chart` skill](../../agents/platform/skills/gitops-helm-chart/SKILL.md) writes an Argo CD
  `Application`, a Flux `HelmRelease`, a Config Sync `RootSync` or kustomize `helmCharts`, whichever
  already syncs the target cluster. A pipeline that only runs `kubectl apply`, like the one sketched
  here, renders no chart, so on it the agent stops and says so instead of opening a pull request.
- **Review gate:** PRs touching `**/provisioning/**`, `**/agents/**`, `**/namespaces/**`,
  `**/policy/**`, `knowledge/**` and `.kube-agents/**` require human review (see `CODEOWNERS.example` —
  copy to `CODEOWNERS` and fill in real teams when forking) + the security review gate (06 §7).
  `knowledge/` is in the gate because a note there can move an audit posture off the ledger
  (next bullet); a declaration is a reviewed change, not a comment. `.kube-agents/` is in it
  because `intent.yaml` decides which paths' notes can do that, and removing the file opens the
  whole repository to them, so the bound carries the same review as the notes it bounds.
- **Declared intent:** the declaring audit streams read `knowledge/` — and the obtainability
  stream `clusters/<cluster>/provisioning/` as well — before they report a posture an owner may
  have chosen: a fixed replica count,
  a pinned HPA or a missing PodDisruptionBudget (`agents/platform/governance/obtainability_audit_sop.md` §4a),
  a namespace with no NetworkPolicy or a workload on the default ServiceAccount's token
  (`compliance_audit_sop.md` §3a, object `Namespace/<ns>` or the workload's `Kind/name`),
  a cluster off its release channel, without a maintenance window or upgrade notifications, under a
  change freeze, or a node pool with auto-upgrade or auto-repair off
  (`security_patch_orchestrator_sop.md` §4a, object `Cluster/<name>` or `NodePool/<pool>`, and `namespace: ""` written out, as that SOP's
  §4a spells it), a
  reservation the waste audit would report: headroom above a workload's peak, a kept volume,
  disk or address, warm node capacity, an idle namespace or standby, a registry with no cleanup
  policy (`fleet_wide_cost_analysis_sop.md` §3a, object as the finding names it, and `namespace`
  empty for a node pool, an idle namespace and the project-scoped disk, address and registry
  repository; a namespace written on any of those five is read as empty; a cost declaration
  covers the object at any size, and the Declared intent row shows the size the collector measured).
  A choice HCL cannot express — a workload meant to run one replica — goes in an OKF document
  (`type` frontmatter, 06 §5) under `knowledge/` as a `declares:` list in the frontmatter, one item
  per posture with `check` (the slug, `single-replica`), `namespace`, `object` as `Kind/name`, and
  `cluster` when the choice is one cluster's rather than fleet-wide, spelled as the qualified
  `<project>/<location>/<name>` every stream's findings carry, never the bare name; the audit reads the
  frontmatter, never the prose, and lists a match under _Declared intent_ with the file's path
  instead of reporting it. `.kube-agents/intent.yaml` names the paths the audit reads for such
  notes (`knowledge/` here); without it, or when a named path has nothing behind it, the whole
  repository is read, and in content mode copied whole under the audit's file and byte caps, so naming paths is what keeps a large repository
  searchable. A Terraform repository that
  is not this one is registered under the `context_repos` key of the agent's `gitops-state`
  ConfigMap, optionally pinned to a branch with `ref`, and is read the same way, never written to.
- **Version pins:** kube-agents artifacts referenced from this repo are pinned to immutable SemVer
  releases — Terraform modules via
  `git::https://github.com/gke-labs/kube-agents.git//terraform/modules/<name>?ref=1.2.0`, the Helm
  chart via `oci://ghcr.io/gke-labs/kube-agents/charts/kube-agents --version 1.2.0`, container
  images via `1.2.0` tags — never `:latest` or a branch ref. Canonical rules:
  [release versioning & promotion](../../docs/site/src/content/docs/deploy/release-versioning.md).

## Reconciler

The pipeline above applies YAML with `kubectl apply`, which renders no Helm chart, for a new cluster
or an existing one. So on a repository that keeps its in-cluster state as YAML, a pull request cannot
carry a third-party chart. (A Terraform repository can, with a `helm_release`, for a cluster that
already exists; see the end of this section for a cluster created in the same apply.) When you need charts, or workloads on a cluster created
in the same pull request, add a reconciler that syncs the repository. The one this layout recommends
is **Config Sync on every cluster, enrolled through Config Connector**: it renders Helm charts, and it
can take on a cluster created in the same pull request, because every object that enrolls the cluster
names it by values known before it exists (project, location and cluster name). One gap today: the
fleet audit traces a chart workload to its declaration only through an Argo CD `Application` or a
Flux `HelmRelease`, so on this layout its findings on chart workloads are `manual` rather than a
pull request until it also reads `RootSync`s
([#3072](https://github.com/gke-labs/kube-agents/issues/3072)).

Config Sync then applies each cluster's `clusters/<cluster>/namespaces/`, and the pipeline keeps
applying everything else (`provisioning/` and `fleet/` where Config Connector runs, `agents/` and
`policy/` as before). Narrow the pipeline's paths to match, so no object has two appliers.

Once per fleet, in `fleet/`, turn the feature on:

```yaml
apiVersion: gkehub.cnrm.cloud.google.com/v1beta1
kind: GKEHubFeature
metadata:
  name: configmanagement
  annotations:
    cnrm.cloud.google.com/deletion-policy: abandon
spec:
  projectRef:
    external: projects/PROJECT_ID
  location: global
  resourceID: configmanagement # must be exactly this
```

Per cluster, in `clusters/<cluster>/provisioning/` beside its `ContainerCluster`, register it to the
fleet and point Config Sync at the cluster's `namespaces/` directory:

```yaml
apiVersion: gkehub.cnrm.cloud.google.com/v1beta1
kind: GKEHubMembership
metadata:
  name: CLUSTER_NAME
  annotations:
    cnrm.cloud.google.com/project-id: PROJECT_ID
    cnrm.cloud.google.com/deletion-policy: abandon
spec:
  location: global
  authority: # fleet Workload Identity; the issuer uses the cluster's own location
    issuer: https://container.googleapis.com/v1/projects/PROJECT_ID/locations/LOCATION/clusters/CLUSTER_NAME
  endpoint:
    gkeCluster:
      resourceRef:
        name: CLUSTER_NAME # the ContainerCluster in this directory
---
apiVersion: gkehub.cnrm.cloud.google.com/v1beta1
kind: GKEHubFeatureMembership
metadata:
  name: CLUSTER_NAME-configsync
  annotations:
    cnrm.cloud.google.com/deletion-policy: abandon
spec:
  projectRef:
    external: projects/PROJECT_ID
  location: global
  membershipLocation: global
  membershipRef:
    name: CLUSTER_NAME
  featureRef:
    name: configmanagement
  configmanagement:
    configSync:
      sourceFormat: unstructured
      git:
        syncRepo: https://github.com/ORG/REPO
        syncBranch: main
        policyDir: clusters/CLUSTER_NAME/namespaces
        secretType: token # or ssh; none for a public repo
```

`policyDir` is `namespaces/`, not the cluster's whole directory: Config Sync applies every YAML file
under it, and `provisioning/` holds Config Connector objects the cluster itself cannot apply.

The same-pull-request property holds end to end for a public repository (`secretType: none`), or for
a source the cluster reaches with its own Google identity (`gcpserviceaccount` or `gcenode`, such as a
repository on Google Cloud). A private GitHub repository needs `token` or `ssh`, and Config Sync reads
that credential from a Secret in the new cluster's `config-management-system` namespace, which exists
only once Config Sync is installed there. Create it after the cluster comes up, never commit it; the
cluster's first sync waits for it.

A chart then goes in the cluster's `namespaces/config-management-system/` directory as a `RootSync`
with a Helm source. Config Sync supports a `RootSync` declared in a repository another `RootSync`
syncs. Unless its `spec.override.roleRefs` names narrower roles, each `RootSync` gets a reconciler
bound to `cluster-admin` that applies whatever its source holds, so it is reviewed like
`provisioning/`: `CODEOWNERS.example` routes that directory to
platform admins. The directory does not confine it, though: an object's namespace comes from its
metadata, so a `RootSync` or `RepoSync` committed under any other `namespaces/<ns>/` is applied just
the same. Review must reject one anywhere else: the `/clusters/*/namespaces/` entry in
`CODEOWNERS.example` tells its reviewers so, and no automated check enforces it yet
([#3163](https://github.com/gke-labs/kube-agents/issues/3163)).

```yaml
apiVersion: configsync.gke.io/v1beta1
kind: RootSync
metadata:
  name: cert-manager
  namespace: config-management-system
spec:
  sourceType: helm
  sourceFormat: unstructured
  helm:
    repo: https://charts.jetstack.io
    chart: cert-manager
    version: CHART_VERSION # pin it
    releaseName: cert-manager
    namespace: cert-manager
    auth: none # a public chart
    values:
      crds:
        enabled: true # chart 1.15 and later; earlier charts call it installCRDs: true
```

Config Sync renders the chart with `helm template`, so a chart's hook and test manifests are applied
as ordinary objects, without Helm's hook ordering or deletion policy. A chart that keeps its CRDs in a
`crds/` directory instead needs `includeCRDs: true`. A Terraform repository
reaches the same result with the cluster's `fleet` block and a `google_gke_hub_feature_membership`,
the pattern [Google's Config Sync install guide](https://docs.cloud.google.com/kubernetes-engine/config-sync/docs/how-to/installing-config-sync)
documents.

Two other reconcilers can also target a cluster created in the same pull request:

- **Flux 2.7 or later**, hub and spoke: a `ConfigMap` with `provider: gcp` and
  `cluster: projects/PROJECT_ID/locations/LOCATION/clusters/CLUSTER_NAME`, referenced from the
  `Kustomization` or `HelmRelease` with `spec.kubeConfig.configMapRef`. Flux looks the endpoint and CA
  up after the cluster exists, and uses the IP endpoint.
- **Argo CD through Connect Gateway**: see
  [GitOps with Argo CD](../../docs/site/src/content/docs/deploy/gitops-argocd.md#a-cluster-created-in-the-same-pull-request).

Argo CD clusters registered by endpoint and CA cannot: the Secret needs the endpoint and CA, which
exist only after the cluster does. A Terraform `helm` provider configured from a cluster created in
the same apply works only with care: the provider reads values unknown at plan time, and
[HashiCorp's helm provider docs](https://registry.terraform.io/providers/hashicorp/helm/latest/docs)
recommend separate applies for the cluster and the charts.

`cluster-a/` and its `namespaces/team-x/` are illustrative scaffolding, not a live target.
