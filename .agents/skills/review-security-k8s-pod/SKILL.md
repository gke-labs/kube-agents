---
name: review-security-k8s-pod
description: Reviews Kubernetes Pod security contexts for workload-level isolation and privilege escalation risks.
---

# Task

Review Pod configurations (`PodSecurityContext`, `SecurityContext`) for workload vulnerabilities.

# Checks

## 1. Privilege Escalation & Host Breakout

- **Privileged**: Flag `privileged: true`.
- **Host Namespaces**: Flag `hostNetwork: true`, `hostPID: true`, `hostIPC: true`. If `runAsUser: 0` or `runAsGroup: 0`, flag `hostUsers: true` and flag omitted `hostUsers` (since it's true by default).
- **Host Volumes**: Flag any use of `hostPath` volumes (direct node filesystem access).
- **Privilege Escalation**: Require `allowPrivilegeEscalation: false`.

## 2. Capabilities & Isolation

- **Root Execution**: If `hostUsers: false` or if `hostUsers` is omitted, require `runAsNonRoot: true`. Flag `runAsUser: 0` or `runAsGroup: 0`.
- **Linux Capabilities**: Require `capabilities.drop: ["ALL"]`. Flag highly privileged additions (e.g., `CAP_SYS_ADMIN`, `CAP_NET_ADMIN`, `CAP_NET_RAW`, `CAP_SYS_MODULE`, `CAP_SYS_PTRACE`, `CAP_DAC_OVERRIDE`).
- **Filesystem**: Require `readOnlyRootFilesystem: true` where applicable.
- **Seccomp**: Require seccomp profiles (e.g., `seccompProfile.type: RuntimeDefault`).

## 3. Service Account Hygiene

- **Default Account**: Flag use of the `default` service account.
- **Token Automounting**: Require `automountServiceAccountToken: false` unless API access is explicitly needed.
- **Token Storage**: Flag static Secret-based service account tokens. Require ephemeral `TokenRequest` volume mounts.

## 4. Supply Chain & Immutability

- **Image Digests**: Require container images to be pinned via immutable SHA digests (`@sha256:...`) instead of mutable tags.
