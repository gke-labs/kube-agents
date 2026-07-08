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
# [TODO] Fill in the actual gateway API token and github credentials
export GATEWAY_TOKEN="<GATEWAY_TOKEN>"
export GITHUB_KEY="<GITHUB_KEY>"

kubectl create secret generic "platformagent-secrets" --namespace $NAMESPACE \
  --from-literal="api-key"="$GATEWAY_TOKEN" \
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

2.4 Grant the Agent admin access to the cluster (allows creating resources)

```bash
gcloud projects add-iam-policy-binding $PROJECT_ID \
    --member "serviceAccount:$GSA_EMAIL" \
    --role "roles/container.admin"
```

2.5 Grant the Platform Agent admin access for managing clusters and their lifecycle

```bash
gcloud projects add-iam-policy-binding $PROJECT_ID \
    --member "serviceAccount:$GSA_EMAIL" \
    --role "roles/container.clusterAdmin"
```

3. Provide the Cluster’s Kubernetes Service Account (KSA) with the Google Service Account (GSA) via the Security struct

```yaml
spec:
  workloadIdentity:
    gcp:
      gsaName: "$GSA_NAME"
      projectId: "$PROJECT_ID"

---

## Exposing OpenClaw Gateway for Google Chat (HTTP Webhooks)

Google Chat API delivery via **HTTPS endpoint URL** (direct Webhook) requires exposing the OpenClaw gateway publicly with TLS termination.

### Developer/Local Testing (ngrok)
For rapid local testing without custom DNS or certificate provisioning, use a port-forward and ngrok:

1. **Start a port-forward** to expose the platform agent gateway service locally:
   ```bash
   kubectl port-forward svc/platform-agent 8642:8642 -n kubeagents-system
   ```
2. **Launch ngrok** to map local port `8642` to a public HTTPS URL:
   ```bash
   ngrok http 8642
   ```
3. **Configure Google Chat API** in the Google Cloud Console (under "Google Chat API" -> "Configuration" -> "Connection settings" -> "HTTPS endpoint URL") to use the forwarded endpoint:
   `https://<your-ngrok-subdomain>.ngrok-free.app/googlechat`

---

### Production Deployment (GKE Ingress + Managed Certificates)
For a production GKE deployment, traffic is routed securely via a Global External Application Load Balancer (GFE) with Google-managed TLS certificates.

#### 1. Reserve a Static IP Address
Reserve a static global IP in your target project:
```bash
gcloud compute addresses create platform-agent-ip --global --project=$PROJECT_ID
```

#### 2. Create the GKE Managed Certificate
Create a `ManagedCertificate` resource so GKE requests and manages the SSL certificate automatically:
```yaml
apiVersion: networking.gke.io/v1
kind: ManagedCertificate
metadata:
  name: platform-agent-cert
  namespace: kubeagents-system
spec:
  domains:
    - platform-agent.yourcompany.com
```

#### 3. Define the GKE Ingress Resource
Create an Ingress to bind the global static IP and certificate, terminating HTTPS traffic and forwarding it to the `platform-agent` Service:
```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: platform-agent-ingress
  namespace: kubeagents-system
  annotations:
    kubernetes.io/ingress.global-static-ip-name: "platform-agent-ip"
    networking.gke.io/managed-certificates: "platform-agent-cert"
    kubernetes.io/ingress.class: "gce"
spec:
  rules:
    - host: platform-agent.yourcompany.com
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: platform-agent
                port:
                  number: 8642
```

#### 4. Configure Cloud DNS
Create an `A` record mapping `platform-agent.yourcompany.com` to the reserved IP address in Cloud DNS or your DNS provider.

#### 5. Save the endpoint in Google Chat API settings
Point the GChat App configuration URL directly to:
`https://platform-agent.yourcompany.com/googlechat`

```
