# Deployment Guide: kube-agents

This guide provides instructions on how to deploy the GKE Platform Agent and Operator stack. 

Depending on your development environment and security constraints, you can choose from five different deployment methods.

---

## Choosing Your Deployment Method

| Method | Best For | State Management | Prerequisites |
| :--- | :--- | :--- | :--- |
| **[1. Google Cloud Shell (Baseline)](#method-1-google-cloud-shell-baseline-scripts)** | Quick, simple procedural deployment in Google Cloud Shell without Terraform. | **None** (Procedural `gcloud` and `helm` calls) | Google Cloud Shell. |
| **[2. IaC Scripts (Local Workstation)](#method-2-iac-scripts-local-workstation)** | Workstations wanting fast local iteration using Terraform. | **Local File** (Stored in `deploy/terraform/`) | Local `gcloud`, `terraform`, `helm`, `kubectl`. |
| **[3. Manual Step-by-Step](#method-3-manual-step-by-step-deployment-alternative)** | Debugging, learning the architecture, or custom fine-grained resource changes. | **Local File** (Must be managed manually) | Local `gcloud`, `terraform`, `helm`, `kubectl`. |
| **[4. Cloud Shell (IaC Wrapper)](#method-4-automated-deployment-via-google-cloud-shell-iac-wrapper)** | Working from Cloud Shell with Terraform, utilizing an existing host cluster for state. | **Automated** (Stored in GKE host cluster Secret) | Google Cloud Shell, `kubectl` access to host cluster. |
| **[5. Cloud Shell (Raw IaC Scripts)](#method-5-google-cloud-shell-raw-iac-scripts)** | Bootstrapping your very first GKE cluster from Cloud Shell using Terraform (no host cluster yet). | **Local File** (Stored on Cloud Shell VM home directory) | Google Cloud Shell. |

---

## Method 1: Google Cloud Shell (Baseline Scripts)

This is the simplest way to deploy the stack if you are working from **Google Cloud Shell** and do not want to use Terraform. It uses a series of procedural bash scripts to bootstrap the GKE cluster, configure IAM, and deploy the workloads via Helm.

### Step 1.1: Clone the Repository

Open Google Cloud Shell and clone the repository:

```bash
git clone https://github.com/gke-labs/kube-agents.git
cd kube-agents
```

### Step 1.2: Run the Provisioning Script

Run the master provisioner script:

```bash
# Ensure your GEMINI_API_KEY is set
export GEMINI_API_KEY="your-api-key"

./k8s-operator/scripts/provision.sh [arguments]
```

This script will sequentially execute the bootstrap scripts (`provision_01_...` through `provision_08_...`) to set up the GKE cluster, build the operator image, configure IAM, Pub/Sub, and deploy the Helm chart.

### Step 1.3: Run the Teardown Script (Optional)

To destroy the cluster and all associated resources, run:

```bash
./k8s-operator/scripts/teardown.sh [arguments]
```

---

## Method 2: IaC Scripts (Local Workstation)

If you are deploying from a local workstation, you can run the IaC scripts directly from your terminal. This uses Terraform to manage the GKE cluster and IAM, and Helm for the workloads.

*Note: The Terraform state is stored as a local file on your machine.*

### Step 2.1: Run the Provisioning Script

Run [provision_iac.sh](scripts/provision_iac.sh) directly:

```bash
# Ensure your GEMINI_API_KEY is set
export GEMINI_API_KEY="your-api-key"

./k8s-operator/scripts/provision_iac.sh [arguments]
```

#### Required Arguments:
*   `-p, --project-id VALUE`: Target GCP Project ID.
*   `-r, --region VALUE`: GCP Region for the target GKE cluster (e.g., `us-east4`).
*   `-c, --cluster-name VALUE`: Target GKE Cluster Name.
*   `-n, --namespace VALUE`: Kubernetes namespace for the workloads (e.g., `kubeagents-system`).

#### Optional Arguments:
*   `-m, --model-provider VALUE`: Model Provider: `gemini`, `anthropic`, `openai`, `chatgpt` (default: `gemini`).
*   `-d, --model-default-name VALUE`: Default Model Name (default: `gemini-3.5-flash`).
*   `-u, --allowed-users VALUE`: Comma-separated list of allowed Google Chat users.
*   `-go, --github-org VALUE`, `-gr, --github-repo VALUE`, `-ga, --github-app-id VALUE`, `-gp, --github-pem-path VALUE`: GitHub integration settings.

*Example:*
```bash
./k8s-operator/scripts/provision_iac.sh \
  -p your-project-id \
  -r us-east4 \
  -c kube-agents-dedicated-cluster \
  -n kubeagents-system
```

### Step 2.2: Run the Teardown Script (Optional)

To destroy the target GKE cluster and all associated GCP resources, run [teardown_iac.sh](scripts/teardown_iac.sh):

```bash
./k8s-operator/scripts/teardown_iac.sh [arguments]
```

*Note: You must keep the local `terraform.tfstate.<cluster-name>` file located in `deploy/terraform/` safe.*

---

## Method 3: Manual Step-by-Step Deployment (Alternative)

This section provides step-by-step instructions on how to manually deploy the stack using Terraform and Helm directly from your local workstation. This is useful for debugging or custom configurations.

### Prerequisites

Ensure you have the following CLI tools installed locally:
* [Google Cloud SDK (gcloud)](https://cloud.google.com/sdk/docs/install)
* [Terraform (>= 1.3.0)](https://developer.hashicorp.com/terraform/downloads)
* [Helm (>= 3.0.0)](https://helm.sh/docs/intro/install/)
* [kubectl](https://kubernetes.io/docs/tasks/tools/)
* [openssl](https://www.openssl.org/) (for generating API keys)

### Step 3.1: Provision GCP Infrastructure with Terraform

1. Navigate to the Terraform directory:
   ```bash
   cd k8s-operator/deploy/terraform
   ```

2. Initialize and apply:
   ```bash
   terraform init
   terraform apply \
     -var="project_id=your-project-id" \
     -var="region=us-east4" \
     -var="cluster_name=kube-agents-dedicated-cluster" \
     -var="namespace=kubeagents-system"
   ```

### Step 3.2: Configure kubectl Credentials

```bash
gcloud container clusters get-credentials kube-agents-dedicated-cluster \
  --region us-east4 \
  --project your-project-id
```

### Step 3.3: Deploy the Workloads via Helm

1. Generate a secure API Server Key:
   ```bash
   API_SERVER_KEY=$(openssl rand -hex 16)
   ```

2. Run Helm:
   ```bash
   helm upgrade --install kube-agents ../helm/kube-agents \
     --namespace "kubeagents-system" \
     --create-namespace \
     --set projectId="your-project-id" \
     --set clusterName="kube-agents-dedicated-cluster" \
     --set clusterLocation="us-east4" \
     --set operator.controllerGsaEmail="OUTPUT_CONTROLLER_GSA_EMAIL" \
     --set agents.platform.gsaEmail="OUTPUT_PLATFORM_AGENT_GSA_EMAIL" \
     --set agents.operator.gsaEmail="OUTPUT_OPERATOR_AGENT_GSA_EMAIL" \
     --set agents.devteam.gsaEmail="OUTPUT_DEVTEAM_AGENT_GSA_EMAIL" \
     --set keys.geminiApiKey="YOUR_GEMINI_API_KEY" \
     --set keys.apiServerKey="${API_SERVER_KEY}"
   ```

---

## Method 4: Automated Deployment via Google Cloud Shell (IaC Wrapper)

If you want to use Terraform IaC from **Google Cloud Shell** while maintaining state persistence in the host cluster's Secret, use the [deploy_from_cloud_shell.sh](scripts/deploy_from_cloud_shell.sh) wrapper script.

### Step 4.1: Clone the Repository

Open Google Cloud Shell and clone the repository:

```bash
git clone https://github.com/gke-labs/kube-agents.git
cd kube-agents
```

### Step 4.2: Run the Wrapper Script

```bash
# Ensure your GEMINI_API_KEY is set
export GEMINI_API_KEY="your-api-key"

./k8s-operator/scripts/deploy_from_cloud_shell.sh [provision|teardown] [arguments]
```

*Example (Provisioning):*
```bash
./k8s-operator/scripts/deploy_from_cloud_shell.sh provision \
  -p your-project-id \
  -r us-east4 \
  -c kube-agents-dedicated-cluster \
  -n kubeagents-system
```

*Note:*
*   **Context**: Before running the script, ensure your local `kubectl` context is set to the **host cluster** (where the state Secret is stored, e.g., `autopilot-cluster-1`).
*   **Permissions**: Since this runs directly on the Cloud Shell VM, it uses your active `gcloud` credentials, bypassing any container-related token restrictions.

---

## Method 5: Google Cloud Shell (Raw IaC Scripts)

If you are working in **Google Cloud Shell** but do not have an existing "host" GKE cluster yet (for example, you are bootstrapping your very first GKE cluster in the project), you cannot use Method 4. Instead, you can run the raw IaC scripts directly on the Cloud Shell VM.

The Terraform state will be stored locally in the Cloud Shell VM's persistent home directory.

### Step 5.1: Clone the Repository

Open Google Cloud Shell and clone the repository:

```bash
git clone https://github.com/gke-labs/kube-agents.git
cd kube-agents
```

### Step 5.2: Run the Provisioning Script

Run [provision_iac.sh](scripts/provision_iac.sh) directly:

```bash
# Ensure your GEMINI_API_KEY is set
export GEMINI_API_KEY="your-api-key"

./k8s-operator/scripts/provision_iac.sh [arguments]
```

*Example:*
```bash
./k8s-operator/scripts/provision_iac.sh \
  -p your-project-id \
  -r us-east4 \
  -c kube-agents-dedicated-cluster \
  -n kubeagents-system
```

### Step 5.3: Run the Teardown Script (Optional)

To destroy the cluster, run [teardown_iac.sh](scripts/teardown_iac.sh):

```bash
./k8s-operator/scripts/teardown_iac.sh [arguments]
```

*Note: The state file `terraform.tfstate.<cluster-name>` will be saved in `deploy/terraform/` on your Cloud Shell VM. Since Cloud Shell home directories are persistent, the state will be preserved across sessions, but it is not backed up in the cluster.*

---

## Step 6: Verify the Rollout

Regardless of the method used, verify that all pods are successfully created and running in the target namespace:

```bash
kubectl get pods -n kubeagents-system
```

You should see the following pods in the `Running` state:
* `kubeagents-controller-manager-...` (The operator)
* `litellm-...` (LiteLLM gateway - 2 replicas)
* `platform-agent-gateway-...` (The platform agent bot gateway - 2/2 containers running)
