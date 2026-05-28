# Hermes Agent GKE Deployment

This folder contains a suite of automated bash scripts to provision, build, deploy, and tear down a custom **Hermes Agent** on Google Kubernetes Engine (GKE) Autopilot. It leverages Google Cloud Build, Artifact Registry, Secret Manager, and Kubernetes manifests for a production-ready, secure deployment.

---

## 🛠️ Prerequisites

Ensure the following tools are installed and configured on your local machine before running the scripts:

* **Google Cloud SDK (`gcloud`)**: Authenticated to your GCP account.
* **GKE Auth Plugin (`gke-gcloud-auth-plugin`)**: Required for `kubectl` to communicate with GKE.
* **Kubernetes CLI (`kubectl`)**: Used for cluster management and port-forwarding.
* **`envsubst`**: Usually available by default on Linux/macOS via the `gettext` package.

> **Note:** Ensure your target GCP project has an active **Billing Account** linked, as GKE Autopilot and Artifact Registry cannot be provisioned without one.

---

## ⚙️ Environment Variables

Create a `.env` file in the root directory (alongside these scripts). The scripts feature strict validation and will halt if required variables are missing.

```env
# Core Infrastructure
PROJECT_ID="your-project-id"
REGION="us-central1"
CLUSTER_NAME="hermes-agent-cluster"

# Container Registry
REPO_NAME="hermes-agent-repo"
IMAGE_NAME="hermes-agent"
IMAGE_TAG="latest"

# Secrets (Optional: If omitted, scripts fallback to GCP Secret Manager)
GCP_API_KEY="your-gcp-api-key"
GEMINI_API_KEY="your-gemini-api-key"

```

---

## 📂 Files Overview

* `01_setup_gcp.sh`: Validates your local environment, enables required GCP APIs, provisions the Artifact Registry, creates the GKE Autopilot cluster, and configures strict IAM roles.
* `02_build_push_image.sh`: Bypasses local Docker entirely. It packages your `../app` directory and submits it to Google Cloud Build, pushing the resulting image directly to your Artifact Registry.
* `03_deploy.sh`: Fetches cluster credentials, securely maps your API keys into Kubernetes Secrets, and applies `deployment.yaml` with your dynamically injected image URI.
* `04_teardown.sh`: An idempotent cleanup script that safely deletes your GKE cluster, Artifact Registry, and Secrets to prevent ongoing billing.
* `deployment.yaml`: Defines the Persistent Volume Claim (PVC), GKE Autopilot burstable resource limits, health probes, ConfigMaps, and an Internal LoadBalancer Service to keep the agent securely inside your VPC.

---

## 🚀 Usage Guide

First, make all scripts executable:

```bash
chmod +x 01_setup_gcp.sh 02_build_push_image.sh 03_deploy.sh 04_teardown.sh

```

### Step 1: Provision Infrastructure

```bash
./01_setup_gcp.sh

```

*Wait for the cluster to fully provision. If you did not put your API keys in the `.env` file, go to the GCP Console and update the `placeholder` values in Secret Manager now.*

### Step 2: Build and Push Image

```bash
./02_build_push_image.sh

```

*Make sure your Hermes source code is located in a directory named `../app` relative to this script.*

### Step 3: Deploy to GKE

```bash
./03_deploy.sh

```

*The deploy script will automatically output the `kubectl port-forward` command you need to access the Hermes Dashboard locally.*

### Step 4: Cleanup (Optional)

```bash
./04_teardown.sh

```

> ⚠️ **Warning:** This will permanently destroy the cluster, registry, and stored data. You will be prompted to confirm before the teardown begins.
