# GitOps Integration: GCP Config Sync for `kube-agents`

This directory contains instructions and manifests for integrating the `kube-agents` repository with **GCP Config Sync** (GKE's GitOps engine). Using Config Sync allows GKE to automatically watch this repository and sync your Custom Resource Definitions (CRDs), operator deployments, and Custom Resources (`HermesAgent`) in real-time.

---

## 📊 RootSync vs. RepoSync: Which is better?

Config Sync provides two different Custom Resources to define a Git synchronization: **RootSync** and **RepoSync**.

| Feature | `RootSync` (Recommended) | `RepoSync` |
| :--- | :--- | :--- |
| **Scope** | **Cluster-wide** (Cluster-level & Namespace-level) | **Namespace-scoped only** |
| **Privileges** | Runs as `cluster-admin` (by default) | Restricted to a single namespace's permissions |
| **CRD Support** | **Yes** (Can install CRDs and ClusterRoles) | **No** (Cannot install cluster-scoped CRDs) |
| **Ideal Use Case** | Platform Admins, Operators, system setups | App Developers, Tenant Teams, single namespace workloads |

### Why `RootSync` is the best choice for this repository:
Because this repository contains the custom Hermes operator which relies on **Custom Resource Definitions (CRDs)** (which are cluster-scoped resources), a standard `RepoSync` cannot be used because it lacks the permission to register CRDs in GKE. 

We must use a **`RootSync`** applied to the `config-management-system` namespace to successfully manage the operator's lifecycle.

---

## 📁 Recommended Git Directory Structure (`unstructured` mode)

Config Sync supports two repository formats: `hierarchical` and `unstructured`. We highly recommend **`unstructured`** because it allows you to organize your YAML manifests in any folder layout you prefer (similar to a standard Helm chart or Kustomize setup).

Here is the Git directory structure for our GitOps manifests inside this repo, utilizing the Bootstrapping Pattern:

```
kube-agents/
└── hack/
    └── git_ops/
        └── config_sync/
            └── manifests/
                ├── system/
                │   └── hermes-operator.yaml                   # Consolidated Operator manifest (Namespace, RBAC, CRD, Deployment)
                └── apps/
                    └── hermes-agent-bot.yaml                  # Custom Resource (CR) instances for your bot
```

---

## 🛠️ Step-by-Step Setup Guide

### Step 0: Configure Environment Variables
We use a local `.env` file to keep secrets and local configs outside of Git.

1. Copy the template file to a local `.env` file:
   ```bash
   cp .env.example .env
   ```
2. Open `.env` and fill in your specific Google Cloud and Git repository details.
3. Load the environment variables into your current shell session:
   ```bash
   export $(grep -v '^#' .env | xargs)
   ```

---

### Step 0.5: 🍳 How to Cook (Build & Release) Both GKE Images in Parallel

To run our GitOps system in GKE, both the **Go Operator** container and the **Chatbot Agent** container must be built ("cooked") and pushed to your Google Artifact Registry (GAR). 

Instead of compiling locally, we run a **unified parallel pipeline completely in Google Cloud** using a single `gcloud` command and a `cloudbuild.yaml` configuration!

---

#### 🔑 One-Time GCP Bootstrap Setup (Registry & IAM Permissions)
*Note: This is a one-time bootstrap step. You must run these commands to enable required GCP services, create the registry, and grant appropriate storage/logging permissions to the automated Cloud Build executor.*

1. **Enable the required GCP Service APIs** (Cloud Build, Artifact Registry, and GKE Fleet Hub) in your project:
   ```bash
   gcloud services enable cloudbuild.googleapis.com \
                          artifactregistry.googleapis.com \
                          gkehub.googleapis.com \
                          --project=${GCP_PROJECT_ID}
   ```

2. **Create the Google Artifact Registry repository**:
   ```bash
   gcloud artifacts repositories create hermes-agent-repo \
       --repository-format=docker \
       --location=${GKE_REGION} \
       --project=${GCP_PROJECT_ID}
   ```

3. **Configure IAM Permissions for the automated Cloud Build runner**:
   Cloud Build runs using your project's default Compute Engine Service Account. We must grant it access to read the source tarball from Cloud Storage and write logs:
   ```bash
   # Resolve your GCP Project Number dynamically
   export GCP_PROJECT_NUMBER=$(gcloud projects describe ${GCP_PROJECT_ID} --format="value(projectNumber)")

   # Grant Storage Object Viewer (GCS read permission)
   gcloud projects add-iam-policy-binding ${GCP_PROJECT_ID} \
       --member="serviceAccount:${GCP_PROJECT_NUMBER}-compute@developer.gserviceaccount.com" \
       --role="roles/storage.objectViewer" \
       --condition=None

   # Grant Logging Log Writer (Stackdriver logs permission)
   gcloud projects add-iam-policy-binding ${GCP_PROJECT_ID} \
       --member="serviceAccount:${GCP_PROJECT_NUMBER}-compute@developer.gserviceaccount.com" \
       --role="roles/logging.logWriter" \
       --condition=None

   # Grant GKE Hub Service Agent (Required for cluster Fleet registration)
   gcloud projects add-iam-policy-binding ${GCP_PROJECT_ID} \
       --member="serviceAccount:service-${GCP_PROJECT_NUMBER}@gcp-sa-gkehub.iam.gserviceaccount.com" \
       --role="roles/gkehub.serviceAgent" \
       --condition=None
   ```

---

#### ⚡ The Single-Command Parallel Build Workflow
Whenever you make changes to the chatbot application (`hack/gchat/app`) or the operator code (`hack/gchat/crd/hermes-operator`), you can build, package, and release **both** images in parallel with **one command**:

1. **Submit the parallel build to Google Cloud Build**:
   Run this command from the **root** of the repository:
   ```bash
   gcloud builds submit --config=hack/git_ops/config_sync/cloudbuild.yaml --project=${GCP_PROJECT_ID} .
   ```
   *(GCP will boot up parallel builders, compile both container images concurrently in the cloud, and push them straight into your Artifact Registry!)*

2. **Re-compile the unified GitOps manifest** (Only if configurations or version tags changed):
   
   > [!TIP]
   > **When you can skip this step (90% of the time):**
   > If you only modified Go source code and are rebuilding the image under the same tag (e.g., `:latest`), **you can skip this step completely**. The YAML manifest in your repo already points to `:latest`, and GKE will fetch the updated container automatically.
   >
   > **You ONLY need to run this step if:**
   > 1. You are releasing a new version tag (e.g., changing the tag from `:v1.0.0` to `:v1.1.0`).
   > 2. You modified GKE-specific settings for the operator (such as RBAC roles, ServiceAccounts, CPU/Memory requests, or added new fields in `_types.go` schemas) under the `config/` folder.
   ```bash
   export IMG="${GKE_REGION}-docker.pkg.dev/${GCP_PROJECT_ID}/hermes-agent-repo/hermes-operator:latest"
   
   cd hack/gchat/crd/hermes-operator
   make build-installer IMG=$IMG
   
   # Overwrite the GitOps deployment manifest
   cp dist/install.yaml ../../../git_ops/config_sync/manifests/system/hermes-operator.yaml
   ```

---

### Step 1: Enable Config Sync on your GKE Cluster
Using the loaded environment variables, natively register the cluster to your project fleet and enable Config Sync:

```bash
# 1. Natively register GKE cluster to your Fleet (Auto-configures Workload Identity)
gcloud container clusters update ${GKE_CLUSTER_NAME} \
    --region=${GKE_REGION} \
    --project=${GCP_PROJECT_ID} \
    --enable-fleet

# 2. Enable the config-management feature on the fleet
gcloud beta container fleet config-management enable --project=${GCP_PROJECT_ID}

# 3. Apply the configuration manager settings using the local apply-spec.yaml configuration
gcloud beta container fleet config-management apply \
    --membership=${GKE_CLUSTER_NAME} \
    --config=apply-spec.yaml \
    --version=${GKE_CONFIG_SYNC_VERSION} \
    --project=${GCP_PROJECT_ID}
```

---

### Step 2: Configure Git Authentication (Personal Access Token)
Config Sync needs read permission to pull manifests from your private Git repository. Since organizational policies block SSH Deploy Keys, we use a **GitHub Personal Access Token (Classic)** over HTTPS.

#### Option A: Personal Access Token (Classic) Authentication
1. Go to your GitHub **Settings** $\rightarrow$ **Developer settings** $\rightarrow$ **Personal access tokens** $\rightarrow$ **Tokens (classic)**.
2. Click **Generate new token** $\rightarrow$ **Generate new token (classic)**.
   * **Note**: `GKE Config Sync`
   * **Expiration**: `30 days` (or preferred length)
   * **Scopes**: Check the box next to **`repo`** (required for private repos).
3. Click **Generate token** and copy the token string immediately.
4. **SSO Authorization (Critical)**: Click the **`Configure SSO`** button next to the newly generated token and click **`Authorize`** for the `gke-agentic` organization.
5. Create the GKE Secret using the username and token string:
   ```bash
   kubectl create namespace config-management-system || true
   
   # Create GKE Secret containing username and PAT
   kubectl create secret generic git-creds \
       --namespace=config-management-system \
       --from-literal=username=your-github-username \
       --from-literal=token=ghp_YOUR_TOKEN_HERE
   ```

---

### Step 3: Create and Apply the `RootSync` Manifest

To dynamically inject the environment variables into the Kubernetes manifest, we use `envsubst` to generate the final configuration.

1. Create a template file named `rootsync.yaml` with the following content:

```yaml
apiVersion: configsync.gke.io/v1beta1
kind: RootSync
metadata:
  name: root-sync
  namespace: config-management-system
spec:
  sourceFormat: unstructured
  sourceType: git
  git:
    repo: ${GIT_REPO_URL}
    branch: ${GIT_BRANCH}
    dir: ${GIT_SYNC_DIR}
    auth: token
    secretRef:
      name: git-creds
```

2. Use `envsubst` to substitute the variables and apply the final manifest directly to GKE:
   ```bash
   envsubst < rootsync.yaml | kubectl apply -f -
   ```

---

## 🔍 Verification & Troubleshooting

Once applied, Config Sync will start reconciling the GKE cluster to match the folders inside your Git repository.

### 1. Check Sync Status via CLI
You can inspect the synchronization status using `kubectl`:

```bash
kubectl get rootsync root-sync -n config-management-system -o yaml
```

Look for the `status.conditions` block. You should see `Reconciling` change to `Synced`.

### 2. Monitor using `nomos` tool
GCP provides a CLI tool called `nomos` specifically for monitoring Config Sync:

```bash
# Install the nomos CLI
gcloud components install nomos

# Check status of all syncs
nomos status
```

### 3. Confirm the Operator and CRDs are Synced
Once synced, verify that Config Sync has automatically applied the Custom Resource and CRDs:

```bash
# Check if the CRD is present
kubectl get crds | grep hermesagents

# Check if the workloads are synced and healthy
kubectl get hermesagents
```
