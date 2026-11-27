<!-- kube-agents: local addition (auto-injected by sync-upstream-skills.py) -->

## Before you pass `--dns-endpoint`

The `get-credentials --dns-endpoint` example above works only on a cluster that publishes a DNS
endpoint **and** has `controlPlaneEndpointsConfig.dnsEndpointConfig.allowExternalTraffic` set to
true. Check first:

```bash
gcloud container clusters describe {cluster_name} --region {region} \
  --format='value(controlPlaneEndpointsConfig.dnsEndpointConfig.endpoint,controlPlaneEndpointsConfig.dnsEndpointConfig.allowExternalTraffic)'
```

Do not infer support from the command succeeding. When external traffic is disabled, a caller that
Google treats as internal gets a warning rather than an error, plus a kubeconfig pointing at the
DNS endpoint that then returns HTTP 403 on first use — a failure that surfaces one step later than
its cause. The setting is turned on as Config Connector YAML on the cluster's `ContainerCluster`
(`spec.controlPlaneEndpointsConfig.dnsEndpointConfig.allowExternalTraffic`) through
`gcp-config-connector`, proposed as a pull request through `submit-suggestion`.

The Platform Agent's own tooling makes this decision per cluster in
`/opt/data/scripts/gke_endpoint.py`, so `switch_kube_context` and the Cluster Agent profile
scaffolding already pass the flag exactly when it applies; the check above is for the times you
run `get-credentials` by hand. That decision is re-read about once a minute per cluster, so after
enabling the setting, wait a moment before retrying rather than concluding it did not work.

## Scaling `kube-dns`, NodeLocal DNSCache, and Cloud DNS

Clusters that scale out rapidly from a small baseline to dozens or hundreds of nodes (for example,
bursty CI/CD builds) can saturate `kube-dns` before `kube-dns-autoscaler` adds replicas, causing
DNS resolution timeouts (`lookup ... i/o timeout`, `SERVFAIL`, `Temporary failure in name
resolution`) and node UDP conntrack table exhaustion (amplified by `ndots:5` search-path
expansion).

### Read-only inspection

Inspect both cluster-level DNS settings and the `kube-system` DNS stack before recommending
changes (do not skip `kube-system` when DNS symptoms or scaling headroom are in question):

```bash
gcloud container clusters describe {cluster_name} --location {location} --project {project_id} \
  --format="json(autopilot,networkConfig.dnsConfig,addonsConfig.dnsCacheConfig)"
kubectl get deployment kube-dns kube-dns-autoscaler -n kube-system --ignore-not-found
kubectl get configmap kube-dns-autoscaler -n kube-system --ignore-not-found -o jsonpath='{.data}'
kubectl get daemonset node-local-dns -n kube-system --ignore-not-found
kubectl top pods -n kube-system -l k8s-app=kube-dns
```

GKE Autopilot defaults to `CLOUD_DNS` with mandatory NodeLocal DNSCache and does not support
editing `ConfigMap/kube-dns-autoscaler`. On Standard, an omitted `addonsConfig.dnsCacheConfig`
block or `enabled: false` means NodeLocal DNSCache is disabled (Standard 1.34.1+ enables NodeLocal
DNSCache by default, though it can still be disabled). `Deployment/kube-dns` is absent on clusters
created with `CLOUD_DNS` (or scaled to 0 after migration), whereas Standard clusters migrated from
`kube-dns` continue running `kube-dns` until manually scaled to 0.

### Tuning `ConfigMap/kube-dns-autoscaler`

On Standard clusters using `kube-dns`, `kube-dns-autoscaler` computes target `kube-dns` replicas
from the `linear` JSON policy in `ConfigMap/kube-dns-autoscaler` in `kube-system`:
`replicas = max(ceil(cores / coresPerReplica), ceil(nodes / nodesPerReplica))`, clamped to
`[min, max]`. GKE Standard's actual default (`coresPerReplica: 256`, `nodesPerReplica: 16`,
`preventSinglePointFailure: true`, `includeUnschedulableNodes: true`, with `min` unset/1 so a
1-node cluster runs 1 replica and 2+ nodes get a 2-replica floor via `preventSinglePointFailure`)
lags behind rapid node bursts. (Do not propose `ConfigMap/kube-dns-autoscaler` edits on GKE
Autopilot, or on `CLOUD_DNS` clusters where `Deployment/kube-dns` is absent or scaled to 0; on a
Standard cluster mid-migration to `CLOUD_DNS` where `Deployment/kube-dns` still has running
replicas, pods on un-recreated node pools still resolve through `kube-dns` until those node pools
are recreated.)

When a Standard cluster stays on `kube-dns`, propose a tighter `ConfigMap/kube-dns-autoscaler`
manifest through `gke-manifest-generation` and `submit-suggestion` — lowering `nodesPerReplica`
(for example `4`–`8`) and/or `coresPerReplica` (for example `64`–`128`), setting both an explicit
`min` (for example `4`) for baseline burst headroom and an explicit `max` cap (up to node count,
capped at `500` on large clusters so `kube-dns` API watches do not overload the control plane), and
keeping `preventSinglePointFailure: true`:

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: kube-dns-autoscaler
  namespace: kube-system
data:
  linear: >-
    {"coresPerReplica":128,"nodesPerReplica":8,"min":4,"max":100,"preventSinglePointFailure":true,"includeUnschedulableNodes":true}
```

### Enabling NodeLocal DNSCache

When `addonsConfig.dnsCacheConfig.enabled` is not `true` on a Standard cluster using `kube-dns`,
recommend enabling NodeLocal DNSCache (`DaemonSet/node-local-dns`). With `kube-dns` + NodeLocal
DNSCache, pods keep the `kube-dns` Service ClusterIP in `/etc/resolv.conf` (intercepted locally on
each node) and NodeLocal DNSCache upgrades upstream queries to `kube-dns` over TCP, reducing
`kube-dns` QPS and eliminating per-lookup UDP conntrack entries. With `CLOUD_DNS` + NodeLocal
DNSCache, pods query `169.254.20.10` and cache misses forward to `169.254.169.254`.

- Treat enabling NodeLocal DNSCache on an existing Standard cluster as a **manual maintenance-window
  change** (`kind: manual`, matching `gcp_networking_fabric_sop.md` §2.7) rather than opening a
  `submit-suggestion` Config Connector or Terraform pull request — merging a Config Connector PR
  applies immediately and recreates every node outside the maintenance window. Give the operator the
  maintenance-window command (`gcloud container clusters update <cluster> --location <location> --update-addons=NodeLocalDNS=ENABLED`, never run by the agent), and update declarative IaC
  (`spec.addonsConfig.dnsCacheConfig.enabled: true` in `gcp-config-connector` or
  `addons_config.dns_cache_config.enabled = true` in `gcp-terraform`) only for new clusters or
  timed with that maintenance window. Verify strict egress `NetworkPolicy` rules for the cluster's
  dataplane: on Dataplane V2
  with `kube-dns`, `node-local-dns` runs on the pod network (`hostNetwork: false`, label
  `k8s-app: node-local-dns`), so egress policies must permit UDP/TCP 53 to `kube-system` DNS pods;
  on non-Dataplane-V2 clusters with legacy NetworkPolicy, `node-local-dns` runs with
  `hostNetwork: true`, requiring an `ipBlock` egress rule for the `kube-dns` Service
  `ClusterIP/32`.

### Migrating to Cloud DNS (`CLOUD_DNS`)

When node scaling velocity routinely exceeds `kube-dns-autoscaler` headroom, recommend Cloud DNS
for GKE (`networkConfig.dnsConfig.clusterDns: CLOUD_DNS`). With `CLOUD_DNS` + NodeLocal DNSCache,
pods query `169.254.20.10` forwarding cache misses to `169.254.169.254`; with `CLOUD_DNS` without
NodeLocal DNSCache, pods resolve directly against `169.254.169.254`, removing in-cluster
`kube-dns` pod scaling bottlenecks altogether.

- For new clusters, set `spec.dnsConfig.clusterDns: CLOUD_DNS` (`gcp-config-connector`) or
  `dns_config.cluster_dns = "CLOUD_DNS"` (`gcp-terraform`).
- For existing Standard clusters running `kube-dns`, note that Config Connector marks
  `ContainerCluster` `spec.dnsConfig` immutable (create-only); propose the migration through
  `gcp-terraform` (or as a maintenance-window operator migration), note that existing node pools
  must be recreated to transition pods off `kube-dns` onto Cloud DNS, and note that
  `Deployment/kube-dns` continues running on migrated clusters until manually scaled to 0.
