# Kubernetes Agentic Harness Operator

This directory contains the Kubernetes Operator for the `kube-agents` harness. The operator defines and manages the lifecycle of agent custom resources:

- **PlatformAgent**: Manages platform-level configuration and capabilities.

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

## Deployment & Operations

For instructions on bootstrapping GCP/GKE infrastructure, capacity planning, cluster sizing, API key configuration, and deploying integrations (LiteLLM, GitHub), please refer to the [Kube-Agents Deployment & Operations Guide](../docs/deployment.md).

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
> This compiles and runs the entry point [main.go](cmd/main.go) with webhooks disabled. The process runs in the foreground, prints reconciliation logs, and watches for custom resource events in the cluster.

### Step 4: Apply Sample Custom Resources

In another terminal window, apply the sample custom resources to test the controllers:

```bash
kubectl apply -f examples/platformagent.yaml
```

Verify that the resources are created and recognized:

```bash
kubectl get platformagents --all-namespaces
```

You should see reconciliation logs printed in the terminal where the operator process is running.

### Step 5: Clean Up Local Resources

To stop the operator, press `Ctrl+C` in the terminal where it is running.
To uninstall the CRDs from the cluster:

```bash
make uninstall
```

### Fast Local Development & Testing (Agent Workspace)

For fast local iteration when updating agent skills, prompts, or code without waiting for CI/CD pipelines, you can use the dedicated rebuild script or `make` target:

```bash
# Run interactively via make
make dev-rebuild-agent

# Or specify arguments directly
make dev-rebuild-agent ARGS="platform"
```

- **`scripts/dev/dev_rebuild_agent.sh`**:
  - Prompts for or accepts an agent target (`platform`).
  - Ensures the GCP Artifact Registry repository exists.
  - Builds and pushes the updated container image via Google Cloud Build (or locally with `--local`).
  - Automatically updates any running Custom Resources and rolling-restarts Kubernetes Deployments in GKE with the new image.

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

## Makefile Reference

The [Makefile](Makefile) provides several targets to automate development workflows:

| Target                                    | Description                                                              |
| :---------------------------------------- | :----------------------------------------------------------------------- |
| `make gcp-provision`                      | Bootstraps all GCP, GKE resources, and deploys the PlatformAgent.        |
| `make gcp-teardown`                       | Cleans up and deletes all provisioned GKE/GCP resources.                 |
| `make gcp-provision-01-cluster`           | Step 1: Provision GKE cluster and initial GCP environment.               |
| `make gcp-provision-02-operator`          | Step 2: Install operator CRDs and deploy controller manager.             |
| `make gcp-provision-03-iam`               | Step 3: Configure IAM service accounts and Workload Identity.            |
| `make gcp-provision-04-secrets`           | Step 4: Configure secrets directly in GKE.                               |
| `make gcp-provision-05-gchat`             | Step 5: Setup Google Chat Pub/Sub topic and subscription.                |
| `make gcp-provision-06-deploy`            | Step 6: Deploy the PlatformAgent Custom Resource.                        |
| `make dev-rebuild-agent`                  | Fast local iteration: rebuild and redeploy an agent image.               |
| `make gcp-teardown-06-deploy`             | Teardown Step 6: Delete the PlatformAgent Custom Resource.               |
| `make gcp-teardown-05-gchat`              | Teardown Step 5: Delete Google Chat Pub/Sub resources.                   |
| `make gcp-teardown-04-secrets`            | Teardown Step 4: Clean up Kubernetes secrets.                            |
| `make gcp-teardown-03-iam`                | Teardown Step 3: Remove IAM service accounts and policies.               |
| `make gcp-teardown-02-operator`           | Teardown Step 2: Undeploy the operator and CRDs.                         |
| `make gcp-teardown-dev-artifact-registry` | Teardown Dev Step: Delete Artifact Registry created during dev rebuilds. |
| `make gcp-teardown-01-cluster`            | Teardown Step 1: Delete GKE cluster and local configuration state.       |
| `make manifests`                          | Generates WebhookConfiguration, ClusterRole, and CRDs.                   |
| `make generate`                           | Generates code containing DeepCopy implementations.                      |
| `make fmt`                                | Formats Go source code using `go fmt`.                                   |
| `make vet`                                | Examines Go source code and reports suspect constructs.                  |
| `make test`                               | Runs unit/integration tests with `setup-envtest`.                        |
| `make build`                              | Compiles the manager binary to `bin/manager`.                            |
| `make run`                                | Runs the controller locally from your host (with webhooks disabled).     |
| `make docker-build`                       | Builds the Docker image.                                                 |
| `make docker-push`                        | Pushes the Docker image to the registry.                                 |
| `make install`                            | Installs the generated CRDs into the cluster.                            |
| `make uninstall`                          | Removes the CRDs from the cluster.                                       |
| `make deploy`                             | Deploys the controller to the cluster.                                   |
| `make undeploy`                           | Removes the controller deployment from the cluster.                      |

---

## Key Files & Code Pointers

- **Main Entrypoint**: [main.go](cmd/main.go)
- **Controllers**:
  - [PlatformAgent Controller](internal/controller/platformagent_controller.go)
- **Example Resource**: [platformagent.yaml](examples/platformagent.yaml)
- **Makefile**: [Makefile](Makefile)
