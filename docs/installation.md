# Kube-Agents Installation Guide

This document provides step-by-step instructions for bootstrapping GCP/GKE infrastructure and deploying the Kube-Agents operator and platform agent.

Before proceeding, ensure you have reviewed the [Architecture & Sizing Guide](architecture_and_sizing.md) to choose the correct cluster size.

---

## Prerequisites & API Key Configuration

To use Gemini models, you must provide a Gemini API Key. Kube-Agents supports Gemini 3.1 and newer models (older models are deprecated).

### How to Acquire a Gemini API Key

1.  Navigate to [Google AI Studio](https://aistudio.google.com/).
2.  Sign in with your Google account.
3.  Click on **Get API Key** in the top left.
4.  Click **Create API Key**. You can associate it with an existing Google Cloud Project (e.g., `gca-gke-2025`) or create a new one.
5.  Copy the generated API Key.

### Attaching the Key to Kube-Agents

- **Using the Provisioner**: When running `./scripts/provision.sh` (see below), the script will securely prompt you for your `GEMINI_API_KEY` and automatically create the required Kubernetes Secret (`platform-agent-secrets`).
- **Manual Setup**: If you are not using the provisioner, or need to rotate the key, you can create/update the secret manually:
  ```bash
  kubectl create secret generic platform-agent-secrets \
    --namespace=kubeagents-system \
    --from-literal=GEMINI_API_KEY="your_actual_api_key_here" \
    --from-literal=API_SERVER_KEY="$(openssl rand -hex 16)" \
    --dry-run=client -o yaml | kubectl apply -f -
  ```

---

## Bootstrapping GCP & GKE Infrastructure

To simplify development and testing in a real GKE/GCP environment, you can use the automated provisioning and teardown workflow. This infrastructure is fully modularized and idempotent.

### 1. The Provisioning Pipeline

To bootstrap GCP APIs, a GKE Standard cluster, Artifact Registry, Secrets, Google Chat Pub/Sub resources, build and push containers, and apply the Custom Resource (CR) in one command:

```bash
# Run from k8s-operator directory
make gcp-provision
```

Or execute the master script directly from the scripts folder:

```bash
./scripts/provision.sh [--dry-run]
```

#### How it Works & Modular Sub-scripts

The master `provision.sh` script orchestrates modular sub-scripts sequentially. Each sub-script is idempotent: it verifies the state of its resources before executing any action.

1.  **`provision_01_gcp_cluster.sh`**:
    - Sets up configuration state (prompts for GCP Project ID, region, cluster name, GChat allowed user, default model configuration) and writes parameters to `scripts/vars.sh`.
    - Enables GKE/GCP Service APIs.
    - Provisions a GKE Standard Cluster with Workload Identity.
    - Configures `kubectl` credentials and creates the target namespace.
2.  **`provision_01a_gvisor_nodepool.sh`** (Optional):
    - Provisions a dedicated GKE Sandbox (gVisor) node pool (`gvisor-pool`) for secure container runtime isolation. Executed automatically if `ENABLE_GVISOR=true`.
3.  **`provision_02_gcp_gke_operator.sh`**:
    - Registers operator CRDs onto the GKE cluster.
    - Deploys the Operator controller manager.
4.  **`provision_03_gcp_iam.sh`**:
    - Pre-provisions GCP Service Accounts (GSAs) and Workload Identity bindings.
    - Configures the Controller's GSA with cluster management permissions and annotates the Controller KSA.
    - Configures the Agent GSAs (Platform Agent) with container viewer/admin permissions.
5.  **`provision_04_gcp_gchat.sh`**:
    - Creates the Pub/Sub Chat Event Topic and Subscriber Subscription for Google Chat events.
6.  **`provision_05_slack.sh`**:
    - Configures Slack integration parameters, bot tokens, and home channel settings.
7.  **`provision_06_gcp_k8s_secrets.sh`**:
    - Prompts for or reads the `MODEL_PROVIDER` and corresponding API keys (e.g., `GEMINI_API_KEY`).
    - Creates the Kubernetes Secret (`platform-agent-secrets`) directly in the GKE Namespace.
8.  **`provision_07_deploy_platform_agent.sh`**:
    - Generates `scripts/platform-agent.yaml` and applies the Custom Resource (CR) to deploy the Platform Agent.
9.  **`provision_08_deploy_litellm.sh`**:
    - Deploys the LiteLLM Gateway to the cluster.
10. **`provision_09_deploy_github_minter.sh`**:
    - Sets up Google Cloud KMS keyrings and keys for token signing.
    - Deploys the GitHub Token Minter into the cluster.

---

### 2. Sourcing Variables & Configuration State

On the first execution of `make gcp-provision` (or `provision_01_gcp_cluster.sh`), you will be prompted for target values. These are saved to **`scripts/vars.sh`**.

Subsequent script runs will skip the interactive configuration and automatically load variables from `vars.sh`. To re-configure, edit `vars.sh` directly or delete it to be prompted again.

---

### 3. Running Individual Steps with `make`

You can execute individual provisioning steps in order:

1.  **Step 1: Provision GKE cluster and initial GCP environment**
    ```bash
    make gcp-provision-01-cluster
    ```
2.  **Step 1a: Provision optional gVisor node pool for GKE Sandbox** (Optional)
    ```bash
    make gcp-provision-01a-gvisor
    ```
3.  **Step 2: Install operator CRDs and deploy controller manager**
    ```bash
    make gcp-provision-02-operator
    ```
4.  **Step 3: Configure IAM service accounts and Workload Identity**
    ```bash
    make gcp-provision-03-iam
    ```
5.  **Step 4: Setup Google Chat Pub/Sub topic and subscription** (Optional if using Google Chat)
    ```bash
    make gcp-provision-04-gchat
    ```
6.  **Step 5: Setup Slack integration configuration** (Optional if using Slack)
    ```bash
    make gcp-provision-05-slack
    ```
7.  **Step 6: Configure secrets directly in GKE**
    ```bash
    make gcp-provision-06-secrets
    ```
8.  **Step 7: Deploy the PlatformAgent Custom Resource**
    ```bash
    make gcp-provision-07-deploy
    ```
9.  **Step 8: Deploy LiteLLM Gateway**
    ```bash
    make gcp-provision-08-litellm
    ```
10. **Step 9: Deploy GitHub Token Minter** (Optional if using GitOps)
    ```bash
    make gcp-provision-09-github
    ```

---

### 4. The Teardown Pipeline

To cleanly tear down and delete all provisioned GCP and GKE resources:

```bash
make gcp-teardown
```

Or run the master teardown script directly:

```bash
./scripts/teardown.sh
```
