---
name: gke-manifest-generation
description: Standard Operating Procedure (SOP) for generating and updating secure, compliant, and cost-effective GKE manifests.
---

# GKE Manifest Generation Skill

This skill provides guidelines and templates to translate natural language descriptions or application code changes into secure, compliant, and cost-effective Kubernetes YAML manifests optimized for GKE.

## Core Rules & Verification

When generating or updating YAML manifests, you **must** strictly adhere to the following rules:

### 1. Namespace & Resource Isolation

- **Explicit Namespace**: Always declare `namespace: <NAMESPACE>` explicitly in the metadata of every resource (Deployments, Services, ConfigMaps, Secrets, PVCs, Roles, bindings). Map it to the namespace configured in your active `SETTINGS.md`. Never omit the namespace.
- **Dedicated ServiceAccount**: Avoid using the namespace's `default` ServiceAccount. Always create and reference a dedicated `ServiceAccount` (e.g., `devteam-agent-sa`) for each microservice.

### 2. GKE Autopilot-Friendly Resource Tuning

- **Resources Requests & Limits**: Always specify CPU and Memory requests and limits.
- **Density Defaults**: For stateless apps or sidecars, default to conservative requests (e.g., `requests.cpu: "100m"` or `"200m"`, `requests.memory: "256Mi"` or `"512Mi"`) with burstable limits (e.g., `limits.cpu: "4"`, `limits.memory: "4Gi"`).
  - _Rationale_: Autopilot bills directly for CPU/Memory requests. Tuning requests down prevents excessive idle costs.
- **Spot VMs for Staging/Dev**: For non-production workloads (e.g. namespaces containing `-test`, `-dev`, or `-staging`), or if the user requests cost optimization, automatically inject a nodeSelector targeting GKE Spot VMs: `cloud.google.com/gke-spot: "true"`.

### 3. Container Security Hardening (Pod Security Standards)

- **Non-Root Execution**: Always configure `securityContext` at both Pod and container levels to run as a non-root user (e.g., `runAsNonRoot: true`, `runAsUser: 10000`, `runAsGroup: 10000`, `fsGroup: 10000`).
- **Minimal Privileges**: Always set `allowPrivilegeEscalation: false` and `seccompProfile: {type: RuntimeDefault}`.
- **Read-Only Root Filesystem**: Set `readOnlyRootFilesystem: true` to prevent modifications to the container image filesystem.
  - _Writable Directory Fallback_: If `readOnlyRootFilesystem` is enabled, mount a local `emptyDir` volume to `/tmp` or `/var/run/` to allow applications (like Java/Nginx) to write temp files without crashing.
- **Secret Volume Mounting**: Prefer mounting Secrets as read-only files (`volumeMounts` with `defaultMode: 0400`) instead of mapping them as environment variables, unless the application framework exclusively supports env-var based configuration. This prevents secrets leaking into application logs.

### 4. Health Checking (Mandatory Probes)

- **Liveness & Readiness Probes**: Every Deployment container must define both `livenessProbe` and `readinessProbe`.
  - **Web/API**: Use `httpGet` probes.
  - **TCP Services**: Use `tcpSocket` probes.
  - **Databases/Caches**: Use command-based `exec` probes (e.g., `exec.command: ["redis-cli", "ping"]`).
- **Sensible Defaults**: Set `initialDelaySeconds: 5` to `15` depending on startup time (e.g., Java requires a longer delay than Go/Nginx).

### 5. Services & Ingress Routing

- **Internal ClusterIP**: Default all internal microservices to `type: ClusterIP`. Never use `type: LoadBalancer` or `NodePort` unless the workload is explicitly intended to be publicly accessible from the internet.
- **Port Naming**: Always assign clear, standard names to service and container ports (e.g., `name: http-web` or `name: grpc-api`) to enable automatic protocol discovery, tracing, and metrics collection.
- **Prefer Gateway API**: When exposing APIs externally, prioritize using GKE Gateway API (`Gateway` and `HTTPRoute` resources) over legacy `Ingress` objects to enable advanced L7 routing and security features (e.g., Cloud Armor).

### 6. Volume Mounts, StorageClasses & subPath Safety

- **Avoid Directory Overwrites**: When mounting a `ConfigMap` or `Secret` to an application directory containing other files (like Nginx public directories), always use `subPath` to overlay only the specific file.
- **StorageClass Selection**: Use the correct GKE storage class in PersistentVolumeClaims:
  - Use `standard-rwo` (default balanced PD) for general-purpose application storage.
  - Use `premium-rwo` (SSD PD) only when the prompt explicitly requests high IOPS, low latency, or database storage.

### 7. High Availability on GKE

- **Topology Spread**: For deployments with >1 replica, use `podAntiAffinity` or `topologySpreadConstraints` with `topologyKey: "kubernetes.io/hostname"` to distribute pods across GKE nodes and availability zones.
- **PodDisruptionBudget**: For deployments with >1 replica, declare a `PodDisruptionBudget` to guarantee minimum replica availability during voluntary GKE node upgrades.

### 8. Updates & Server-Side Apply Reconciliations

- **Rename List Items**: When modifying existing resource list items (like volume mounts, containers, or ports), rename the item key (e.g., `theme-volume` -> `theme-volume2`) to ensure Kubernetes server-side apply merges the changes cleanly.
- **Minimal Diff**: Make only the changes requested. Adhere closely to existing labels, annotations, and naming conventions.

---

## Specialty Workloads: GKE AI/Inference Serving (vLLM, TGI, etc.)

For model serving workloads, prioritize using optimized tooling like GKE Inference Quickstart if available. If generating manually:

1. **GPU Request & Allocation**:
   - Always request `nvidia.com/gpu` in both `requests` and `limits`.
   - Add a `nodeSelector` or node affinity targeting the desired GKE accelerator tag (e.g., `cloud.google.com/gke-accelerator: nvidia-l4`).
2. **Shared Memory Boost**:
   - Model servers require high shared memory (`/dev/shm`) for inter-process communications. Always declare and mount an `emptyDir` volume with `medium: Memory` to `/dev/shm`.
3. **Weight Loading Optimization**:
   - Mount model weight directories (like GCS buckets) using the GKE GCS Fuse CSI driver (`csi.storage.gke.io`) as `readOnly: true` for efficient cold-starts.

---

## Tooling & Grounding Guidelines

When generating manifests, you should leverage the following tooling to reduce hallucinations and optimize configurations:

1. **Inference Workloads (GKE Inference Quickstart CLI)**:
   - For all AI/LLM inference workloads (e.g., model serving), you **must** prioritize using the `gcloud` CLI GKE Inference Quickstart command to generate the optimized manifests instead of writing them manually:
     ```bash
     gcloud container ai profiles manifests create \
       --model=<MODEL_NAME> \
       --model-server=<SERVER_NAME> \
       --accelerator-type=<ACCELERATOR_TYPE> \
       --output=manifest \
       --output-path=<OUTPUT_FILE_PATH>
     ```
   - _Constraint_: You must include all resources returned by this command (Deployments, Services, PodMonitoring, etc.) without filtering.

2. **Grounding in Official Documentation (Developer Knowledge API)**:
   - For complex, custom, or evolving GKE features (such as GKE Gateway API HTTPRoute configurations, Workload Identity bindings, or StorageClass options), you **must** call the Developer Knowledge API (DK API) to query the latest official Google Cloud GKE documentation.
   - Grounding your manifest generation in the live DK API results is mandatory to ensure syntax compatibility and prevent the use of deprecated fields.
