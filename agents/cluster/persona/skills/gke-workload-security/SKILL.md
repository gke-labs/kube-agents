---
name: gke-workload-security
description: Workflows for auditing and hardening the security of GKE workloads.
---

# GKE Workload Security

This skill provides workflows and best practices for securing GKE workloads. It
covers security auditing, Identity and Access Management (Workload Identity),
Network Security (Network Policies), and Node Security.

> You are read-only: each change below is a proposal you report with your findings, never a command you run.

## Workflows

### 1. Security Audit

Assess the current security posture of your cluster using the provided audit
script.

**Capabilities:**

- Checks for Workload Identity.
- Verifies Network Policy is enabled.
- Checks if Shielded Nodes are enabled.
- Checks if Binary Authorization is enabled.
- Checks for Private Cluster configuration.

**Command** (run from this skill's directory, the base directory the harness gives when it loads the skill):

```bash
bash scripts/audit_cluster.sh <cluster-name> <region> <project-id>
```

### 2. Configure Workload Identity

Workload Identity allows Kubernetes Service Accounts (KSAs) to impersonate
Google Service Accounts (GSAs). This is the recommended method for workloads to
access Google Cloud APIs.

**Steps:**

Each step is a change you propose.

1. **Namespace and KSA:** propose the Namespace and the KSA:

   ```yaml
   apiVersion: v1
   kind: Namespace
   metadata:
     name: workload-identity-test-ns
   ---
   apiVersion: v1
   kind: ServiceAccount
   metadata:
     name: <ksa-name>
     namespace: workload-identity-test-ns
     annotations:
       iam.gke.io/gcp-service-account: GSA_NAME@PROJECT_ID.iam.gserviceaccount.com
   ```

2. **Bind KSA to GSA:** propose granting `roles/iam.workloadIdentityUser` on
   the GSA `GSA_NAME@PROJECT_ID.iam.gserviceaccount.com` to the member
   `serviceAccount:<project-id>.svc.id.goog[workload-identity-test-ns/<ksa-name>]`.

3. **Annotate KSA:** the `iam.gke.io/gcp-service-account` annotation in the
   manifest above links the KSA to the GSA.

4. **Example Pod:** propose a test Pod in `workload-identity-test-ns` with
   `serviceAccountName: <ksa-name>` and the node selector
   `iam.gke.io/gke-metadata-server-enabled: "true"`. A complete example is in
   `assets/workload-identity-pod.yaml`.

### 3. Implement Network Policies

Control traffic flow between Pods using Network Policies. By default, all
traffic is allowed.

**Check Network Policy Enforcement & Dataplane:**

Before modifying cluster networking, inspect whether NetworkPolicy enforcement
is already active or provided natively by Dataplane V2:

```bash
gcloud container clusters describe <cluster-name> \
    --location <location> \
    --format='value(networkConfig.datapathProvider,networkPolicy.enabled)'
```

- If `datapathProvider` is `ADVANCED_DATAPATH` (Dataplane V2), NetworkPolicy
  enforcement is built-in natively via eBPF/Cilium from cluster creation. Calico
  addons cannot be enabled and are not needed.
- If `networkPolicy.enabled` is `True`, Calico enforcement is already enabled on nodes.
- If neither is active, propose enabling Calico network policy enforcement in
  the two steps below.

**Network Policy Enforcement (non-DPv2 clusters):**

On clusters without Dataplane V2 this is two sequential changes, in this order:
first enable the NetworkPolicy addon on the control plane (Config Connector
`ContainerCluster` field `spec.addonsConfig.networkPolicyConfig.disabled: false`,
with `spec.networkPolicy.enabled: false` alongside it, since the addon setting
does nothing without a `networkPolicy` block), then enable network policy
enforcement on the nodes (`spec.networkPolicy.enabled: true`, `provider: CALICO`).
GKE rejects node enforcement with HTTP 400 until the addon is enabled, and
node pools may be recreated, which can take several minutes. Propose both, with
that order stated.

**Default Deny Policy:**
Isolate namespaces by denying all ingress and egress traffic by default.

Propose this manifest, with `<target-namespace>` set to the namespace to
isolate. [assets/default-deny-netpol.yaml](assets/default-deny-netpol.yaml)
holds the same policy without the `metadata.namespace` line.

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: default-deny-all
  namespace: <target-namespace>
spec:
  podSelector: {}
  policyTypes:
    - Ingress
    - Egress
```

### 4. Enable Shielded Nodes

Ensure nodes are running with verifiable integrity.

Propose enabling Shielded Nodes (Config Connector `ContainerCluster` field
`spec.enableShieldedNodes: true`).

### 5. GKE Sandbox (gVisor)

Run untrusted workloads in a sandbox for extra isolation.

**GKE Sandbox:**
Propose a node pool with GKE Sandbox enabled (Config Connector
`ContainerNodePool` field `spec.nodeConfig.sandboxConfig.sandboxType: gvisor`).

**Run a Sandboxed Pod:**
Add `runtimeClassName: gvisor` to your Pod spec.

### 6. Pod Security Standards

Enforce security policies on namespaces using labels.

**Enforce Restricted Profile:**
Propose these labels on the namespace:

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: <namespace>
  labels:
    pod-security.kubernetes.io/enforce: restricted
    pod-security.kubernetes.io/enforce-version: latest
```

> [!NOTE]
> Using `latest` ensures you use the policies corresponding to the cluster's current version. You can pin it to a specific version (e.g., `v1.30`) to lock down the namespace to policies of a specific release.

### 7. Secret Manager Integration (CSI Driver)

Mount secrets from Google Cloud Secret Manager directly as volumes in your pods.

**Prerequisites**: Secret Manager CSI driver must be enabled on the cluster.

**Example SecretProviderClass:**

```yaml
apiVersion: secrets-store.csi.x-k8s.io/v1
kind: SecretProviderClass
metadata:
  name: my-secret-provider
spec:
  provider: gcp
  parameters:
    secrets: |
      - resourceName: "projects/<project-id>/secrets/my-secret/versions/latest"
        fileName: "my-secret-file"
```

**Example Pod Spec excerpt:**

```yaml
spec:
  containers:
    - name: my-app
      volumeMounts:
        - name: secrets-store-inline
          mountPath: "/mnt/secrets"
          readOnly: true
  volumes:
    - name: secrets-store-inline
      csi:
        driver: secrets-store.csi.k8s.io
        readOnly: true
        volumeAttributes:
          secretProviderClass: "my-secret-provider"
```

### 8. Enable Network Policy Logging

If using GKE Dataplane V2, you can log allowed and denied connections.

**Steps:**

1. Propose the `NetworkLogging` custom resource.

**Example NetworkLogging Manifest:**

```yaml
apiVersion: networking.gke.io/v1alpha1
kind: NetworkLogging
metadata:
  name: default
spec:
  cluster:
    allow:
      log: true
      delegate: true
    deny:
      log: true
      delegate: true
```

This will log connection details to Cloud Logging.

## Best Practices

1. **Least Privilege:** Always use Workload Identity with minimal IAM roles. Avoid using Node default service accounts.
2. **Network Isolation:** Use Network Policies to restrict Pod-to-Pod communication. Enable Network Policy Logging for visibility.
3. **Image Security:** Use Binary Authorization to ensure only trusted images are deployed.
4. **Secret Management**: Use Secret Manager CSI driver instead of default Kubernetes secrets for sensitive data.
5. **Pod Security**: Enforce `baseline` or `restricted` Pod Security Standards on all non-system namespaces.
6. **Policy Enforcement**: Consider using **Policy Controller** (Gatekeeper) to enforce custom security and compliance policies across the cluster.
