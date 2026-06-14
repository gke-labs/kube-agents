# Kubernetes Agentic Harness Operator

This directory contains the Kubernetes Operator for the `kube-agents` harness. The operator defines and manages the lifecycle of agent custom resources:

- **PlatformAgent**: Manages platform-level configuration and capabilities.
- **DevTeamAgent**: Manages developer-team-specific configurations and workspaces.
- **OperatorAgent**: Manages operational policies and task execution.

The operator is built using the Kubebuilder framework and is written in Go.

---

## Prerequisites

Before building or deploying the operator, ensure you have the following installed:

- [Go](https://go.dev/doc/install) (version 1.24+)
- [Docker](https://docs.docker.com/get-docker/) or Podman (for building container images)
- [kubectl](https://kubernetes.io/docs/tasks/tools/) (configured to access your Kubernetes/GKE cluster)
- Access to a running Kubernetes/GKE cluster
- [gcloud](https://cloud.google.com/sdk/docs/install) (for GKE cluster access)

---

## Local Development (Fast Iteration)

For local development and testing, you can run the operator controller as a local Go process on your machine, while pointing it to a remote GKE or local Kubernetes cluster. This bypasses the need to build and push container images on every code change.

### Step 1: Set Active Kubernetes Context

Ensure your `kubectl` is pointed to the correct cluster:

```bash
# Check the active context
kubectl config current-context

# If needed, authenticate and switch to your GKE cluster
gcloud container clusters get-credentials <CLUSTER_NAME> --zone <ZONE> --project <PROJECT_ID>
```

### Step 2: Install the Custom Resource Definitions (CRDs)

Register the operator's Custom Resource Definitions (CRDs) with the cluster:

```bash
make install
```

> [!NOTE]
> This command uses `controller-gen` to generate the CRD manifests from Go structs and applies them to the cluster via `kustomize`.

### Step 3: Run the Operator Locally

Start the operator controller process. Because admission webhooks require TLS certificates (typically managed by cert-manager when running inside the cluster), you should run the operator locally with webhooks disabled by setting the `ENABLE_WEBHOOKS=false` environment variable:

```bash
ENABLE_WEBHOOKS=false make run
```

Or directly run the main entry point:

```bash
ENABLE_WEBHOOKS=false go run ./cmd/main.go
```

> [!TIP]
> This compiles and runs the entry point [main.go](file:///usr/local/google/home/fatoshoti/playground/kube-agents/k8s-operator/cmd/main.go) with webhooks disabled. The process runs in the foreground, prints reconciliation logs, and watches for custom resource events in the cluster.

### Step 4: Apply Sample Custom Resources

In another terminal window, apply the sample custom resources to test the controllers:

```bash
kubectl apply -f examples/platformagent.yaml
kubectl apply -f examples/clusteroperatoragent.yaml
kubectl apply -f examples/devteamagent.yaml
```

Verify that the resources are created and recognized:

```bash
kubectl get platformagents,operatoragent,devteamagent --all-namespaces
```

You should see reconciliation logs printed in the terminal where the operator process is running.

### Step 5: Clean Up Local Resources

To stop the operator, press `Ctrl+C` in the terminal where it is running.
To uninstall the CRDs from the cluster:

```bash
make uninstall
```

---

## Building and Deploying to GKE

When you are ready to deploy the operator as a deployment inside the cluster, use the following steps.

### Step 1: Build and Push the Docker Image

Build the container image and push it to a container registry (e.g., Google Artifact Registry) accessible by your GKE cluster.

#### 1. Authenticate Docker with the Registry

Before pushing, ensure your local Docker client is authenticated with Google Cloud's container registries. Run the command matching your registry domain:

```bash
# For Google Artifact Registry (recommended, e.g. us-central1 region)
gcloud auth configure-docker us-central1-docker.pkg.dev

# For Google Container Registry (legacy)
gcloud auth configure-docker gcr.io
```

#### 2. Build and Push

Set the image target URL and run the build/push targets:

```bash
# Replace with your actual registry and image tag
export IMG=us-central1-docker.pkg.dev/ai-platform-1-464114/k8s-harness-poc/kube-agents-operator:latest

# Build the image
make docker-build IMG=$IMG

# Push the image to the registry
make docker-push IMG=$IMG
```

### Step 2: Deploy the Operator Controller

Deploy the operator deployment, RBAC permissions, and CRDs into the cluster:

```bash
make deploy IMG=$IMG
```

### Step 3: Verify the Deployment

Check the status of the operator deployment:

```bash
kubectl get deployments -n kubeagents-system
kubectl get pods -n kubeagents-system
```

---

## Deploying LiteLLM Integration

LiteLLM gateway can be deployed to the Kubernetes cluster using the `kustomize` targets in the Makefile.

### Prerequisites

To successfully deploy LiteLLM, you must have:

1. The `platform-agent-secrets` Secret created in your destination namespace (containing `GEMINI_API_KEY`).

### Step-by-Step Deployment

Run the `make deploy-litellm` target, passing the required environment variables:

```bash
# 1. Define the destination namespace, model provider, and default model name:
export NAMESPACE=kubeagents-system
export MODEL_PROVIDER=gemini
export MODEL_DEFAULT_NAME=gemini-3.1-flash

# 2. Deploy LiteLLM:
make deploy-litellm
```

To uninstall/remove the LiteLLM integration:

```bash
make undeploy-litellm
```

---

## Deploying GitHub Integration

The GitHub Token Broker (Minty) can be deployed to the Kubernetes cluster using the `kustomize` targets in the Makefile.

### Prerequisites

Before deploying the GitHub integration, ensure you have:

1. Created the `github-app-credentials` Secret containing your GitHub App ID in the destination namespace.
2. Completed the Workload Identity and GCP Cloud KMS setup (see [integrations/github/README.md](integrations/github/README.md) for details).

### Step-by-Step Deployment

Run the `make deploy-github` target, passing the required environment variables:

```bash
# 1. Define the destination namespace and GCP/GitHub parameter variables:
export NAMESPACE=kubeagents-system
export PROJECT_ID=your-gcp-project-id
export REGION=your-gcp-region
export CLUSTER=your-gke-cluster-name
export KEYRING=your-kms-keyring
export KEY=your-kms-key
export KEY_VERSION=your-kms-key-version
export GITHUB_ORG=your-github-org
export GITHUB_REPO=your-github-repo

# 2. Deploy GitHub:
make deploy-github
```

To uninstall/remove the GitHub integration:

```bash
make undeploy-github
```

---

## Makefile Reference

The [Makefile](file:///usr/local/google/home/fatoshoti/playground/kube-agents/k8s-operator/Makefile) provides several targets to automate development workflows:

| Target                  | Description                                             |
| :---------------------- | :------------------------------------------------------ |
| `make manifests`        | Generates WebhookConfiguration, ClusterRole, and CRDs.  |
| `make generate`         | Generates code containing DeepCopy implementations.     |
| `make fmt`              | Formats Go source code using `go fmt`.                  |
| `make vet`              | Examines Go source code and reports suspect constructs. |
| `make test`             | Runs unit/integration tests with `setup-envtest`.       |
| `make build`            | Compiles the manager binary to `bin/manager`.           |
| `make run`              | Runs the controller locally from your host.             |
| `make docker-build`     | Builds the Docker image.                                |
| `make docker-push`      | Pushes the Docker image to the registry.                |
| `make install`          | Installs the generated CRDs into the cluster.           |
| `make uninstall`        | Removes the CRDs from the cluster.                      |
| `make deploy`           | Deploys the controller to the cluster.                  |
| `make undeploy`         | Removes the controller deployment from the cluster.     |
| `make deploy-litellm`   | Deploys the LiteLLM integration.                        |
| `make undeploy-litellm` | Removes the LiteLLM integration.                        |
| `make deploy-github`    | Deploys the GitHub integration.                         |
| `make undeploy-github`  | Removes the GitHub integration.                         |

---

## Key Files & Code Pointers

- **Main Entrypoint**: [main.go](file:///usr/local/google/home/fatoshoti/playground/kube-agents/k8s-operator/cmd/main.go)
- **Controllers**:
  - [PlatformAgent Controller](file:///usr/local/google/home/fatoshoti/playground/kube-agents/k8s-operator/internal/controller/platformagent_controller.go)
  - [DevTeamAgent Controller](file:///usr/local/google/home/fatoshoti/playground/kube-agents/k8s-operator/internal/controller/devteamagent_controller.go)
  - [OperatorAgent Controller](file:///usr/local/google/home/fatoshoti/playground/kube-agents/k8s-operator/internal/controller/operatoragent_controller.go)
- **Example Resource**: [platformagent.yaml](file:///usr/local/google/home/fatoshoti/playground/kube-agents/k8s-operator/examples/platformagent.yaml)
- **Makefile**: [Makefile](file:///usr/local/google/home/fatoshoti/playground/kube-agents/k8s-operator/Makefile)
