# 🤖 GKE Standard Operator Deployment Runbook: GChat Platform Agent

This guide outlines the architecture, manual prerequisites, and declarative deployment runbook for the **Hermes Platform Agent** on **GKE Standard** orchestrated dynamically via a custom **Go Kubernetes Operator**.

---

## 🏗️ System Architecture Map

The entire deployment is driven by a custom Kubernetes controller (**`hermes-operator`**) and a single Custom Resource Definition (**`HermesAgent`**). Applying a `HermesAgent` resource triggers the operator to provision GCP and GKE resources dynamically:

```
                     [ GKE Standard Cluster ]
                                │
 1. Apply CRD ──────────────────┼──────────────┐
                                ▼              ▼
                  [ hermes-operator-system ] [ agent-system ]
                  │   (Controller Manager)  │  (ConfigConnectorContext)
                  │                         │
 2. Reconcile ────┼─────────────────────────┼──────────────┐
                  ▼                         ▼              ▼
           [ K8s Resources ]        [ KCC Controller ] [ GCP Secret Manager ]
           - ServiceAccount (KSA)   - GSA & WI bindings    (Gemini keys)
           - PVC (Volume)           - Pub/Sub Topic        │
           - ConfigMap (config.yaml) - Pub/Sub Subscription │ 3. Sync Secrets
           - Deployment (Pod)       - IAM Policy bindings ──┘ (hermes-secrets)
```

---

## 🛠️ Prerequisite Tools

Ensure the following tools are installed on your local terminal:

- `gcloud` (Google Cloud SDK authenticated to your project)
- `kubectl` (Kubernetes CLI)
- `make` & `go` (For building the Go Operator codebase)
- `docker` (For compiling the controller containers)

---

## 🚀 Automated GKE SRE Bootstrap (`provision.sh`)

We have built a unified, idempotent, and fully interactive bootstrap script to completely automate GKE Standard cluster creation, GCP API enablement, secrets sync, Cloud Build of the custom GChat container, operator deployment, and CR generation.

To bootstrap the entire environment in minutes:

1.  Navigate to your CRD folder:
    ```bash
    cd integrations/gchat/crd
    ```
2.  Run the SRE bootstrap script:
    ```bash
    ./provision.sh
    ```
3.  Follow the interactive terminal prompts to supply your target Project ID, Region, Cluster Name, Namespace, and API Secrets.

---

## ☸️ Architectural Walkthrough (What `provision.sh` Does Under the Hood)

For SRE auditing purposes, here is the exact sequence of operations executed dynamically by the SRE script and Go Operator:

### 1. GCP API Enablement

Enables all mandatory APIs required for the GKE platform to function:

- `container.googleapis.com` (GKE Engine)
- `artifactregistry.googleapis.com` (Docker Registries)
- `cloudbuild.googleapis.com` (Google Cloud Build)
- `secretmanager.googleapis.com` (API Secret Vaults)
- `pubsub.googleapis.com` (Google Chat Event Bus)
- `chat.googleapis.com` & `gsuiteaddons.googleapis.com` (GSuite permissions)
- `aiplatform.googleapis.com` (Vertex AI/Gemini keyless API access)

### 2. Artifact Registry Repository

Provisions a secure Docker repository `hermes-agent-repo` in your region to host the operator and agent images.

### 3. GKE Standard Cluster & Workload Identity

Creates a GKE Standard cluster with GKE **Workload Identity** enabled (`--workload-pool=<project-id>.svc.id.goog`), allowing Kubernetes pods to securely authenticate against GCP resources without requiring JSON service account keys!

### 4. Secret Manager Vaults

Bootstraps Google Secret Manager placeholders `GCP_API_KEY` and `GEMINI_API_KEY` to securely store your AI Studio and Vertex access keys in the cloud.

### 5. Secret Synchronization

Pulls the latest key values from Secret Manager and synchronizes them into a local Kubernetes Secret `hermes-secrets` inside the target GKE namespace.

### 6. Custom Chat-Enabled Agent Container (Cloud Build)

Uses Google Cloud Build to package, compile, and push a custom **unpatched** platform agent container `hermes-agent:latest` using [`integrations/gchat/app/cloudbuild.yaml`](file:///usr/local/google/home/mklinowski/Projects/kube-agents-team-fork/integrations/gchat/app/cloudbuild.yaml) and [`integrations/gchat/app/Dockerfile`](file:///usr/local/google/home/mklinowski/Projects/kube-agents-team-fork/integrations/gchat/app/Dockerfile). This container packages `google-cloud-pubsub` and GKE utilities cleanly in the isolated team directory.

### 7. Operator Controller Manager Deployment

Registers the custom resource definition (`make install`) and deploys the custom controller pod `hermes-operator-controller-manager` inside the `hermes-operator-system` namespace.

### 8. Custom Resource Manifest Generation & Apply

Generates a clean, declarative `platform-agent.yaml` Custom Resource manifest and applies it to GKE:

```yaml
apiVersion: agent.hermes.io/v1alpha1
kind: HermesAgent
metadata:
  name: platform-agent
  namespace: agent-system
spec:
  projectId: "mklinowski-gkedemos"
  numericProjectId: "7774963878"
  clusterName: "platform-agent-host"
  location: "us-central1"
  imageUri: "us-central1-docker.pkg.dev/mklinowski-gkedemos/hermes-agent-repo/hermes-agent:latest"
  chatTopicName: "hermes-chat-events"
  chatSubName: "hermes-chat-events-sub"
  gsaName: "hermes-bot-platform-agent"
  ksaName: "hermes-platform-sa"
  googleChatAllowedUsers: "mklinowski@google.com"
  googleChatHomeChannel: ""
```

---

## 💬 Step 9: Complete Google Chat Bot Registration

Once your pods rollout successfully, configure the Bot webhook connection in GCP Console:

1.  Go to the **Google Cloud Console** ➡️ **Google Chat API** ➡️ **Configuration**.
2.  Disable _"Build this chat app as a workspace-addon"_.
3.  Set App Name: `GKE Hermes Platform Bot`
4.  Under **Connection Settings**, select **Cloud Pub/Sub**.
5.  Cloud Pub/Sub Topic Name: `projects/<project-id>/topics/hermes-chat-events`
6.  Add your allowed email address under **Visibility**.
7.  Click **Save**.
8.  Send a DM to the Bot on Google Chat (`Hi Hermes`) and approve the resulting **Pairing Code** inside the container to finalize the E2E loop:
    ```bash
    kubectl exec -it deploy/platform-agent-gateway -n agent-system -c hermes -- hermes pairing approve google_chat <PAIRING_CODE>
    ```
