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

### 3. Container Security Hardening (Pod Security Standards)

- **Non-Root Execution**: Always configure `securityContext` at both Pod and container levels to run as a non-root user (e.g., `runAsNonRoot: true`, `runAsUser: 10000`, `runAsGroup: 10000`, `fsGroup: 10000`).
- **Minimal Privileges**: Always set `allowPrivilegeEscalation: false` and `seccompProfile: {type: RuntimeDefault}`.
- **Read-Only Root Filesystem**: Set `readOnlyRootFilesystem: true` to prevent modifications to the container image filesystem.
  - _Writable Directory Fallback_: If `readOnlyRootFilesystem` is enabled, and the runtime needs to write temp files (like Java/Python/Nginx), mount a local `emptyDir` volume to `/tmp` or `/var/run/` to prevent crashes.

### 4. Health Checking (Mandatory Probes)

- **Liveness & Readiness Probes**: Every Deployment container must define both `livenessProbe` and `readinessProbe`.
  - **Web/API**: Use `httpGet` probes.
  - **TCP Services**: Use `tcpSocket` probes.
  - **Databases/Caches**: Use command-based `exec` probes (e.g., `exec.command: ["redis-cli", "ping"]`).
- **sensible Defaults**: Set `initialDelaySeconds: 5` to `15` depending on startup time (e.g. Java requires a longer delay than Go/Nginx).

### 5. Volume Mounts & subPath Safety

- **Avoid Directory Overwrites**: When mounting a `ConfigMap` or `Secret` to an application directory containing other files (like Nginx public directories), always use `subPath` to overlay only the specific file.

### 6. High Availability on GKE

- **Topology Spread**: For deployments with >1 replica, use `podAntiAffinity` or `topologySpreadConstraints` with `topologyKey: "kubernetes.io/hostname"` to distribute pods across GKE nodes and availability zones.
- **PodDisruptionBudget**: For deployments with >1 replica, declare a `PodDisruptionBudget` to guarantee minimum replica availability during voluntary node upgrades.

### 7. Updates & Server-Side Apply Reconciliations

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
