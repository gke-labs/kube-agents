# Kubernetes Agentic Harness Operator

This directory contains the Kubernetes Operator for the `kube-agents` harness. The operator defines and manages the lifecycle of agent custom resources:

- **PlatformAgent**: Manages platform-level configuration and capabilities.

The operator is built using the Kubebuilder framework and is written in Go.

---

## Prerequisites

Before building or deploying the operator, ensure you have the following installed:

- [Go](https://go.dev/doc/install) (version 1.27+)
- [Docker](https://docs.docker.com/get-docker/) or Podman (for building container images)
- [kubectl](https://kubernetes.io/docs/tasks/tools/) (configured to access your Kubernetes/GKE cluster)
- Access to a running Kubernetes/GKE cluster
- [gcloud](https://cloud.google.com/sdk/docs/install) (for GKE cluster access)

---

## Bootstrapping GCP & GKE Infrastructure

The install engine is Terraform + Helm: `terraform/examples/full-install` owns every GCP
resource and `charts/kube-agents` every Kubernetes resource. To stand up a real GKE/GCP
environment, use the repository-root installer, or drive the composition directly:

```bash
# The zero-friction path: interview, terraform.tfvars generation, apply.
../install.sh

# Or hand-driven, with your own tfvars:
cd ../terraform/examples/full-install
cp terraform.tfvars.example terraform.tfvars   # then edit it
./lifecycle.sh apply
```

Teardown is `../uninstall.sh`, or `./lifecycle.sh destroy` from the composition directory. See
[INSTALL.md](../INSTALL.md) for the full walkthrough and
[scripts/installer/README.md](../scripts/installer/README.md) for the installer's shared
helpers and the `install.env` configuration model. Those helpers used to live in this
directory; they moved out because they serve the installer, not the operator.

For fast local iteration when updating agent skills, prompts, or code without waiting for CI/CD
pipelines, use the dedicated rebuild script or `make` target:

```bash
# Run interactively via make
make dev-rebuild-agent

# Or specify arguments directly
make dev-rebuild-agent ARGS="platform"
```

- **[scripts/dev/dev_rebuild_agent.sh](../scripts/dev/dev_rebuild_agent.sh)**:
  - Prompts for or accepts an agent target (`platform`).
  - Ensures the GCP Artifact Registry repository exists.
  - Builds and pushes the updated container image via Google Cloud Build (or locally with `--local`).
  - Automatically updates any running Custom Resources and rolling-restarts Kubernetes Deployments in GKE with the new image.

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
> This applies the CRD manifests **as committed** in `config/crd/bases/`, via `kustomize`. It does not run `controller-gen`, so edits to the Go API types do not reach the cluster until you run `make manifests` and install again. How the build targets and CI keep generated output in sync is covered in the [operator development guide](../docs/site/src/content/docs/operator/development.md).

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

When webhooks are enabled, the server binds `10250` rather than Kubebuilder's usual `9443`: it is one of only two ports GKE's automatic control-plane-to-node firewall rule permits, so a private cluster reaches the webhook without a hand-added VPC rule. Where 10250 is not the reachable port, `--webhook-port`, the manager `containerPort`, and the Service `targetPort` have to be changed together — the flag alone moves the listener and leaves the Service dialing a dead port, which fail-closed admission turns into a wedged cluster. The rationale, the Kustomize patch that moves all three, the drift guard across them, and the recovery steps for an unreachable webhook are in [Admission webhooks](../docs/site/src/content/docs/operator/index.md#admission-webhooks).

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
# Replace with your actual registry. The tag must be immutable: `make deploy`
# refuses a floating tag such as `latest`, because it lets a pod reschedule
# upgrade the controller past the RBAC applied with it (issue #1009).
export IMG=us-central1-docker.pkg.dev/ai-platform-1-464114/k8s-harness-poc/kube-agents-operator:$(git rev-parse HEAD)

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

> [!NOTE]
> LiteLLM is deployed automatically by the kube-agents Helm chart (`litellm.enabled`, default true). The following instructions are for manual standalone kustomize deployment.

LiteLLM gateway can be deployed to the Kubernetes cluster using the `kustomize` targets in the Makefile.

### Prerequisites

To successfully deploy LiteLLM, you must have:

1. The `platform-agent-secrets` Secret created in your destination namespace (containing `GEMINI_API_KEY`, `ANTHROPIC_API_KEY`, or `OPENAI_API_KEY`).

### Step-by-Step Deployment

Run the `make deploy-litellm` target, passing the required environment variables:

```bash
# 1. Define model provider and default model name:
export MODEL_PROVIDER=gemini
export MODEL_DEFAULT_NAME=gemini-3.5-flash

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
2. Completed the Workload Identity and GCP Cloud KMS setup (see [config/integrations/github/README.md](config/integrations/github/README.md) for details).

### Step-by-Step Deployment

Run the `make deploy-github` target, passing the required environment variables. The KSA/GSA names below are the same defaults the installer uses (see [`scripts/installer/installer_common.sh`](../scripts/installer/installer_common.sh) and [`scripts/installer/common.sh`](../scripts/installer/common.sh)), but they still have to be exported here: `make deploy-github` renders the manifests with `envsubst` and does not source `common.sh`, so an unset variable would be substituted as an empty string.

`KMS_LOCATION` is the Cloud KMS location, which is separate from `REGION`, the GKE cluster location. Cloud KMS has no zonal locations, so the two differ for a zonal cluster: a cluster in `us-central1-c` needs `KMS_LOCATION=us-central1`. For a regional cluster they are the same value.

`GITHUB_ORG` must name a GitHub organization, not a user: the Minter resolves installations at `/orgs/{org}/installation`, which returns 404 for personal accounts. This manual path bypasses the installer's check for it — see [`config/integrations/github/README.md`](config/integrations/github/README.md).

```bash
# 1. Define the GCP and GitHub parameter variables:
export PROJECT_ID=your-gcp-project-id
export REGION=your-gcp-region
export CLUSTER_NAME=your-gke-cluster-name
export KMS_LOCATION=your-kms-region
export KMS_KEYRING=your-kms-keyring
export KMS_KEY=your-kms-key
export KMS_KEY_VERSION=your-kms-key-version
export GITHUB_ORG=your-github-org
export GITHUB_REPO=your-github-repo
export GITHUB_MINTER_KSA_NAME=kubeagents-github-minter
export GITHUB_MINTER_GSA_NAME=kubeagents-github-minter-gsa
export PLATFORM_AGENT_GSA_NAME=kubeagents-platform-gsa

# 2. Deploy GitHub:
make deploy-github
```

To uninstall/remove the GitHub integration:

```bash
make undeploy-github
```

---

## Makefile Reference

```bash
make help
```

`make help` prints every documented target with its description, generated from the Makefile.
It replaces the table that previously lived here, which had to be updated by hand whenever a
target changed.

---

## Key Files & Code Pointers

- **Main Entrypoint**: [main.go](cmd/main.go)
- **Controllers**:
  - [PlatformAgent Controller](internal/controller/platformagent_controller.go)
- **Example Resource**: [platformagent.yaml](examples/platformagent.yaml)
- **Makefile**: [Makefile](Makefile)
