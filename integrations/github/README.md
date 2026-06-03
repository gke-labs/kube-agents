# GitHub Token Broker

The **GitHub Token Broker** is a lightweight, secure microservice designed to act as an authentication proxy for GKE agents. It abstracts the master GitHub App private key PEM, ensuring that client agents (such as `platform-agent` and `devteam-agent`) only request and handle short-lived, repository-scoped installation tokens.

---

## Architecture

1. **Secret Isolation:** The long-lived GitHub App private key PEM is mounted exclusively inside the Token Broker container via the `github-app-credentials` Kubernetes secret.
2. **Short-Lived Tokens:** When an agent requires access to GitHub, it queries the Broker. The Broker issues a signed JWT, calls the GitHub API, and returns a repository-scoped installation token valid for 1 hour.
3. **Network Isolation:** A Kubernetes `NetworkPolicy` restricts ingress to the Token Broker, allowing only authorized agent pods (`app: platform-agent`, `app: devteam-agent`) to communicate with it.

---

## REST API Contract

### Request Token

- **Endpoint:** `GET /token`
- **Query Parameter:** `repository` (Optional, maps to `owner/repo-name`. Restricts the token to this repository).

```bash
curl "http://github-token-broker.agent-system.svc.cluster.local:8080/token?repository=your-org/your-repo"
```

### JSON Response

```json
{
  "token": "ghs_1234567890abcdefghijklmnopqrstuvwxyz",
  "expires_at": "2026-06-03T18:51:47Z",
  "repository": "your-org/your-repo"
}
```

---

## How to Build and Deploy (Suggested Commands)

Follow these steps from a terminal context with push/deploy permissions to build the Docker image and deploy the broker:

### 1. Build and Push the Docker Image

Replace `<REPO>` with your Artifact Registry repository path (e.g. `us-central1-docker.pkg.dev/my-project/my-registry`):

```bash
# Navigate to the broker directory
cd integrations/github

# Build the docker container
docker build -t <REPO>/github-token-broker:latest .

# Push the container to Artifact Registry
docker push <REPO>/github-token-broker:latest
```

### 2. Configure deployment.yaml

Open `deployment.yaml` and update the image path:

- Replace `<REPO>` in `image: <REPO>/github-token-broker:latest` with your Artifact Registry path.

### 3. Deploy to GKE

Run the following `kubectl` command to deploy the deployment, service, and network policies:

```bash
kubectl apply -f deployment.yaml
```

This will create:

- The `github-token-broker` deployment (mounting the existing `github-app-credentials` secret).
- The internal `github-token-broker` service.
- Ingress `NetworkPolicy` restricting access to authorized agent pods.
