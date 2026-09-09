# ContainerNodePool and ContainerCluster

`apiVersion: container.cnrm.cloud.google.com/v1beta1`. Project comes from
the `cnrm.cloud.google.com/project-id` annotation (resource or namespace);
neither kind has a `projectRef`. Immutable lists below are from the
reference pages as published in September 2026; `kubectl explain` on the
customer's CRDs decides.

## ContainerNodePool

The common request: grow a pool, turn on autoscaling, change its bounds.

### Fields

- **Required:** `spec.location`, `spec.clusterRef`.
- **`clusterRef`:** `name:` when the cluster is a `ContainerCluster` object
  in the repository, otherwise
  `external: projects/<project>/locations/<location>/clusters/<cluster>`.
- **Size:** `spec.nodeCount` for a fixed pool, `spec.autoscaling` for an
  autoscaled one. Not both: with autoscaling on, a stated `nodeCount` is
  re-asserted on every reconcile against whatever the autoscaler chose.
  Inside `autoscaling`, `minNodeCount`/`maxNodeCount` are per zone and
  `totalMinNodeCount`/`totalMaxNodeCount` are pool-wide; the two pairs are
  mutually exclusive. `locationPolicy` is `BALANCED` or `ANY`.
- **Immutable (create-only):** `location`, `resourceID`, `namePrefix`,
  `initialNodeCount`, `queuedProvisioning`, and under `nodeConfig`:
  `machineType`, `diskSizeGb`, `diskType`, `spot`, `preemptible`,
  `oauthScopes`, `labels`, `metadata`, `minCpuPlatform`, `localSsdCount`,
  `guestAccelerator`, `shieldedInstanceConfig`, `reservationAffinity`,
  `bootDiskKMSCryptoKeyRef`, `confidentialNodes`, `advancedMachineFeatures`,
  `gvnic`, `sandboxConfig`, `soleTenantConfig`, `nodeGroupRef`,
  `ephemeralStorageConfig`, `ephemeralStorageLocalSsdConfig`,
  `hostMaintenancePolicy`. `networkConfig.podRange`,
  `networkConfig.podIpv4CidrBlock` and `networkConfig.createPodRange` cannot
  change after creation either.
- **Mutable:** `nodeCount`, `autoscaling`, `nodeLocations`, `version`,
  `management.autoRepair`/`autoUpgrade`, `upgradeSettings`,
  `nodeConfig.imageType`, `nodeConfig.taint`, `nodeConfig.tags`,
  `nodeConfig.workloadMetadataConfig`, `nodeConfig.kubeletConfig`,
  `nodeConfig.linuxNodeConfig`, `maxPodsPerNode` (check with `explain`).

### `describe` output to spec

From `gcloud container node-pools describe ... --format=json`:

| `describe` field                                      | spec path                                              | Note                                       |
| ----------------------------------------------------- | ------------------------------------------------------ | ------------------------------------------ |
| `name`                                                | `metadata.name`, `spec.resourceID`                     |                                            |
| (the `--cluster` and `--location` you passed)         | `spec.clusterRef.external`, `spec.location`            | immutable                                  |
| `locations[]`                                         | `spec.nodeLocations`                                   |                                            |
| `autoscaling.{minNodeCount,maxNodeCount}`             | `spec.autoscaling.{minNodeCount,maxNodeCount}`         | only when `autoscaling.enabled` is true    |
| `autoscaling.{totalMinNodeCount,totalMaxNodeCount}`   | `spec.autoscaling.{totalMinNodeCount,totalMaxNodeCount}` | exclusive with the per-zone pair         |
| `autoscaling.locationPolicy`                          | `spec.autoscaling.locationPolicy`                      |                                            |
| `initialNodeCount`                                    | omit                                                   | immutable; describes creation only         |
| `config.machineType`, `config.diskSizeGb`, `config.diskType` | `spec.nodeConfig.{machineType,diskSizeGb,diskType}` | immutable; copy verbatim               |
| `config.spot`, `config.preemptible`                   | `spec.nodeConfig.{spot,preemptible}`                   | immutable                                  |
| `config.serviceAccount`                               | `spec.nodeConfig.serviceAccountRef.external`           |                                            |
| `config.oauthScopes[]`                                | `spec.nodeConfig.oauthScopes`                          | immutable                                  |
| `config.labels`, `config.metadata`                    | `spec.nodeConfig.{labels,metadata}`                    | immutable                                  |
| `config.taints[]`                                     | `spec.nodeConfig.taint[]` (`key`, `value`, `effect`)   |                                            |
| `config.imageType`                                    | `spec.nodeConfig.imageType`                            |                                            |
| `management.{autoRepair,autoUpgrade}`                 | `spec.management.{autoRepair,autoUpgrade}`             |                                            |
| `upgradeSettings.{maxSurge,maxUnavailable}`           | `spec.upgradeSettings.{maxSurge,maxUnavailable}`       |                                            |
| `maxPodsConstraint.maxPodsPerNode`                    | `spec.maxPodsPerNode`                                  |                                            |
| `version`                                             | `spec.version`                                         | state only when the request pins it        |
| `status`, `selfLink`, `instanceGroupUrls`, `podIpv4CidrSize` | not spec                                        |                                            |

### Create: an autoscaled pool on a cluster the repo already describes

```yaml
apiVersion: container.cnrm.cloud.google.com/v1beta1
kind: ContainerNodePool
metadata:
  name: batch-pool
  namespace: infra-prod
  annotations:
    cnrm.cloud.google.com/deletion-policy: abandon
    cnrm.cloud.google.com/state-into-spec: absent
spec:
  location: us-central1
  clusterRef:
    name: prod-cluster
  initialNodeCount: 1
  autoscaling:
    totalMinNodeCount: 0
    totalMaxNodeCount: 12
    locationPolicy: ANY
  nodeConfig:
    machineType: n2-standard-8
    diskSizeGb: 100
    diskType: pd-balanced
    spot: true
    oauthScopes:
      - https://www.googleapis.com/auth/cloud-platform
    workloadMetadataConfig:
      mode: GKE_METADATA
  management:
    autoRepair: true
    autoUpgrade: true
```

### Acquire: raise the ceiling on a pool that exists in GKE but not in the repo

`describe` reported `config.machineType: e2-standard-4`,
`config.diskSizeGb: 100`, `config.diskType: pd-balanced`, autoscaling
`minNodeCount: 1`, `maxNodeCount: 3`, and the cluster is not a KCC object.

```yaml
apiVersion: container.cnrm.cloud.google.com/v1beta1
kind: ContainerNodePool
metadata:
  name: default-pool
  namespace: infra-prod
  annotations:
    cnrm.cloud.google.com/deletion-policy: abandon
    cnrm.cloud.google.com/state-into-spec: absent
spec:
  resourceID: default-pool
  location: us-central1
  clusterRef:
    external: projects/acme-prod/locations/us-central1/clusters/prod-cluster
  autoscaling:
    minNodeCount: 1
    maxNodeCount: 6
  nodeConfig:
    machineType: e2-standard-4
    diskSizeGb: 100
    diskType: pd-balanced
```

A request that also wants a bigger machine type is a replacement: a new
pool, a cordon-and-drain the customer runs, and the old pool abandoned later.
Say so and stop.

## ContainerCluster

Most requests against an existing cluster are edits to mutable fields; the
create case exists for a shop that provisions clusters through the repo.
The shape decisions (Autopilot or Standard, private or public, release
channel) come from the `gke-cluster-creation` skill's templates; this
reference only turns them into KCC.

### Fields

- **Required:** `spec.location`.
- **Immutable (create-only):** `location`, `resourceID`, `enableAutopilot`,
  `initialNodeCount`, `description`, `clusterIpv4Cidr`,
  `defaultMaxPodsPerNode`, `networkingMode`, `datapathProvider`,
  `enableTpu`, `enableKubernetesAlpha`, `enableMultiNetworking`,
  `dnsConfig`, `confidentialNodes`, `masterAuth.clientCertificateConfig`,
  all of `ipAllocationPolicy`, all of `nodeConfig` (the default pool), and
  `clusterAutoscaling.autoProvisioningDefaults.bootDiskKMSKeyRef` and
  `.shieldedInstanceConfig`. `networkRef` and `subnetworkRef` carry no
  `Immutable.` marker on the ref fields but a cluster cannot move networks;
  treat them as create-only.
- **Mutable:** `releaseChannel.channel`, `minMasterVersion`,
  `maintenancePolicy`, `resourceLabels`, `addonsConfig`, `loggingConfig`,
  `monitoringConfig`, `verticalPodAutoscaling`,
  `workloadIdentityConfig.workloadPool`, `masterAuthorizedNetworksConfig`,
  `binaryAuthorization`, `gatewayApiConfig`, `costManagementConfig`,
  `notificationConfig`, `clusterAutoscaling` (bar the two above). Confirm
  each with `explain`.
- **Default node pool:** `initialNodeCount` counts per zone. A Standard
  cluster managed alongside `ContainerNodePool` objects sets
  `cnrm.cloud.google.com/remove-default-node-pool: "true"` and
  `initialNodeCount: 1`.

### Acquire: move an existing cluster to the regular channel

```yaml
apiVersion: container.cnrm.cloud.google.com/v1beta1
kind: ContainerCluster
metadata:
  name: prod-cluster
  namespace: infra-prod
  annotations:
    cnrm.cloud.google.com/deletion-policy: abandon
    cnrm.cloud.google.com/state-into-spec: absent
spec:
  resourceID: prod-cluster
  location: us-central1
  releaseChannel:
    channel: REGULAR
```

`location` is the one immutable field stated; `describe` gave it. Every
node pool the cluster has stays externally managed until a
`ContainerNodePool` manifest acquires it.

### Create: an Autopilot cluster

```yaml
apiVersion: container.cnrm.cloud.google.com/v1beta1
kind: ContainerCluster
metadata:
  name: analytics-cluster
  namespace: infra-prod
  annotations:
    cnrm.cloud.google.com/deletion-policy: abandon
    cnrm.cloud.google.com/state-into-spec: absent
spec:
  location: us-central1
  enableAutopilot: true
  releaseChannel:
    channel: REGULAR
  networkRef:
    external: projects/acme-prod/global/networks/prod-vpc
  subnetworkRef:
    external: projects/acme-prod/regions/us-central1/subnetworks/prod-gke
  ipAllocationPolicy:
    clusterSecondaryRangeName: pods
    servicesSecondaryRangeName: services
  workloadIdentityConfig:
    workloadPool: acme-prod.svc.id.goog
```

Network, subnetwork, and the secondary range names come from the repository
or the user, never from a guess; every one of them is create-only.
