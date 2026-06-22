# KubeAgents Setup on GKE

## Setup k8s operator

0. Set the project

```bash
export PROJECT_ID="<PROJECT_ID>"
export REGION="<REGION>"
export CLUSTER_NAME="<CLUSTER_NAME>"

gcloud config set project $PROJECT_ID
gcloud auth application-default login
```

1. Enable APIs

```bash
gcloud services enable container.googleapis.com
```

2. Create a cluster and authenticate to it

```bash
gcloud container clusters create $CLUSTER_NAME --region $REGION --workload-pool ${PROJECT_ID}.svc.id.goog
gcloud container clusters get-credentials $CLUSTER_NAME --region $REGION
```

3. Create required secrets

```bash
# [TODO] Fill in the actual MODEL API key
export API_KEY="<API_KEY>"
export HERMES_API_KEY="<HERMES_API_KEY>"
export GITHUB_KEY="<GITHUB_KEY>"

kubectl create secret generic "platformagent-secrets" --namespace $NAMESPACE \
  --from-literal="api-key"="$API_KEY"

kubectl create secret generic "platformagent-secrets" --namespace $NAMESPACE \
  --from-literal="hermes-api-key"="$HERMES_API_KEY"

kubectl create secret generic "platformagent-secrets" --namespace $NAMESPACE \
  --from-literal="github-key"="$GITHUB_KEY"
```

4. Install the k8s operator

```bash
git clone https://github.com/gke-labs/kube-agents.git
cd kube-agents/k8s-operator
make deploy
make deploy-github
make deploy-litellm
```

5. Prepare env variables

```bash

export NAMESPACE="kubeagents-system"
export KSA_NAME="kubeagents-controller"
export GSA_NAME="kubeagents-controller-gsa"
export GSA_EMAIL="${GSA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

```

6. Set up Workload Identity and IAM to enable controller-manager to access gcp services and create resources on managed clusters.

```bash
# 1. Create the Google Service Account (GSA)
gcloud iam service-accounts create $GSA_NAME \
    --project=$PROJECT_ID \
    --display-name="Kubeagents Controller Manager GSA"

# 2. Bind the KSA in the hosting Cluster to the GSA (Workload Identity)
gcloud iam service-accounts add-iam-policy-binding $GSA_EMAIL \
    --project=$PROJECT_ID \
    --role roles/iam.workloadIdentityUser \
    --member "serviceAccount:${PROJECT_ID}.svc.id.goog[${NAMESPACE}/${KSA_NAME}]"

# 3. Grant the GSA basic API access to the cluster (allows fetching cluster info)
gcloud projects add-iam-policy-binding $PROJECT_ID \
    --member "serviceAccount:$GSA_EMAIL" \
    --role "roles/container.clusterViewer"

# 4. Grant the GSA admin access to the cluster (allows creating resources)
gcloud projects add-iam-policy-binding $PROJECT_ID \
    --member "serviceAccount:$GSA_EMAIL" \
    --role "roles/container.admin"

# 5. Grant the GSA admin access for managing clusters and their lifecycle
gcloud projects add-iam-policy-binding $PROJECT_ID \
    --member "serviceAccount:$GSA_EMAIL" \
    --role "roles/container.clusterAdmin"
```

7. Annotate the KSA

```bash

kubectl annotate serviceaccount $KSA_NAME --namespace $NAMESPACE iam.gke.io/gcp-service-account=$GSA_EMAIL

```

# Agent permission setup

1. Create Google Service Account for the Platform Agent

```bash
export PROJECT_ID="<PROJECT_ID>"

# Uncomment one line below based on the agent you want to configure.

# export AGENT_GSA_DISPLAY_NAME="Platform Agent GSA"
# export AGENT_GSA_DISPLAY_NAME="ClusterOperator Agent GSA"
# export AGENT_GSA_DISPLAY_NAME="DevTeam Agent GSA"


# Uncomment one line below based on the agent you want to configure.

# export GSA_NAME="platform-agent-gsa"
# export GSA_NAME="clusteroperator-agent-gsa"
# export GSA_NAME="devteam-agent-gsa"

export GSA_EMAIL="${GSA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

# Uncomment one line below based on the agent you want to configure.

# export KSA_NAME="platform-agent"
# export KSA_NAME="clusteroperator-agent"
# export KSA_NAME="devteam-agent"

gcloud iam service-accounts create $GSA_NAME \
    --project=$PROJECT_ID \
    --display-name="${AGENT_GSA_DISPLAY_NAME}"
```

2.1 Grant the Agent access to the cluster (allows fetching cluster info)

```bash
gcloud projects add-iam-policy-binding $PROJECT_ID \
    --member "serviceAccount:$GSA_EMAIL" \
    --role "roles/container.clusterViewer"
```

2.2 Grant the Agent read-only access to cluster resources

```bash
gcloud projects add-iam-policy-binding $PROJECT_ID \
    --member "serviceAccount:$GSA_EMAIL" \
    --role "roles/container.viewer"
```

2.3 Grant the Agent developer permissions on the cluster

```bash
gcloud projects add-iam-policy-binding $PROJECT_ID \
    --member "serviceAccount:$GSA_EMAIL" \
    --role "roles/container.developer"
```

2.4 Grant the Agent admin access to the cluster (optional, for Operator Agent only)

```bash
gcloud projects add-iam-policy-binding $PROJECT_ID \
    --member "serviceAccount:$GSA_EMAIL" \
    --role "roles/container.admin"
```

> [!NOTE]
> **Least Privilege & Separation of Concerns**: To prevent the Platform Agent from running `kubectl` commands directly or having direct administrative access to GKE clusters, it is **not** granted `roles/container.admin` or `roles/container.clusterAdmin`. Instead, it delegates GKE/Kubernetes operations to the Operator and DevTeam agents.

3. Provide the Cluster’s Kubernetes Service Account (KSA) with the Google Service Account (GSA) via the Security struct

```yaml
spec:
  workloadIdentity:
    gcp:
      gsaName: "$GSA_NAME"
      projectId: "$PROJECT_ID"
```
