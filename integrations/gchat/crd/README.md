# 🤖 Platform Agent Operator-based GKE Deployment (`crd`)

This module provides a declarative, **operator-based** approach to provisioning, deploying, and managing the **Platform Agent Bot** on Google Kubernetes Engine (GKE) Autopilot.

Instead of relying on local, imperative bash scripts to configure GCP infrastructure and Kubernetes resources, this module leverages a custom Kubernetes Controller (**`platform-agent-operator`**) and a Custom Resource Definition (**`PlatformAgent`**). The operator continuously reconciles the state of your deployment to match your desired configuration.

---

## 📂 Directory Structure

```bash
integrations/gchat/crd/
├── provision.sh           # Idempotent, interactive setup to provision GKE, APIs, secrets, build agent, operator, and custom resource
├── teardown.sh            # Idempotent, interactive cleanup to tear down all GKE, operator, and GCP resources in reverse
├── platform-agent-operator/ # Go-based Operator for PlatformAgent (Kubebuilder-scaffolded)
└── devteam-agent-operator/  # Go-based Operator for DevTeamAgent (Kubebuilder-scaffolded)
    # Both operators share a standard Kubebuilder structure:
    ├── api/v1alpha1/      # Custom Resource Definition (CRD) Spec types
    ├── internal/          # Controller reconciliation logic
    ├── config/            # Kustomize configurations (CRD, RBAC, Manager)
    ├── Dockerfile         # Containerizes the operator manager
    └── Makefile           # Standard build/deploy targets
```

---

## ⚙️ The Reconciliation Lifecycle

When you apply a `PlatformAgent` Custom Resource, the `PlatformAgentReconciler` running inside the operator automatically runs through the following steps to ensure your desired state is achieved:

```mermaid
flowchart TD
    A[Apply PlatformAgent CR] --> B{Is CR Deleted?}
    B -- Yes --> C[Run GCP Teardown via Finalizer]
    C --> D[Delete GCP Pub/Sub & IAM SA]
    D --> E[Remove Finalizer & Delete CR]

    B -- No --> F[Add Finalizer agent.platform.io/finalizer]
    F --> G[Provision GCP Pub/Sub Topic & Subscription]
    G --> H[Create GCP Service Account & Workload Identity Bridge]
    H --> I[Configure IAM Policies: Vertex AI user, Pub/Sub pub/sub]
    I --> J[Fetch GCP Secrets & Sync to K8s Secret platform-agent-secrets]
    J --> K[Ensure K8s Resources: PVC, ConfigMap, ServiceAccount, Deployment]
    K --> L[Update CR Status Phase to Ready]
```

1. **Finalizer Registration**: Registers `agent.platform.io/finalizer` on the CR to prevent deletion until external GCP resources are safely cleaned up.
2. **GCP Pub/Sub Provisioning**: Automatically creates the target GCP Pub/Sub Topic and Subscription for Google Chat events if they do not already exist.
3. **Identity & Access (Workload Identity)**:
   - Creates a GCP Service Account (GSA) for the bot.
   - Binds the GSA to the Kubernetes Service Account (KSA) using Workload Identity (`roles/iam.workloadIdentityUser`).
   - Binds GCP IAM role `roles/aiplatform.user` to the GSA to enable native, keyless Vertex AI/Gemini API access.
   - Grants the GSA subscriber access to the Pub/Sub subscription and publish rights for Google Chat systems on the Pub/Sub topic.
4. **Secret Synchronization**: Resolves the latest active version of `GEMINI_API_KEY` from GCP Secret Manager and populates it into a local Kubernetes Secret `platform-agent-secrets` mapped directly to the pod environment.
5. **Workload Deployment**: Deploys the standard Kubernetes workloads (ConfigMap `platform-agent-config`, PVC `platform-agent-data`, ServiceAccount, and the Deployment `platform-agent-gateway` container).

---

## 🚀 Getting Started

### ⚡ Quickstart: Interactive Provisioner

The easiest way to get started is using the interactive `provision.sh` script. It automates GKE cluster setup, enables APIs, creates Artifact Registry, generates keys in Secret Manager, builds the agent container, builds and deploys the controller operator, and provisions a live `PlatformAgent` custom resource!

#### 1. Start the Provisioner

Run the provisioner from the `crd` directory:

```bash
cd integrations/gchat/crd
./provision.sh
```

The script will ask you for:

- Target GCP Project ID
- Target GKE GCP Region (default: `us-central1`)
- GKE Cluster Name (default: `platform-agent-host`)
- Target Namespace (default: `platform-agent`)
- Allowed Google Chat User Email

#### 2. Verify Operator & Workload Rollout

Once the script completes, check that the operator and gateway are rolling out:

```bash
kubectl get deployments -n platform-agent-operator-system
kubectl get pods -n platform-agent
```

You can track the reconciliation phase of your `PlatformAgent` custom resource:

```bash
kubectl get platformagent platform-agent-gateway -n platform-agent
```

#### 3. Populate API Secrets (Optional but Recommended)

If you chose not to supply your Gemini API key during the interactive setup, you should edit the GCP Secret Manager secret `GEMINI_API_KEY` in the Google Cloud Console with your live key.

---

## 🔌 Access and Administration

### 1. Access the Local Dashboard

Port-forward the dashboard to your local machine:

```bash
kubectl port-forward -n platform-agent deployment/platform-agent-gateway 9119:9119
```

Open your browser and navigate to `http://localhost:9119` to view the Platform Agent Visual Dashboard.

### 2. Approve Google Chat Integrations

To approve a pairing code and complete Google Chat setup:

```bash
kubectl exec -it deploy/platform-agent-gateway -n platform-agent -c hermes -- hermes pairing approve google_chat <PAIRING_CODE>
```

---

## 🧹 Clean Up & Teardown

The `teardown.sh` script deletes the custom resource (triggering Config Connector to clean up GSA, Pub/Sub, and IAM policies), undeploys the Operator, removes KCC configurations, destroys the Secret Manager secrets, removes the Artifact Registry repository, and tears down the GKE cluster.

Run the teardown script from the `crd` directory:

```bash
cd integrations/gchat/crd
./teardown.sh
```

---

## 🤖 DevTeam Agent Operator

The `devteam-agent-operator` is a dedicated operator for managing the lifecycle of `DevTeamAgent` instances. It is simpler than the Platform Agent Operator as it does not require GCP/KCC infrastructure reconciliation, focusing entirely on Kubernetes-native resources.

### Reconciliation Lifecycle

When you apply a `DevTeamAgent` Custom Resource, the operator ensures the following resources are provisioned and matched to your spec:

1. **ServiceAccount (KSA)**: A `<name>` ServiceAccount (or as specified by `ksaName`). If `gsaName` and `projectId` are provided, it annotates the KSA to enable Workload Identity.
2. **PersistentVolumeClaim (PVC)**: A `<name>-pvc` claim (default: `10Gi`) for persistent agent data storage.
3. **Deployment**: A `<name>` deployment running the `devteam-agent` image, configured with the requested replicas, resource limits, model environment variables, GKE/GCP context env vars, using the reconciled ServiceAccount, and mounting the PVC.
4. **Service**: A `<name>` ClusterIP service exposing ports `8642` (API) and `9119` (Dashboard).

### DevTeamAgent Custom Resource Spec

The `DevTeamAgent` spec allows configuring the following fields:

- `imageUri` (Required): The container image for the devteam agent.
- `replicas` (Optional, default: `1`): Number of desired pods.
- `storageSize` (Optional, default: `"10Gi"`): Size of the PVC.
- **Model Config (AI)**:
  - `modelName` (Optional, default: `"gemini-model"`): Name of the model to use.
  - `modelBaseUrl` (Optional, default: `"http://litellm.agent-system.svc.cluster.local/v1"`): Model API base URL.
  - `modelApiKey` (Optional, default: `"none"`): Model API key.
  - `apiServerKeySecretRef` (Optional, default: `"devteam-agent-secrets"`): Secret containing the `api-server-key`.
- **GCP / GKE Context**:
  - `projectId` (Optional): Target GCP Project ID.
  - `numericProjectId` (Optional): Target GCP Project Number.
  - `clusterName` (Optional): Host GKE Cluster Name.
  - `location` (Optional): Host GKE Cluster Location.
- **Identity (Workload Identity)**:
  - `gsaName` (Optional): GCP Service Account Name to bind to.
  - `ksaName` (Optional, default: CR name): Kubernetes Service Account Name.

### Build and Deploy

To build and deploy the DevTeam Agent Operator:

#### 1. Build the Operator Image

```bash
cd integrations/gchat/crd/devteam-agent-operator
make docker-build IMG=<your-registry>/devteam-agent-operator:latest
```

#### 2. Deploy to Cluster

Ensure you have configured `kubectl` to point to your target cluster, then:

```bash
# Install CRD
make install

# Deploy Controller
make deploy IMG=<your-registry>/devteam-agent-operator:latest
```

#### 3. Create a DevTeam Agent Instance

Create a file `my-devteam-agent.yaml`:

```yaml
apiVersion: devteam.platform.io/v1alpha1
kind: DevTeamAgent
metadata:
  name: devteam-agent
  namespace: agent-system
spec:
  imageUri: "gke-agentic/devteam-agent:latest"
```

Apply it to your cluster:

```bash
kubectl apply -f my-devteam-agent.yaml
```
