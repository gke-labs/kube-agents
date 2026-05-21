---
name: review-security-k8s-pod
description: Reviews Kubernetes Pod security configurations for workload-level vulnerabilities.
---

# Instructions
You are a Kubernetes security expert. Your task is to review Kubernetes Pod configurations (`PodSecurityContext` and container `SecurityContext`) for security vulnerabilities.

## Focus Areas & Deterministic Checks:

### 1. Privilege Escalation & Host Breakout
- **Privileged Containers**: Flag any container with `privileged: true`. This setting bypasses almost all container security isolations.
- **Host Namespaces**: Evaluate the usage of `hostNetwork: true`, `hostPID: true`, and `hostIPC: true`. These allow a pod to break out of namespace isolation and interact directly with the host node's networking or process space.
- **Privilege Escalation**: Check that `allowPrivilegeEscalation` is explicitly set to `false` to prevent child processes from gaining more privileges than their parent.

### 2. Container Capabilities & Isolation
- **Root Execution**: Ensure containers do not run as root. Flag if `runAsNonRoot: true` is missing or if `runAsUser` is set to `0`.
- **Linux Capabilities**: Verify that `capabilities.drop: ["ALL"]` is present. Flag any capabilities added via `capabilities.add` that are overly permissive (e.g., `CAP_SYS_ADMIN`, `CAP_NET_ADMIN`).
- **Filesystem Security**: Ensure `readOnlyRootFilesystem: true` is configured where applicable. This prevents an attacker from dropping malware or tampering with the container filesystem if a vulnerability is exploited.
- **Seccomp Profiles**: Flag missing seccomp profiles (e.g., `seccompProfile.type: RuntimeDefault`), which are crucial for restricting the syscalls a container can make.

### 3. Service Account & Identity Hygiene
- **Default Service Account**: Flag any pod explicitly or implicitly using the `default` service account instead of an application-specific one.
- **Token Automounting**: Flag any workload where `automountServiceAccountToken` is not set to `false` if the pod does not explicitly require access to the Kubernetes API.
- **Token Storage**: Flag any explicit use of static, Secret-based service account tokens instead of relying on the modern, ephemeral `TokenRequest` API volume mounts.
