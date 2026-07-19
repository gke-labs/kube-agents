---
name: gke-security-posture-audit
description: Workflows for auditing workload security postures, root execution, privileged containers, hostPath mounts, and Pod Security Admission standards across GKE.
---

# GKE Security Posture Audit

This skill provides automated security audit workflows to detect insecure pod specifications, privileged container executions, and missing Pod Security Admission (PSA) enforcement.

## Workflows

### 1. Detect Privileged Containers & Host Namespace Sharing

Scan all running workloads across all namespaces for security context violations (`privileged: true`, `hostPID: true`, `hostNetwork: true`).

**Command:**

```bash
kubectl get pods -A -o custom-columns=\
NAMESPACE:.metadata.namespace,\
NAME:.metadata.name,\
PRIVILEGED:.spec.containers[*].securityContext.privileged,\
HOST_PID:.spec.hostPID,\
HOST_NET:.spec.hostNetwork
```

### 2. Audit Root Execution (`runAsNonRoot: false`)

Identify pods running as root user (`UID 0`) or missing `runAsNonRoot: true`.

**Command:**

```bash
kubectl get pods -A -o jsonpath='{range .items[*]}{.metadata.namespace}{"\t"}{.metadata.name}{"\t"}{.spec.containers[*].securityContext.runAsNonRoot}{"\n"}{end}'
```

### 3. Check Sensitive Volume Mounts (`hostPath`)

Audit workloads mounting sensitive host paths (`/var/run/docker.sock`, `/etc`, `/var/log`).

**Command:**

```bash
kubectl get pods -A -o jsonpath='{range .items[*]}{.metadata.namespace}{"\t"}{.metadata.name}{"\t"}{.spec.volumes[*].hostPath.path}{"\n"}{end}'
```

### 4. Audit Pod Security Admission (PSA) Labels

Check all non-system namespaces for missing Pod Security Admission labels (`pod-security.kubernetes.io/enforce`).

**Command:**

```bash
kubectl get ns -l 'pod-security.kubernetes.io/enforce'
```
