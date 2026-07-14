# Importing Skills from google/skills

Kube-Agents Platform Agents come pre-packaged with a default set of GKE skills. However, you can import additional or updated skills from the official [google/skills](https://github.com/google/skills/tree/main/skills/cloud) repository into your running Kube-Agents environment.

This guide outlines the two supported methods for importing skills:

1. **Container Image Extension (Recommended for Production)**: Building a custom container image that bundles the desired skills.
2. **Persistent Volume Injection (Recommended for Development & Testing)**: Injecting skills directly into the running agent's persistent storage (`/opt/data/skills/`).

---

## Method 1: Container Image Extension (Production)

This is the most secure and reliable method. It ensures that the skills are baked into the container image, making deployments reproducible and immutable.

### Step 1: Sync or Download the Skill

You can copy the skill folder directly into the `agents/platform/skills/` directory of your `kube-agents` repository.

For example, to import the `gke-compute-classes` skill:

1. Clone the `google/skills` repository locally or use `curl`/`gh` to fetch the files.
2. Place the skill folder under:
   ```path
   agents/platform/skills/gke-compute-classes/
   ```
3. Ensure the folder contains the primary `SKILL.md` file and any accompanying files in the `references/` subdirectory.

### Step 2: Build and Push the Custom Image

Build a new container image targeting the `platform` stage:

```bash
# Build the image using the local Dockerfile
docker build -f deploy/docker/Dockerfile --target platform -t my-registry/kube-agents/platform-agent:v1.1.0 .

# Push to your container registry
docker push my-registry/kube-agents/platform-agent:v1.1.0
```

### Step 3: Update the PlatformAgent Resource

Edit your `PlatformAgent` Custom Resource manifest to point to the new image and tag:

```yaml
apiVersion: kubeagents.x-k8s.io/v1alpha1
kind: PlatformAgent
metadata:
  name: platformagent
  namespace: kubeagents-system
spec:
  deployment:
    image: "my-registry/kube-agents/platform-agent"
    tag: "v1.1.0"
```

Apply the changes:

```bash
kubectl apply -f platformagent.yaml
```

The Kube-Agents Operator will automatically trigger a rolling update of the agent Pod, loading the new skills on boot.

---

## Method 2: Persistent Volume Injection (Development / Testing)

If you are iterating quickly or testing a new skill, you can inject it directly into the running agent's persistent workspace at runtime without rebuilding the container image.

The Platform Agent runtime (Hermes) automatically checks both `/opt/hermes/skills/` (pre-packaged) and the user workspace `$HERMES_HOME/skills/` (defaults to `/opt/data/skills/`) for available skills.

### Step 1: Prepare the Skill Files

Download the skill directory structure from `google/skills`. Ensure it has the following layout:

```path
gke-compute-classes/
├── SKILL.md
└── references/
    ├── compute-class-cost-optimization.md
    ├── compute-class-crd-fields.md
    └── ...
```

### Step 2: Copy to the Running Pod

Use `kubectl cp` to copy the skill directory into the agent container's persistent volume path `/opt/data/skills/`:

```bash
# Get the active platform agent pod name
export AGENT_POD=$(kubectl get pods -n kubeagents-system -l app=platformagent-gateway -o jsonpath='{.items[0].metadata.name}')

# Recreate the target skills directory structure in the pod
kubectl exec -n kubeagents-system $AGENT_POD -c platform-agent -- mkdir -p /opt/data/skills/

# Copy the local skill directory into the pod's persistent workspace
kubectl cp gke-compute-classes/ kubeagents-system/$AGENT_POD:/opt/data/skills/gke-compute-classes -c platform-agent
```

### Step 3: Verify the Import

Log into the agent pod to verify that the files are present:

```bash
kubectl exec -n kubeagents-system -it $AGENT_POD -c platform-agent -- ls -la /opt/data/skills/gke-compute-classes
```

The running Platform Agent will dynamically discover and load the imported skill. Any active sessions or new prompts requesting this capability will immediately utilize the imported guide.
