---
name: gke-platform-security
description: >-
  Plans, configures, and hardens platform-level Google Kubernetes Engine (GKE)
  cluster security. Covers cluster add-ons (Secret Manager enablement), RBAC
  hardening (disabling insecure bindings, audit tools), Binary Authorization,
  Secrets Encryption (--database-encryption-key), Security Posture (--security-posture),
  enabling Shielded Nodes, GKE Sandbox cluster enablement, GKE IAM roles, and
  cross-service authentication IAM patterns. Use when securing cluster control
  planes, hardening GKE RBAC, enabling Shielded Nodes, enabling GKE Sandbox runtime,
  enabling cluster-wide security add-ons, or managing GKE IAM roles. Don't use
  for Workload Identity (use gke-workload-identity) or workload-level security
  (SecretProviderClass, PSS, NetPol, gVisor pod runtimeClassName; use gke-workload-security).
metadata:
  version: "1.0.1"
  category: Security
---

# GKE Platform Security

This reference covers platform-level security hardening and cluster
configuration for Google Kubernetes Engine (GKE). For workload-level security
controls (such as SecretProviderClass volume mounts, Network Policies, and Pod
Security Standards), refer to the `gke-workload-security` skill; for Workload
Identity Service Account bindings, the `gke-workload-identity` skill.

> **MCP Tools:** `get_cluster`, `check_k8s_auth`, `get_k8s_resource` (all on the `gke`
> server)
>
> A change goes out as a pull request to the GitOps repository, never as a write from here: Kubernetes objects as YAML through `gke-manifest-generation`, cluster settings and other Google Cloud resources as Config Connector YAML through `gcp-config-connector`, opened with `submit-suggestion`. Each profile's config offers only the `gke` MCP server's read tools, and the broker refuses `kubectl` and `gcloud` writes.

## Golden Path Security Defaults

Setting                                                        | Golden Path Value                       | Day-0/1 | Notes
-------------------------------------------------------------- | --------------------------------------- | ------- | -----
`workloadIdentityConfig.workloadPool`                          | `<PROJECT>.svc.id.goog`                 | Day-0   | Workload Identity Federation for cluster pods
`secretManagerConfig.enabled`                                  | `true`                                  | Day-1   | Google Secret Manager cluster add-on integration
`secretManagerConfig.rotationConfig`                           | `enabled: true, rotationInterval: 120s` | Day-1   | Automatic secret rotation at the cluster level
`rbacBindingConfig.enableInsecureBindingSystemAuthenticated`   | `false`                                 | Day-0   | Blocks legacy `system:authenticated` bindings
`rbacBindingConfig.enableInsecureBindingSystemUnauthenticated` | `false`                                 | Day-0   | Blocks legacy `system:unauthenticated` bindings
`nodeConfig.shieldedInstanceConfig.enableSecureBoot`           | `true`                                  | Day-0   | Verifiable boot integrity
`nodeConfig.shieldedInstanceConfig.enableIntegrityMonitoring`  | `true`                                  | Day-0   | Runtime integrity checks
`nodeConfig.workloadMetadataConfig.mode`                       | `GKE_METADATA`                          | Day-0   | Blocks legacy metadata API, enforces Workload Identity
Private cluster + Dataplane V2 settings                        | See the `gke-networking` skill          | Day-0   | Private nodes, private endpoint enforcement, ADVANCED_DATAPATH

## Secret Manager Add-on Enablement

The golden path enables Secret Manager at the cluster level with automatic
secret rotation.

```bash
# Verify Secret Manager is enabled on cluster
gcloud container clusters describe <CLUSTER_NAME> --region <REGION> \
  --format="value(secretManagerConfig.enabled)" \
  --quiet
```

If it is not enabled, Secret Manager with a 120s rotation interval is a Day-1 cluster setting.
Config Connector's `ContainerCluster` has no field for it, so it has no declarative path in a
Config Connector repository: say so, and block rather than invent a field. A Terraform repository
carries it on the cluster resource.

> **Note:** For configuring `SecretProviderClass` manifests and mounting secrets
> as volumes inside application deployments, see the `gke-workload-security`
> skill.

## RBAC Hardening

The golden path disables insecure legacy RBAC bindings that grant broad access
to `system:authenticated` and `system:unauthenticated` groups.

```bash
# Verify insecure bindings are disabled
gcloud container clusters describe <CLUSTER_NAME> --region <REGION> \
  --format="yaml(rbacBindingConfig)" \
  --quiet
```

**Best practices for RBAC:**

-   Use namespace-scoped Roles over cluster-wide ClusterRoles.
-   Bind to specific Groups or ServiceAccounts, never to `system:authenticated`
    or `system:unauthenticated`.
-   Audit permissions via MCP: `check_k8s_auth(parent="...", verb="list",
    resourceType="pods", namespace="...")` (or `kubectl auth can-i --list
    --as=<user>`).
-   Review bindings via MCP: `get_k8s_resource(parent="...",
    resourceType="clusterrolebinding")` (or `kubectl get
    clusterrolebindings,rolebindings --all-namespaces`).

> See the `gke-multitenancy` skill for enterprise RBAC planning and
> https://docs.cloud.google.com/kubernetes-engine/docs/best-practices/rbac.md.txt

## Binary Authorization

Not enabled in golden path by default but recommended for enforcing production
image provenance across the cluster. It is turned on with the evaluation mode
`PROJECT_SINGLETON_POLICY_ENFORCE` (`spec.binaryAuthorization.evaluationMode`), changed as
Config Connector YAML on the cluster's `ContainerCluster` through `gcp-config-connector`.

## Shielded Nodes & GKE Sandbox Enablement

Verifiable node boot integrity is a cluster setting and kernel isolation a node pool one. Shielded Nodes (`spec.enableShieldedNodes`) are changed as Config Connector
YAML on the cluster's `ContainerCluster` through `gcp-config-connector`. The GKE Sandbox
(gVisor) runtime is a node pool setting, `spec.nodeConfig.sandboxConfig.sandboxType: gvisor` on
a `ContainerNodePool`; the field is immutable and the default pool cannot run it, so propose a
new sandboxed node pool rather than editing an existing one.

> **Note:** To run workloads inside the gVisor sandbox, specify
> `runtimeClassName: gvisor` in your Pod specs as detailed in the
> `gke-workload-security` skill.

## Common IAM Roles

The five most common predefined IAM roles for GKE platform and cluster access:

| Role                            | Purpose             | When to Use          |
| ------------------------------- | ------------------- | -------------------- |
| `roles/container.admin`         | Full control over   | Platform team admins |
:                                 : clusters and        : managing cluster     :
:                                 : Kubernetes          : lifecycle            :
:                                 : resources           :                      :
| `roles/container.clusterAdmin`  | Manage clusters but | Cluster operators    |
:                                 : not project-level   : who create/delete    :
:                                 : IAM                 : clusters             :
| `roles/container.developer`     | Deploy workloads    | Application          |
:                                 : (pods, services,    : developers deploying :
:                                 : deployments)        : to existing clusters :
| `roles/container.viewer`        | Read-only access to | Monitoring,          |
:                                 : clusters and        : auditing, or         :
:                                 : Kubernetes          : read-only dashboards :
:                                 : resources           :                      :
| `roles/container.clusterViewer` | List and get        | CI/CD pipelines that |
:                                 : cluster details     : need cluster         :
:                                 : only                : metadata             :

> **Principle of least privilege**: Start with `roles/container.viewer` or
> `roles/container.developer` and escalate only as needed. Avoid granting
> `roles/container.admin` broadly across teams.

## Service Accounts & Agents

-   **GKE Service Agent**
    (`service-<PROJECT_NUMBER>@container-engine-robot.iam.gserviceaccount.com`):
    Automatically created. Manages nodes, networking, and cluster operations on
    your behalf. Do not remove or modify its permissions.
-   **Node Service Account**: By default, nodes use the Compute Engine default
    service account. For production platforms, create a dedicated Google Service
    Account with minimal required permissions (`roles/monitoring.metricWriter`,
    `roles/logging.logWriter`) and assign it at node pool creation time.
-   **Workload Identity**: For binding Google Service Accounts to Kubernetes
    Service Accounts (`roles/iam.workloadIdentityUser`), refer to the
    `gke-workload-identity` skill.

## Cross-Service Authentication Patterns

Common project-level IAM policy binding patterns for granting backend Google
Service Accounts (GSAs) access to external Google Cloud services before linking
via Workload Identity: grant the GSA `roles/storage.objectViewer` for Cloud Storage objects,
`roles/cloudsql.client` for Cloud SQL databases, or `roles/pubsub.subscriber` for Pub/Sub
subscriptions, each as a project-level IAM binding in Config Connector YAML through
`gcp-config-connector`.

## Resources

- [GKE Cluster Hardening Guide](https://cloud.google.com/kubernetes-engine/docs/how-to/hardening-your-cluster)
- [GKE RBAC Best Practices](https://cloud.google.com/kubernetes-engine/docs/best-practices/rbac)
- [Secret Manager Add-on for GKE](https://cloud.google.com/secret-manager/docs/secret-manager-managed-csi-component)
- [Binary Authorization on GKE](https://cloud.google.com/binary-authorization/docs/setting-up)
- [Shielded GKE Nodes](https://cloud.google.com/kubernetes-engine/docs/how-to/shielded-gke-nodes)
- [GKE Sandbox (gVisor)](https://cloud.google.com/kubernetes-engine/docs/how-to/sandbox-pods)

<!-- kube-agents: local addition (auto-injected by sync-upstream-skills.py) -->

## Application-layer secrets encryption and Security Posture

The description and the gke-basics and gke-workload-security routing notes send these two flags
here.

**Secrets encryption (`--database-encryption-key`)** envelope-encrypts Secrets in etcd with a
Cloud KMS key. The key must be in the cluster's region (`KMS_LOCATION`; for a zonal cluster, the
region that contains its zone, since Cloud KMS has no zonal locations), and the GKE service agent
of the cluster's project (`CLUSTER_PROJECT_NUMBER`, not the key project's number) needs
`roles/cloudkms.cryptoKeyEncrypterDecrypter` on it before the cluster can use it. Both go out as
Config Connector YAML through `gcp-config-connector`: the key's IAM binding for
`service-CLUSTER_PROJECT_NUMBER@container-engine-robot.iam.gserviceaccount.com`, and on the
cluster's `ContainerCluster` the key
(`projects/KMS_PROJECT_ID/locations/KMS_LOCATION/keyRings/KEYRING_NAME/cryptoKeys/KEY_NAME`) in
`spec.databaseEncryption.keyName` with `spec.databaseEncryption.state: ENCRYPTED`. Verify:

```bash
# CURRENT_STATE_ENCRYPTED once encryption completes (`state` is the requested setting, not the observed one)
gcloud container clusters describe CLUSTER_NAME --location LOCATION \
  --format="value(databaseEncryption.currentState)"
```

**Security Posture (`--security-posture`)** turns on configuration auditing (`standard`) and,
with `--workload-vulnerability-scanning`, OS vulnerability scanning of running workloads. Both
are changed as Config Connector YAML on the cluster's `ContainerCluster` through
`gcp-config-connector`: `spec.securityPostureConfig.mode: BASIC` and
`spec.securityPostureConfig.vulnerabilityMode: VULNERABILITY_BASIC` (Config Connector's values,
not gcloud's `standard`).
